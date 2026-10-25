"""Isolated dispatcher checks with real Pandoc/TeX and optional real local MT.

--host selects an ALREADY available local model, never loads/unloads/downloads.
Without --host: deterministic mock MT, explicitly labelled in summary.
"""
import argparse
from pathlib import Path
import sys
import threading
import subprocess
import json
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from provenance import atomic_text, write_json, sha_file
from translate import split_blocks
import pipeline
from run_integrations import MockMT


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host")
    ap.add_argument("--model", default="mock")
    ap.add_argument("--source-md", help="Optional corrected volume excerpt; read-only; take two complete prose blocks")
    ap.add_argument("--protect", help="Existing PDF whose hash must not change")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    if out.exists():
        raise ValueError("new --out required")
    out.mkdir(parents=True)
    protected = sha_file(a.protect) if a.protect else None
    server = None
    if not a.host:
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockMT)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        a.host = f"http://127.0.0.1:{server.server_port}"
    cfg = {"host": a.host, "model": a.model, "mode": "generic", "timeout": 1200, "parts": 10,
           "title": "Локальная проверка фрагмента", "context_per_request": 32768}
    results = {}
    try:
        if a.source_md:
            all_blocks = split_blocks(Path(a.source_md).read_text(encoding="utf-8"))
            # Complete, already corrected prose; no fresh OCR or semantic claims.
            prose = [b for b in all_blocks if not b.startswith(("#", "<!--", "§", "Zusatz")) and 150 <= len(b) <= 1100]
            if len(prose) < 2:
                raise ValueError("need two whole prose blocks in provided excerpt")
            sources = sorted(prose, key=len)[:2]
        else:
            sources = ["Der Begriff bezeichnet hier eine Bestimmung.\n\nZusatz. Diese Bestimmung bleibt erhalten.",
                       "Das Unendliche ist nicht das Endliche.\n\nHalt-\nund Wesenloses bleibt ein gedruckter Ausdruck."]
        for i, source in enumerate(sources, 1):
            folder = out / f"source-{i}"
            atomic_text(folder / "pages/p0001.txt", source + "\n")
            write_json(folder / "structure.json", {"paragraph_numbers": False,
                "inject": {"1": [{"level": 1, "text": "Prüfung", "ru": f"Фрагмент {i}"}]}})
            job = out / f"job-{i}"
            pipeline.initialize(folder, job, cfg)
            log = out / f"job-{i}.log"
            with log.open("wb") as stream:
                rc = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts/pipeline.py"), "run", str(job)],
                     stdout=stream, stderr=subprocess.STDOUT, timeout=1500).returncode
            state = pipeline.status(job)
            if rc or state["status"] != "done":
                results[f"fragment-{i}"] = {"status": "failed", "rc": rc, "state": state}
                continue
            before = {p.name: sha_file(p) for p in (job / "logs").glob("*.log")}
            assert pipeline.run_job(job) == 0
            after = {p.name: sha_file(p) for p in (job / "logs").glob("*.log")}
            assert before == after, "resume must not invoke stages/MT on an unchanged completed job"
            assert state["polish"] == "not_requested" and state["local_only"]
            pdf = job / "result.pdf"
            import fitz
            with fitz.open(pdf) as doc:
                pages = len(doc)
                doc[-1].get_pixmap(matrix=fitz.Matrix(1.3, 1.3)).save(out / f"fragment-{i}.png")
            results[f"fragment-{i}"] = {"status": "passed", "pdf": str(pdf), "pages": pages,
                 "resume_new_processes": 0, "polish": False, "pdf_sha256": sha_file(pdf)}
        # Ordinary book route uses grouped paragraph envelopes by default.
        # Keep this fixture synthetic even when optional OCR excerpts are given.
        source = out / "grouped-source.md"
        atomic_text(source, "# Mechanics\n\nThe equation $F=ma$ defines force.\n\n"
                           "The energy is $E=mc^2$.\n")
        job = out / "job-grouped-book"
        created = pipeline.initialize(source, job, dict(cfg, source_lang="English",
                                      group_chars=900, split_chars=900))
        with (out / "job-grouped-book.log").open("wb") as stream:
            rc = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts/pipeline.py"),
                                 "run", str(job)], stdout=stream, stderr=subprocess.STDOUT,
                                 timeout=1500).returncode
        state = pipeline.status(job)
        if rc or state["status"] != "done":
            results["grouped-book"] = {"status": "failed", "rc": rc, "state": state}
        else:
            work = job / "work" / created["books"][0]["id"]
            receipt = json.loads((work / "work/book.ru.masked.md.report.json").read_text(encoding="utf-8"))
            assert receipt["request_config"]["paragraph_protocol"] == "paragraph-ids-v1"
            assert len(receipt["paragraph_coverage"]) == 3 and receipt["kept_source"] == 0
            before = {p.name: sha_file(p) for p in (job / "logs").glob("*.log")}
            assert pipeline.run_job(job) == 0
            assert before == {p.name: sha_file(p) for p in (job / "logs").glob("*.log")}
            pdf = work / "grouped-source.ru.pdf"
            results["grouped-book"] = {"status": "passed", "pdf": str(pdf),
                "pdf_sha256": sha_file(pdf), "covered_paragraphs": 3,
                "resume_new_processes": 0, "polish": False}
    finally:
        if server:
            server.shutdown()
            server.server_close()
        same = sha_file(a.protect) == protected if a.protect else None
        summary = {"status": "passed" if results and all(r["status"] == "passed" for r in results.values()) and same is not False else "failed",
              "MT": "mock HTTP" if server else "real local model", "model": a.model,
              "source": "two complete corrected prose blocks; not a new OCR run" if a.source_md else "synthetic",
              "protected_pdf_sha256": protected, "protected_pdf_unchanged": same,
              "routes": results, "semantic_review": "not_run", "visual_review": "not_run"}
        write_json(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
