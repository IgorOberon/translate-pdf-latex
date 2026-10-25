"""Shared bounded LaTeX build, technical acceptance and atomic publication."""
from pathlib import Path
from contextlib import contextmanager
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time

from provenance import canonical_hash, sha_file, write_json
from tex_logs import read_tex_log


AUX_SUFFIXES = (".aux", ".toc", ".out", ".lof", ".lot")
COMPILE_WORK = ".compile-work"
COMPILE_STATE = "compile.state.json"
IMAGE_PAGE_TOLERANCE_PT = 1.0


def _aux_files(build):
    return [p for p in sorted(Path(build).rglob("*"))
            if p.is_file() and p.suffix in AUX_SUFFIXES
            and COMPILE_WORK not in p.relative_to(build).parts]


def aux_state(build):
    build = Path(build)
    return canonical_hash({p.relative_to(build).as_posix(): sha_file(p)
                           for p in _aux_files(build)})


@contextmanager
def _compile_lock(work):
    """OS locks disappear on process termination; stale lock files are harmless."""
    with (work / "lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _source_inputs(main):
    """Find literal TeX dependencies before the first pass; recorder adds the rest."""
    pending, seen, includes = [main], set(), set()
    pattern = (r"\\(include|input|includegraphics|FigWithCaption|bibliography|"
               r"usepackage|documentclass)(?:\[[^\]]*\])?\{([^}]+)\}")
    while pending:
        path = pending.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        if path.suffix not in (".tex", ".sty", ".cls"):
            continue
        for kind, names in re.findall(pattern, path.read_text(encoding="utf-8-sig")):
            for name in names.split(","):
                child = (main.parent / name.strip()).resolve()
                endings = {"include": [".tex"], "input": [".tex", ""],
                           "usepackage": [".sty"], "documentclass": [".cls"],
                           "bibliography": [".bib"]}.get(kind, ["", ".pdf", ".png", ".jpg", ".jpeg"])
                candidates = [child] if child.suffix else [Path(str(child) + ext) for ext in endings]
                existing = next((p for p in candidates if p.is_file()), None)
                if existing is not None:
                    pending.append(existing)
                    if kind == "include" and existing.is_relative_to(main.parent):
                        includes.add(existing.relative_to(main.parent).parent.as_posix())
    return {str(p): sha_file(p) for p in sorted(seen)}, sorted(includes)


def _check_inputs(inputs):
    if any(not Path(path).is_file() or sha_file(path) != digest
           for path, digest in inputs.items()):
        raise ValueError("TeX inputs changed; use a fresh candidate directory")


def _recorded_inputs(attempt, main):
    """Record fonts, graphics and packages actually read by XeTeX as well as TeX."""
    fls = attempt / (main.stem + ".fls")
    found = {}
    if fls.is_file():
        for line in read_tex_log(fls).splitlines():
            if not line.startswith("INPUT "):
                continue
            path = (main.parent / line[6:].strip().strip('"')).resolve()
            if (path.is_file() and not path.is_relative_to(attempt.parent)
                    and path.suffix not in AUX_SUFFIXES):
                found[str(path)] = sha_file(path)
    return found


def _checkpoint(output, state):
    checkpoint = state.get("checkpoint")
    if checkpoint is None:
        return None
    path = (output / checkpoint["path"]).resolve()
    if not path.is_relative_to(output / COMPILE_WORK) or path == output / COMPILE_WORK:
        raise ValueError("checkpoint path escapes compile workspace")
    for name, digest in checkpoint["files"].items():
        file = (path / name).resolve()
        if (not file.is_relative_to(path) or not file.is_file()
                or sha_file(file) != digest):
            raise ValueError("last successful TeX checkpoint changed")
    return path


def _materialize(output, state, checkpoint):
    """Project only committed aux/log files; failed passes never touch them."""
    files = state["checkpoint"]["files"]
    for name in files:
        if Path(name).suffix not in AUX_SUFFIXES + (".log",):
            continue
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".compile-copy-", dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(checkpoint / name, temporary)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _finish_compile(output, main, state, report, checkpoint):
    """Hard-link publication is atomic and refuses to replace any existing PDF."""
    pdf = output / (main.stem + ".pdf")
    digest = state["checkpoint"]["files"][pdf.name]
    if pdf.exists():
        # Recover a crash between exposing our stable PDF and saving completion.
        if not state.get("publishing") or sha_file(pdf) != digest:
            raise ValueError("candidate PDF already exists; use a fresh build directory")
    else:
        state["publishing"] = True
        write_json(output / COMPILE_STATE, state)
        os.link(checkpoint / pdf.name, pdf)
    report.update(status="passed", pdf_sha256=digest, stabilized=True,
                  inputs_sha256=canonical_hash(state["inputs"]))
    report.pop("error", None)
    state.update(status="passed", report=report)
    # Publish the receipt before marking the candidate completed. If saving it
    # is interrupted, the durable `publishing` state can reconstruct it.
    write_json(output / "compile.report.json", report)
    write_json(output / COMPILE_STATE, state)
    return report


def compile_latex(main, output=None, engine="xelatex", max_passes=4, runner=None):
    """Build a fresh candidate, or explicitly resume its own unchanged checkpoint.

    Every engine invocation writes to a separate directory. Successful passes are
    immutable checkpoints; an interrupted pass can never truncate the last good
    aux/toc. Only a stabilized candidate exposes main.pdf in the output root.
    """
    runner = runner or subprocess.run
    main = Path(main).resolve()
    output = Path(output or main.parent).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"stage": "compile", "status": "failed", "passes": [],
              "source_sha256": sha_file(main), "started_ns": time.time_ns()}
    work = output / COMPILE_WORK
    work.mkdir(exist_ok=True)
    try:
        with _compile_lock(work):
            return _compile_locked(main, output, str(engine), max_passes, runner, report)
    except (OSError, ValueError) as exc:
        report["error"] = str(exc)
        # Refusing an unrelated/completed build must not rewrite its receipts.
    return report


