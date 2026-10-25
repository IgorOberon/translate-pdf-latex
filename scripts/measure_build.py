#!/usr/bin/env python3
"""Measure typesetting-quality metrics of a LaTeX book build.

Reads build artifacts (main.log, main.toc, main.pdf, body.tex) and prints a
JSON metrics report. With --compile it first cleans aux files and runs
xelatex three times, timing the full compile.

Usage:
  python scripts/measure_build.py <build_dir> [--compile] [--json out.json]
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from tex_logs import read_tex_log

print(">> measure_build.py v1", flush=True)

AUX_EXT = (".aux", ".toc", ".out", ".log", ".pdf", ".fls", ".fdb_latexmk",
           ".xdv", ".bbl", ".blg")

TOC_HEADINGS = ("Содержание", "Оглавление", "Contents")
# a TOC heading line, optionally prefixed with a section number: "3.1. Оглавление"
TOC_HEAD_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:%s)\s*$" % "|".join(TOC_HEADINGS))


def compile_build(build: Path, passes: int = 3) -> float:
    for f in build.iterdir():
        if f.stem in ("main", "body") and f.suffix in AUX_EXT and f.suffix != ".tex":
            f.unlink()
    t0 = time.time()
    for i in range(passes):
        print(f">> xelatex pass {i + 1}/{passes}", flush=True)
        r = subprocess.run(["xelatex", "-interaction=nonstopmode", "main.tex"],
                           cwd=build, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        if r.returncode:
            print(f"?? pass {i + 1} exited with code {r.returncode} "
                  "(nonstopmode: build may still be usable)", flush=True)
    return round(time.time() - t0, 1)


def log_metrics(log_text: str) -> dict:
    m = re.search(r"Output written on .*?\((\d+) pages", log_text)
    over = [float(x) for x in re.findall(
        r"^Overfull \\[hv]box \((\d+(?:\.\d+)?)pt too", log_text, re.M)]
    return {
        "pages": int(m.group(1)) if m else None,
        "overfull_count": len(over),
        "overfull_max_pt": max(over) if over else 0.0,
        "underfull_count": len(re.findall(r"^Underfull \\[hv]box", log_text, re.M)),
        "missing_chars": len(re.findall(r"^Missing character:", log_text, re.M)),
    }


def toc_trailing_digits(toc_text: str) -> int:
    """Count \\contentsline titles whose visible text ends in 1-4 digits.

    A trailing page number glued to a heading by OCR looks like
    'Introduction . . . 122' or 'Introduction  122'; we count any title whose
    last token is a bare 1-4 digit number preceded by space or leader dots.
    """
    n = 0
    for line in toc_text.splitlines():
        m = re.match(r"\\contentsline \{[^}]*\}\{(.*)\}\{\d+\}\{[^}]*\}%?$", line)
        if not m:
            continue
        title = m.group(1)
        title = re.sub(r"\\numberline \{[^}]*\}", "", title)
        title = re.sub(r"\\[a-zA-Z@]+ ?", "", title).replace("{", "").replace("}", "")
        if re.search(r"(?:\.\s*){2,}\d{1,4}\s*$|\s{2,}\d{1,4}\s*$|\s\d{1,4}\s*$",
                     title.rstrip()):
            n += 1
    return n


def pdf_metrics(pdf_path: Path, first_pages: int = 30) -> dict:
    import fitz
    doc = fitz.open(str(pdf_path))
    dup, toc_pages = 0, []
    for i in range(min(first_pages, len(doc))):
        lines = [ln.strip() for ln in doc[i].get_text().splitlines()]
        hits = sum(1 for ln in lines if TOC_HEAD_RE.match(ln))
        if hits:
            toc_pages.append(i + 1)
        dup += hits
    # toc_links: hyperlinks on the contiguous run of TOC pages starting at the
    # first heading page (a TOC page carries many internal links)
    links = 0
    if toc_pages:
        p = toc_pages[0] - 1
        while p < len(doc):
            n = len(doc[p].get_links())
            if n < 3 and p != toc_pages[0] - 1:
                break
            links += n
            p += 1
    pages = len(doc)
    doc.close()
    return {"duplicate_toc": dup, "toc_heading_pages": toc_pages,
            "toc_links": links, "pages": pages, "pdf_bytes": pdf_path.stat().st_size}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("build_dir", help="directory with main.tex/body.tex")
    ap.add_argument("--compile", action="store_true",
                    help="clean aux files and run xelatex x3 first (timed)")
    ap.add_argument("--json", default=None, help="write metrics to this file")
    a = ap.parse_args()
    build = Path(a.build_dir).resolve()
    if not (build / "main.tex").exists():
        sys.exit(f"!! no main.tex in {build}")

    metrics = {}
    if a.compile:
        metrics["compile_sec"] = compile_build(build)

    log = build / "main.log"
    if log.exists():
        metrics.update(log_metrics(read_tex_log(log)))
    toc = build / "main.toc"
    if toc.exists():
        metrics["toc_trailing_digits"] = toc_trailing_digits(
            toc.read_text(encoding="utf-8", errors="replace"))
    pdf = build / "main.pdf"
    if pdf.exists():
        metrics.update(pdf_metrics(pdf))
    body = build / "body.tex"
    if body.exists():
        metrics["figure_blocks"] = len(re.findall(
            r"\\includegraphics", body.read_text(encoding="utf-8", errors="replace")))

    out = json.dumps(metrics, ensure_ascii=False, indent=2)
    print(out)
    if a.json:
        Path(a.json).write_text(out + "\n", encoding="utf-8")
        print(f">> written: {a.json}")


if __name__ == "__main__":
    main()
