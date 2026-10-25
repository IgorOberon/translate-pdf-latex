"""Opt-in real Pandoc/TeX integration; mock localhost MT, no model/GPU/book.

python tests/run_integrations.py --out <new-directory>
Keeps candidate PDFs, previews, logs and a machine-readable summary.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from provenance import atomic_text, write_json, sha_file, sha_text, blocks
from build_release import compile_latex, inspect_pdf
from translate import validate_candidate
from paragraph_units import MARKER, parse_response, encode


def mock_translate_body(source):
    """Deterministic fixture translation; framing remains a real translator gate."""
    if source.startswith("#"):
        number = re.match(r"#+\s+(\d+(?:\.\d+)*)\s", source)
        return re.match(r"#+", source)[0] + " " + (number[1] + " " if number else "") + "Основные определения"
    target = "Проверенный тестовый перевод сохраняет определения и формулы. "
    target += " ".join(re.findall(r"⟦M\d{6}⟧", source))
    return target.strip()


def mock_translate_request(source):
    if source.startswith("Translate the following segment"):
        source = source.split("\n\n", 1)[1]
    if MARKER.search(source):
        parts = [{"fragment_id": m[2]} for m in MARKER.finditer(source) if not m[1]]
        return encode(parts, [mock_translate_body(body) for body in parse_response(source, parts)])
    return mock_translate_body(source)


class MockMT(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply({"data": [{"id": "mock"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        source = payload["messages"][-1]["content"]
        target = mock_translate_request(source)
        self.reply({"choices": [{"message": {"content": target.strip()}, "finish_reason": "stop"}]})

    def reply(self, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if out.exists():
        raise SystemExit("Use a NEW output directory; old evidence is never overwritten")
    out.mkdir(parents=True)
    missing = [x for x in ("xelatex", "pandoc") if not shutil.which(x)]
    if missing:
        write_json(out / "summary.json", {"status": "not_run", "missing": missing})
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockMT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def run(label, script, *argv):
        with (out / (label + ".log")).open("wb") as log:
            r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / script), *map(str, argv)],
                               cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        if r.returncode:
            raise RuntimeError(f"{label}: rc={r.returncode}; inspect retained log")

    try:
        # DE OCR text route: synthetic OCR pages, strict structure, MT manifest,
        # unchanged raw report through postprocessing, real release assembler.
        de = out / "de"
        (de / "pages").mkdir(parents=True)
        for n in (1, 2):
            atomic_text(de / "pages" / f"p{n:04d}.txt", f"s. {n}.\n\nDer Begriff ist hier ein synthetisches Beispiel.\n\n")
        structure = {"paragraph_numbers": True, "paragraph_profile": {"range": [1, 2]},
                     "inject": {"1": [{"level": 1, "text": "Einleitung", "ru": "Введение"}]}}
        write_json(de / "structure.json", structure)
        run("de-ocr", "scripts/build_ocr_md.py", de, "--structure", de / "structure.json", "--parts", "4")
        run("de-mt", "scripts/run_doc_translate.py", de, "--host", host, "--model", "mock", "--parallel", "1")
        run("de-edit", "scripts/postfix_ru.py", de)
        run("de-build", "scripts/assemble_ru_book.py", de, "--title", "Проверка OCR", "--pdf", de / "published.pdf")
        results["DE_fraktur_text"] = {"status": "passed", "ocr_engine": "not_run (synthetic OCR text)", "pdf": "de/published.pdf"}

        # EN math: exercise the actual single-book entrypoint end to end.
        en = out / "en"
        en.mkdir()
        atomic_text(en / "source.md", "# Mechanics\n\nThe equation $F=ma$ defines the force.\n\nThe energy is $$E=mc^2.$$\n")
        run("en-pipeline", "scripts/translate_book.py", en / "source.md", "--host", host,
            "--model", "mock", "--workdir", en / "work", "--no-lock", "--no-plaques",
            "--group-chars", "900", "--split-chars", "900")
        mt_report = json.loads((en / "work/work/book.ru.masked.md.report.json").read_text(encoding="utf-8"))
        assert len(mt_report["paragraph_coverage"]) == 3, mt_report
        assert mt_report["blocks_total"] == 1 and mt_report["kept_source"] == 0, mt_report
        assert Path(mt_report["attempt_journal"]).is_dir(), mt_report
        results["EN_math"] = {"status": "passed", "pdf": "en/work/source.ru.pdf",
                              "source_paragraphs": 3, "covered_paragraphs": 3,
                              "grouped_requests": True, "attempt_journal": "retained"}

        # A long original bibliography is explicitly retained; its Latin text
        # must not trip the target-script heuristic or silently relax failures.
        retained = out / "preserved"
        retained.mkdir()
        bibliography = "Author reference and original bibliographic spelling. " * 50
        bibliography = bibliography.strip()
        atomic_text(retained / "source.md", "# References\n\n" + bibliography)
        write_json(retained / "keep.json", {"schema": 1, "blocks": [{"index": 2,
            "source_sha256": sha_text(bibliography), "reason": "Bibliography retained in its original spelling"}]})
        original = sha_file(retained / "source.md")
        run("preserved-pipeline", "scripts/translate_book.py", retained / "source.md", "--host", host,
            "--model", "mock", "--workdir", retained / "work", "--no-lock", "--no-plaques", "--no-clean",
            "--layout", "compact", "--keep-source", retained / "keep.json")
        receipt = json.loads((retained / "work/source.ru.pdf.release.json").read_text(encoding="utf-8"))
        assert receipt["profile"] == "release" and receipt["status"] == "passed", receipt
        assert receipt["preserved_source"] == 1 and len(receipt["allowed_source"]) == 1
        assert sha_file(retained / "source.md") == original
        assert bibliography in (retained / "work/work/book.ru.masked.md").read_text(encoding="utf-8")
        results["explicit_source_preservation"] = {"status": "passed", "profile": "release",
            "preserved_source": 1, "pdf": "preserved/work/source.ru.pdf"}

        # Multiscript: technical validator + real fonts/PDF, including lawful CJK.
        mixed = out / "mixed"
        mixed.mkdir()
        src = "The names 王 and λόγος appear here."
        target = "Здесь встречаются имена 王 и λόγος."
        assert validate_candidate(src, target).ok
        assert not validate_candidate(src, target + " 新增").ok
        atomic_text(mixed / "main.tex", r"\documentclass{article}" + "\n" +
            r"\usepackage{fontspec}\setmainfont{Arial Unicode MS}\usepackage{hyperref}" + "\n" +
            r"\begin{document}\section{Проверка} " + target + r"\end{document}")
        compiled = compile_latex(mixed / "main.tex")
        assert compiled["status"] == "passed", compiled
        technical = inspect_pdf(mixed / "main.pdf", mixed / "main.log", source_text=src)
        write_json(mixed / "technical.json", technical)
        assert technical["status"] == "passed", technical
        results["mixed_scripts"] = {"status": "passed", "pdf": "mixed/main.pdf"}

        # Foreign skeleton: chapter translation + insertion + real build.
        spec = importlib.util.spec_from_file_location("test_skeleton_integration", ROOT / "skeleton" / "translate_into_skeleton.py")
        sk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sk)
        skdir = out / "skeleton"
        (skdir / "chapters").mkdir(parents=True)
        atomic_text(skdir / "chapters" / "ch01.tex", r"\chapter{Основные определения}" + "\n" + r"\section{Сила}" + "\n% >>> сюда вставить перевод раздела 1.1 <<<\n")
        md = skdir / "source.md"
        atomic_text(md, "## 1.1 Force\n\nThe equation $F=ma$ defines the force.\n")
        # Keep numbered headings in this subcase (the rest use fixed mock headings).
        from types import SimpleNamespace
        ru = sk.translate_chapter(skdir, md, SimpleNamespace(mode="hymt", model="mock", host=host,
                                                            glossary=None, force=False, parallel=1))
        intro, sections, duplicates = sk.split_ru_md(ru.read_text(encoding="utf-8"))
        assert not duplicates and "1.1" in sections
        fragment = sk.md_to_fragment(sections["1.1"], skdir, "1_1")
        filled, left = sk.insert_fragments(sk.parse_stub(skdir / "chapters" / "ch01.tex"),
                                           {"1.1": fragment}, "", "integration")
        assert filled == ["1.1"] and not left
        assert "F=ma" in (skdir / "chapters" / "ch01.tex").read_text(encoding="utf-8")
        preamble = (ROOT / "assets" / "preamble.tex").read_text(encoding="utf-8")
        atomic_text(skdir / "main.tex", preamble + "\n" + r"\begin{document}\tableofcontents\clearpage\input{chapters/ch01}\end{document}")
        ok, pdf = sk.build(skdir / "main.tex", "xelatex")
        assert ok, pdf
        results["foreign_skeleton"] = {"status": "passed", "pdf": "skeleton/main.pdf",
                                       "scope": "chapter MT, split, Pandoc fragment, insertion, foreign main, transactional build"}
    except Exception as exc:
        import traceback
        results["failure"] = {"status": "failed", "error": str(exc), "traceback": traceback.format_exc()}
    finally:
        server.shutdown()
        server.server_close()

    import fitz
    for name, row in results.items():
        if row.get("pdf"):
            with fitz.open(out / row["pdf"]) as doc:
                row["pages"] = len(doc)
                row["sha256"] = sha_file(out / row["pdf"])
                # First content page where practical, retained for human visual QA.
                page = doc[-1]
                page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2)).save(out / (name + ".png"))
    status = "failed" if "failure" in results else "passed"
    write_json(out / "summary.json", {"status": status, "routes": results, "MT": "mock HTTP; no real model",
                                       "semantic_review": "not_run", "visual_review": "not_run"})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