def _compile_locked(main, output, engine, max_passes, runner, report):
    state_path = output / COMPILE_STATE
    pdf_name, log_name = main.stem + ".pdf", main.stem + ".log"
    inputs, includes = _source_inputs(main)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (state.get("schema") != 1 or state.get("main") != str(main)
                or state.get("engine") != engine or state.get("status") == "passed"):
            raise ValueError("completed or foreign candidate; use a fresh build directory")
        _check_inputs(state["inputs"])
        if any(state["inputs"].get(path) != digest for path, digest in inputs.items()):
            raise ValueError("TeX input set changed; use a fresh candidate directory")
        checkpoint = _checkpoint(output, state)
        if (output / pdf_name).exists() and not state.get("publishing"):
            raise ValueError("candidate PDF already exists; use a fresh build directory")
        report = state["report"]
        report.update(status="failed", resumed=True)
        report.pop("error", None)
        if checkpoint:
            _materialize(output, state, checkpoint)
        if state.get("publishing"):
            return _finish_compile(output, main, state, report, checkpoint)
    else:
        if (output / pdf_name).exists() or _aux_files(output):
            raise ValueError("candidate PDF/aux already exists; use a fresh build directory")
        state = {"schema": 1, "main": str(main), "engine": engine,
                 "inputs": inputs, "checkpoint": None, "successful_passes": 0,
                 "status": "running", "report": report}
        checkpoint = None
        write_json(state_path, state)
    report["inputs_sha256"] = canonical_hash(state["inputs"])
    try:
        for n in range(state["successful_passes"] + 1, max_passes + 1):
            _check_inputs(state["inputs"])
            attempt = Path(tempfile.mkdtemp(prefix=f"pass-{n}-", dir=output / COMPILE_WORK))
            if checkpoint:
                for path in _aux_files(checkpoint):
                    target = attempt / path.relative_to(checkpoint)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
            for directory in includes:
                (attempt / directory).mkdir(parents=True, exist_ok=True)
            row = {"pass": n, "attempt": len(report["passes"]) + 1,
                   "directory": attempt.relative_to(output).as_posix(), "rc": None}
            report["passes"].append(row)
            # Persist the in-progress attempt before spawning TeX. A hard kill
            # leaves this record and the previous checkpoint fully resumable.
            state["report"] = report
            write_json(state_path, state)
            cmd = [engine, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
                   "-no-shell-escape", "-recorder", f"-output-directory={attempt}", main.name]
            try:
                console = output / f"pass-{row['attempt']}.log"
                with console.open("xb") as stream:
                    result = runner(cmd, cwd=main.parent, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=18000)
                row["rc"] = result.returncode
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                row["error"] = type(exc).__name__ + ": " + str(exc)
                raise
            if row["rc"]:
                raise ValueError(f"{engine} returned {row['rc']}")
            if not (attempt / pdf_name).is_file() or not (attempt / log_name).is_file():
                raise ValueError("fresh PDF/log not produced")
            _check_inputs(state["inputs"])
            recorded = _recorded_inputs(attempt, main)
            if any(path in state["inputs"] and state["inputs"][path] != digest
                   for path, digest in recorded.items()):
                raise ValueError("TeX inputs changed during compilation")
            state["inputs"].update(recorded)
            row["aux_sha256"] = aux_state(attempt)
            log_text = read_tex_log(attempt / log_name)
            rerun = bool(re.search(r"Rerun to get|Label\(s\) may have changed|Please rerun", log_text, re.I))
            # A ToC is first populated by pass 1, then read by pass 2. Compare
            # pass 2 with pass 3 before accepting its printed page destinations.
            toc = attempt / (main.stem + ".toc")
            row["toc_sha256"] = sha_file(toc) if toc.is_file() else None
            stable = bool(checkpoint and state["checkpoint"]["aux_sha256"] == row["aux_sha256"]
                          and not rerun and (not toc.is_file() or n >= 3))
            files = _aux_files(attempt) + [attempt / pdf_name, attempt / log_name]
            state["checkpoint"] = {"path": row["directory"], "aux_sha256": row["aux_sha256"],
                                   "files": {p.relative_to(attempt).as_posix(): sha_file(p) for p in files}}
            state["successful_passes"] = n
            state["report"] = report
            write_json(state_path, state)
            checkpoint = attempt
            _materialize(output, state, checkpoint)
            if stable:
                return _finish_compile(output, main, state, report, checkpoint)
        raise ValueError("aux/toc did not stabilize within pass limit")
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        report["status"] = "failed"
        report["error"] = type(exc).__name__ + ": " + str(exc)
        report["inputs_sha256"] = canonical_hash(state["inputs"])
        state.update(status="failed", report=report)
        write_json(state_path, state)
        write_json(output / "compile.report.json", report)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return report


def inspect_image_geometry(doc):
    """Check image placements against the visible page, not its text margins.

    PyMuPDF returns image bboxes in unrotated, CropBox-relative coordinates.
    Rotate those bboxes to compare them with Page.rect. Raw image bounds also
    include deliberately clipped/bleeding images: crossing the page therefore
    requires review, rather than proving unintended loss. No cover/background
    heuristic silently exempts a placement from this conservative release gate.
    """
    import fitz

    result = {"status": "passed", "coordinate_space": "rotated visible page (CropBox)",
              "tolerance_pt": IMAGE_PAGE_TOLERANCE_PT, "placements": 0,
              "outside_count": 0, "invalid_count": 0, "affected_pages": [], "details": [],
              "text_margins": "not_checked",
              "clipping": "raw image bounds; intentional clipping or bleed also requires review"}
    for page in doc:
        visible = page.rect
        if visible.is_empty or visible.is_infinite or not all(math.isfinite(v) for v in visible):
            raise ValueError(f"invalid visible page rectangle on page {page.number + 1}")
        for occurrence, item in enumerate(page.get_image_info(), 1):
            result["placements"] += 1
            raw = tuple(float(v) for v in item["bbox"])
            bbox = fitz.Rect(raw) * page.rotation_matrix
            valid = (all(math.isfinite(v) for v in (*raw, *bbox))
                     and not bbox.is_empty and not bbox.is_infinite)
            if valid:
                overflow = {"left": max(0.0, visible.x0 - bbox.x0),
                            "top": max(0.0, visible.y0 - bbox.y0),
                            "right": max(0.0, bbox.x1 - visible.x1),
                            "bottom": max(0.0, bbox.y1 - visible.y1)}
                if max(overflow.values()) <= IMAGE_PAGE_TOLERANCE_PT:
                    continue
                result["outside_count"] += 1
                reason = "outside_visible_page"
            else:
                result["invalid_count"] += 1
                reason, overflow = "invalid_image_bounds", None
            # Keep diagnostics JSON-safe even if a malformed transform produced
            # NaN/infinity; never turn an uninspectable image into a pass.
            result["details"].append({"page": page.number + 1, "occurrence": occurrence,
                "reason": reason, "rotation": page.rotation,
                "bbox": [v if math.isfinite(v) else str(v) for v in bbox],
                "visible_page": list(visible), "overflow_pt": overflow})
    if result["details"]:
        result["status"] = "needs_review"
        result["affected_pages"] = sorted({row["page"] for row in result["details"]})
    return result


def inspect_pdf(pdf, log, require_toc=False, source_text=""):
    """Technical checks only. Visual/semantic review are explicitly not_run."""
    pdf, log = Path(pdf), Path(log)
    result = {"stage": "technical_pdf", "status": "failed", "errors": [], "warnings": [],
              "visual_review": {"status": "not_run", "pages": []}, "semantic_review": "not_run"}
    errors = result["errors"]
    if not pdf.is_file() or not log.is_file():
        errors.append("PDF/log missing")
        return result
    text = read_tex_log(log)
    for pattern, name in [(r"(?m)^! |Emergency stop|Fatal error", "TeX error"),
                          (r"Missing character:", "Missing character"),
                          (r"undefined references|Reference .* undefined|Citation .* undefined", "unresolved references"),
                          (r"multiply defined", "duplicate labels")]:
        if re.search(pattern, text, re.I):
            errors.append(name)
    over = [float(n) for n in re.findall(r"Overfull \\[hv]box \((\d+(?:\.\d+)?)pt", text)]
    if over:
        result["warnings"].append({"kind": "overfull", "count": len(over), "max_pt": max(over),
                                   "review": "inspect affected pages in main.log"})
    try:
        import fitz
        from translate import added_cjk, leaks_instructions
        with fitz.open(pdf) as doc:
            if not len(doc):
                errors.append("empty PDF")
            content = "\n".join(page.get_text() for page in doc)
            if not content.strip():
                errors.append("no readable PDF text")
            if re.search(r"⟦M\d{6}⟧|(?<!\w)M\d{6}(?!\w)", content):
                errors.append("unexpanded mask in PDF")
            if "[рисунок не найден]" in content:
                errors.append("missing image placeholder")
            toc = doc.get_toc()
            if require_toc and (not toc or not (pdf.parent / (pdf.stem + ".toc")).is_file()):
                errors.append("required ToC/bookmarks missing")
            if any(not 1 <= row[2] <= len(doc) for row in toc):
                errors.append("invalid bookmark destination")
            titles = "\n".join(row[1] for row in toc)
            if leaks_instructions(source_text, titles) or added_cjk(source_text, titles):
                errors.append("unexpected script/control text in bookmarks")
            fonts = {font[0]: font for page in doc for font in page.get_fonts(full=True)}
            font_rows = []
            for xref, font in fonts.items():
                embedded = bool(doc.extract_font(xref)[3]) if xref else False
                font_rows.append({"name": font[3], "type": font[2], "embedded": embedded})
                if font[2] == "Type3" or not embedded:
                    errors.append(f"unembedded/Type3 font: {font[3]}")
            geometry = inspect_image_geometry(doc)
            result["image_geometry"] = geometry
            if geometry["status"] != "passed":
                errors.append("image geometry needs review: "
                              f"{geometry['outside_count']} placement(s) cross the visible page, "
                              f"{geometry['invalid_count']} invalid image bounds")
            result.update(pages=len(doc), bookmarks=len(toc), fonts=font_rows,
                          pdf_sha256=sha_file(pdf), log_sha256=sha_file(log))
    except Exception as exc:
        errors.append(f"PDF inspection failed: {exc}")
    result["status"] = "failed" if errors else "passed"
    return result


def publish_candidate(pdf, destination, report):
    """Only a passed release report for these exact bytes can replace a final file."""
    if (report.get("status") != "passed" or report.get("profile") != "release"
            or report.get("pdf_sha256") != sha_file(pdf)
            or report.get("technical", {}).get("status") != "passed"
            or report.get("technical", {}).get("pdf_sha256") != sha_file(pdf)
            or report.get("compile", {}).get("status") != "passed"
            or report.get("compile", {}).get("pdf_sha256") != sha_file(pdf)):
        raise ValueError("candidate has not passed release acceptance")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".publish-", suffix=".pdf", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(pdf, tmp)
        if sha_file(tmp) != report["pdf_sha256"]:
            raise ValueError("candidate changed during publication")
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    write_json(str(destination) + ".release.json", report)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compile or resume one unchanged LaTeX candidate; no release publication.")
    parser.add_argument("main_tex", type=Path)
    parser.add_argument("--output", type=Path, help="candidate directory (default: directory containing main_tex)")
    parser.add_argument("--engine", default="xelatex")
    parser.add_argument("--max-passes", type=int, default=4,
                        help="total successful passes allowed across resumptions (default: 4)")
    args = parser.parse_args(argv)
    if not args.main_tex.is_file():
        parser.error("main_tex must be an existing file")
    if args.max_passes < 1:
        parser.error("--max-passes must be positive")
    report = compile_latex(args.main_tex, args.output, args.engine, args.max_passes)
    summary = {"status": report["status"], "passes": len(report["passes"]),
               "resumed": report.get("resumed", False),
               "report": str((args.output or args.main_tex.parent).resolve() / "compile.report.json")}
    if "error" in report:
        summary["error"] = report["error"]
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
