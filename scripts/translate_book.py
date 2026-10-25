#!/usr/bin/env python3
"""One-shot autonomous book translation: English book -> Russian PDF.

Accepts multiple input formats and routes them automatically:
  .pdf                      -> Marker / MinerU / PyMuPDF (choose with --extract)
  .epub .fb2 .docx .odt
  .html .htm .rst           -> pandoc -> markdown (+ extracted media)
  .txt                      -> treated as markdown
  .md .markdown             -> used as-is (extraction skipped)
  .djvu .mobi .azw .azw3    -> converted to EPUB via Calibre's ebook-convert
                               (install Calibre), then pandoc

Then: mask math -> translate via local model -> unmask -> LaTeX -> PDF.
Resume support: conversion/extraction is skipped if markdown already exists
(unless --force); translation caches every paragraph.

Usage:
  python scripts/translate_book.py book.pdf  --extract math --auto-glossary
  python scripts/translate_book.py book.epub --auto-glossary
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from procutils import (TIMEOUT_RC, PipelineLock, parse_unresolved,  # noqa: E402
                       reap_mineru, run_mineru, unload_llm, untranslated_ok)

from provenance import check_result, sha_file, write_json, canonical_hash, inside, book_settings
from build_release import compile_latex, inspect_pdf, publish_candidate, aux_state
from prepare_mineru import prepare, assert_markdown_structure, assert_typeset_structure
from source_preservation import translatable_output
from tex_logs import read_tex_log
from content_coverage import (clean_with_coverage, verify_cleaning_coverage,
                              apply_book_edits, assert_no_control_chars, load_book_profile)

print(">> translate_book.py v7 (clean, mask-repair, figcaption, layout, "
      "smart-build, audit) loaded", flush=True)

from endpoint_policy import model_opener
OPENER = model_opener()

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}

PANDOC_EXT = {".epub", ".fb2", ".docx", ".odt", ".html", ".htm", ".rst"}
CALIBRE_EXT = {".djvu", ".djv", ".mobi", ".azw", ".azw3"}
MD_EXT = {".md", ".markdown", ".txt"}


def run(cmd, **kw):
    print(">>", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(list(map(str, cmd)), **kw)


def die(msg):
    print("!!", msg)
    sys.exit(1)


UNTRANSLATED_TOL = 0.02     # доля блоков, что можно оставить исходником


def run_translate(cmd, dst, what="перевод", tol=UNTRANSLATED_TOL):
    """translate.py с одним авторетраем при неполном переводе (код 4).

    Провальные блоки не кэшируются, поэтому второй проход стоит ровно их
    повторения и лечит транзиентные 500-ки сервера. Код 3 (сервер мёртв)
    не ретраим. Если после ретрая осталась ГОРСТЬ непереведённых блоков
    (<= tol доли или <= 2 штук — обычно echo на именах/библиографии),
    конвейер НЕ падает, а идёт дальше к вёрстке: эти блоки останутся на
    английском. Массовый недобор (сервер сыплется) по-прежнему = die."""
    report = Path(str(dst) + ".report.json")
    for attempt in (1, 2):
        rc = run([sys.executable, SCRIPTS / "translate.py", *cmd]).returncode
        if rc in (0, 4):
            try:
                check_result(cmd[0], dst)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                die(f"missing/foreign translation report: {exc}")
        if rc == 0:
            return
        if rc == 4 and attempt == 1:
            print(f">> {what} неполный (код 4) — второй проход по "
                  "непереведённым блокам (кэш цел)", flush=True)
            continue
        if rc == 4:
            kept, total = None, None
            try:
                rep = json.loads(report.read_text(encoding="utf-8"))
                kept, total = rep.get("kept_source"), rep.get("blocks_total")
            except (OSError, ValueError, KeyError):
                pass
            if untranslated_ok(kept, total, tol):
                print(f"?? {what}: {kept} из {total} блоков остались исходником "
                      f"(<= допуска {tol:.0%}) — продолжаю; они останутся на "
                      f"английском (обычно имена/библиография). Список: "
                      f"{report.name}", flush=True)
                return
            die(f"{what} не принят: {kept} из {total} блоков остались "
                f"исходником после повторного прохода (см. {report.name}) — "
                "чините сервер (для длинных книг помогает разбиение, см. "
                "README) и перезапустите ту же команду (кэш продолжит). "
                "Ослабить порог: --untranslated-tol")
        die(f"{what} failed (код {rc})"
            + (" — сервер мёртв; кэш цел, перезапуск той же команды продолжит"
               if rc == 3 else " — re-run the same command to resume"))


def run_unmask(src, dst, mapping, what="unmask"):
    """mask_math.py unmask с приёмкой: unresolved > 0 — формулы потеряны,
    прогон не принимается (паритет со skeleton-оркестратором)."""
    r = run([sys.executable, SCRIPTS / "mask_math.py", "unmask",
             src, dst, mapping],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if out.strip():
        print(out.strip(), flush=True)      # вывод стадии не терять
    if r.returncode:
        die(f"{what}: unmasking failed")
    unres = parse_unresolved(out)
    if unres is None:
        die(f"{what}: missing unmask acceptance result; output is not approved")
    elif unres > 0:
        die(f"{what}: unresolved={unres} — формулы потеряны, прогон не "
            "принят; перезапустите перевод (битые ответы не кэшируются) "
            "и повторите")


def preflight(host, model, suffix, extract_mode, have_md):
    try:
        with OPENER.open(host.rstrip("/") + "/v1/models", timeout=5) as r:
            ids = [m.get("id") for m in json.load(r).get("data", [])]
        if model not in ids:
            print(f"?? model '{model}' is not in the server list {ids}; "
                  "LM Studio may still JIT-load it — continuing")
    except Exception as e:
        die(f"Server not reachable at {host} ({e}). "
            "In LM Studio: Developer -> Start Server.")
    if not have_md:
        if suffix == ".pdf":
            need = {"auto": ("marker_single", "pip install marker-pdf"),
                    "scan": ("marker_single", "pip install marker-pdf"),
                    "math": ("mineru", "pip install mineru")}.get(extract_mode)
            if need and not shutil.which(need[0]):
                die(f"'{need[0]}' not found in PATH ({need[1]})")
            if extract_mode == "digital":
                try:
                    import pymupdf4llm  # noqa: F401
                except ImportError:
                    die("pymupdf4llm not installed (pip install pymupdf4llm)")
                print("?? режим digital (pymupdf4llm) — для born-digital PDF "
                      "без формул; растровые картинки извлекаются, но "
                      "векторную графику он не рендерит (её «текст» = мусор). "
                      "Для книг с рисунками/формулами берите --extract auto "
                      "или math.", flush=True)
        elif suffix in CALIBRE_EXT and not shutil.which("ebook-convert"):
            die(f"'{suffix}' needs Calibre's ebook-convert in PATH "
                "(install Calibre from calibre-ebook.com), or convert the "
                "book to EPUB/PDF yourself first")
        elif suffix not in PANDOC_EXT | MD_EXT | {".pdf"}:
            die(f"unsupported input format: {suffix}")
        if suffix == ".pdf" and extract_mode in ("auto", "scan", "math"):
            try:
                import torch
                if not torch.cuda.is_available():
                    print("?? ВНИМАНИЕ: PyTorch без CUDA — извлечение пойдёт на CPU "
                          "в десятки раз медленнее и может упереться в таймаут. "
                          "Лечение: pip install torch torchvision "
                          "--index-url https://download.pytorch.org/whl/cu128 --force-reinstall")
                else:
                    free, _total = torch.cuda.mem_get_info()
                    if free < 6 * 2 ** 30:
                        print(f"?? ВНИМАНИЕ: свободно лишь {free / 2**30:.1f} ГБ VRAM — "
                              "выгрузите LLM из LM Studio (Eject) на время извлечения, "
                              "иначе оно будет медленным")
            except Exception:
                pass
    if not shutil.which("pandoc"):
        die("'pandoc' not found in PATH (install from pandoc.org)")
    if not (shutil.which("latexmk") or shutil.which("xelatex")):
        die("xelatex/latexmk not found (install MiKTeX or TeX Live)")


def find_md(root):
    # derived sanitation artifacts (.clean.md, .removed_*.md) must never be
    # picked as the "source" markdown — that snowballs .clean.clean.md chains
    mds = sorted((p for p in root.rglob("*.md")
                  if "_chunks" not in p.parts
                  and ".clean" not in p.name
                  and ".prepared" not in p.name
                  and ".removed_" not in p.name),
                 key=lambda p: p.stat().st_size, reverse=True)
    return mds[0] if mds else None


def extract_pdf(pdf, outdir, mode, pages_per_chunk=200, vram_limit=0,
                mineru_timeout=3600):
    if mode != "digital":
        unload_llm()
        reap_mineru()
    if mode == "math" and pages_per_chunk:
        pages = None
        try:
            import fitz
            pages = len(fitz.open(str(pdf)))
        except Exception:
            pass
        if pages and pages > pages_per_chunk:
            print(f">> {pages} страниц > {pages_per_chunk}: чанковое извлечение "
                  "(обход 60-минутного таймаута MinerU)")
            ccmd = [sys.executable, SCRIPTS / "extract_chunked.py", pdf, outdir,
                    "--pages-per-chunk", pages_per_chunk,
                    "--mineru-timeout", mineru_timeout]
            if vram_limit:
                ccmd += ["--vram-limit", vram_limit]
            rc = run(ccmd).returncode
            reap_mineru()
            if rc:
                die("chunked extraction failed — перезапустите ту же команду, "
                    "готовые части не потеряются")
            md = find_md(outdir)
            if md is None:
                die("chunked extractor produced no .md file")
            return md
    if mode == "digital":
        import pymupdf4llm
        md = outdir / (pdf.stem + ".md")
        imgdir = outdir / "images"
        # write_images=True: растровые картинки/графику извлечь файлами и
        # вставить ![](...) на их места (иначе digital давал только текст,
        # а «текст» внутри фигур — OCR-мусор)
        try:
            text = pymupdf4llm.to_markdown(
                str(pdf), write_images=True, image_path=str(imgdir),
                image_format="jpg")
            # pymupdf4llm пишет абсолютный путь — нормализуем на images/имя
            text = re.sub(r"!\[\]\((?:[^)]*[/\\])?([^/\\)]+)\)",
                          r"![](images/\1)", text)
        except TypeError:               # старый pymupdf4llm без write_images
            text = pymupdf4llm.to_markdown(str(pdf))
        md.write_text(text, encoding="utf-8")
        n_img = len(list(imgdir.glob("*"))) if imgdir.is_dir() else 0
        if n_img == 0:
            print("?? режим digital не извлёк ни одной картинки — если в "
                  "книге есть рисунки, они потеряны; используйте --extract "
                  "auto (Marker) или math (MinerU)", flush=True)
        return md
    if mode == "math":
        rc = run_mineru(["-p", pdf, "-o", outdir],
                        Path(outdir) / "mineru.log",
                        timeout=mineru_timeout, vram_limit=vram_limit)
        if rc == TIMEOUT_RC:
            die("mineru завис (>--mineru-timeout) и был прерван — "
                "перезапустите ту же команду")
        if rc:
            die("PDF extraction failed")
    else:
        env = dict(os.environ)
        if vram_limit:
            env["MINERU_VIRTUAL_VRAM_SIZE"] = str(vram_limit)
        cmd = ["marker_single", pdf, "--output_dir", outdir,
               "--output_format", "markdown"]
        if mode == "scan":
            cmd.append("--force_ocr")
        if run(cmd, env=env).returncode != 0:
            die("PDF extraction failed")
        reap_mineru()
    md = find_md(outdir)
    if md is None:
        die("extractor produced no .md file")
    return md


def pandoc_to_md(src, outdir):
    d = outdir / src.stem
    d.mkdir(parents=True, exist_ok=True)
    md = d / (src.stem + ".md")
    if run(["pandoc", src, "--to=markdown", "--wrap=none",
            f"--extract-media={d}", "-o", md]).returncode:
        die(f"pandoc failed to convert {src.name}")
    return md


def to_markdown(src, outdir, mode, pages_per_chunk=200, vram_limit=0,
                mineru_timeout=3600):
    """Route any supported input format to a markdown file in outdir."""
    suffix = src.suffix.lower()
    outdir.mkdir(parents=True, exist_ok=True)

    if suffix == ".pdf":
        return extract_pdf(src, outdir, mode, pages_per_chunk, vram_limit,
                           mineru_timeout)

    if suffix in MD_EXT:
        d = outdir / src.stem
        d.mkdir(parents=True, exist_ok=True)
        md = d / (src.stem + ".md")
        shutil.copyfile(src, md)
        # bring sibling images along: folders and loose files
        for item in src.parent.iterdir():
            if item.is_dir() and ("image" in item.name.lower()
                                  or item.name.lower() == "media"):
                shutil.copytree(item, d / item.name, dirs_exist_ok=True)
            elif item.suffix.lower() in IMG_EXT:
                shutil.copy2(item, d / item.name)
        return md

    if suffix in PANDOC_EXT:
        return pandoc_to_md(src, outdir)

    if suffix in CALIBRE_EXT:
        tmp_epub = Path(tempfile.mkdtemp()) / (src.stem + ".epub")
        if run(["ebook-convert", src, tmp_epub]).returncode:
            die("Calibre conversion failed")
        return pandoc_to_md(tmp_epub, outdir)

    die(f"unsupported input format: {suffix}")


def copy_figures(md, build):
    for item in md.parent.iterdir():
        name = item.name.lower()
        if item.is_dir() and ("image" in name or name == "media"):
            shutil.copytree(item, build / item.name, dirs_exist_ok=True)
        elif item.suffix.lower() in IMG_EXT:
            shutil.copy2(item, build / item.name)


# ---- layout profiles & main.tex assembly ------------------------------------
# Overrides are appended to the assembled main.tex AFTER the preamble text;
# assets/preamble.tex itself stays single-source (no per-profile forks).
LAYOUT_BLOCKS = {
    "screen": "",  # default: current behaviour, strict backwards compatibility
    "print": (
        "% --- layout profile: print (twoside, openright, binding offset) ---\n"
        "\\makeatletter\\@twosidetrue\\@openrighttrue\\@mparswitchtrue\\makeatother\n"
        "\\usepackage[twoside,bindingoffset=8mm]{geometry}\n"),
    "compact": (
        "% --- layout profile: compact (dense margins, no vertical stretch) ---\n"
        "\\usepackage[margin=1.8cm]{geometry}\n"
        "\\raggedbottom\n"),
}

# LaTeX разрешает четыре уровня вложенных списков; извлечение MinerU из
# книги с подпунктами вида «a) 1) i) …» легко даёт больше, и сборка встаёт
# с «Too deeply nested». Ставим в СОБИРАЕМЫЙ main.tex, а не в
# assets/preamble.tex: преамбула общая для всех книг и остаётся
# односточниковой, а enumitem нужен только тем, у кого списки глубокие.
# fancyhdr считает высоту колонтитула от 12pt и жалуется «\headheight is too
# small» на каждой странице: в логе NFCM таких строк 996, и они забивают всё
# остальное. Правится одной строкой в СОБИРАЕМЫЙ main.tex — фрактурный
# сборщик так и делает с самого начала, а маршрут MinerU почему-то нет.
HEAD_HEIGHT = ("% --- fancyhdr: высота колонтитула под 11pt ---\n"
               "\\setlength{\\headheight}{14pt}\n"
               "\\addtolength{\\topmargin}{-2pt}\n")

DEEP_LISTS = (
    "% --- глубокая вложенность списков («Too deeply nested») ---\n"
    "\\usepackage{enumitem}\n"
    "\\setlistdepth{9}\n"
    "\\renewlist{enumerate}{enumerate}{9}\n"
    "\\renewlist{itemize}{itemize}{9}\n"
    "\\setlist[enumerate]{label=\\arabic*.}\n"
    "\\setlist[itemize]{label=\\textbullet}\n")

TEX_SPECIALS = {"&": "\\&", "%": "\\%", "$": "\\$", "#": "\\#", "_": "\\_",
                "{": "\\{", "}": "\\}", "~": "\\textasciitilde{}",
                "^": "\\textasciicircum{}", "\\": "\\textbackslash{}"}


def tex_escape(s):
    return "".join(TEX_SPECIALS.get(c, c) for c in s)


def titlepage_block(title, author):
    lines = ["\\begin{titlepage}\\centering",
             "\\vspace*{0.25\\textheight}",
             "{\\Huge\\bfseries %s\\par}" % tex_escape(title)]
    if author:
        lines += ["\\vspace{2em}", "{\\Large %s\\par}" % tex_escape(author)]
    lines += ["\\vfill", "\\end{titlepage}"]
    return "\n".join(lines)


def assemble_main_tex(preamble, layout="screen", title="", author=""):
    """Build the main.tex text: preamble + profile overrides + title page."""
    if layout == "compact":
        # base font size is a class option, so it is swapped in the assembled
        # copy only — the preamble file on disk keeps 11pt
        preamble = preamble.replace("\\documentclass[11pt",
                                    "\\documentclass[10pt", 1)
    parts = [preamble, LAYOUT_BLOCKS[layout], HEAD_HEIGHT, DEEP_LISTS,
             "\\begin{document}"]
    if title:
        parts.append(titlepage_block(title, author))
    parts += ["\\tableofcontents\\clearpage", "\\input{body.tex}",
              "\\end{document}", ""]
    return "\n".join(parts)


def latex_build(build):
    report = compile_latex(Path(build) / "main.tex")
    return report["status"] == "passed", (0 if report["status"] == "passed" else 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book", help="input file: pdf/epub/fb2/docx/odt/html/rst/"
                                 "txt/md (djvu/mobi via Calibre)")
    ap.add_argument("--extract", choices=["auto", "digital", "scan", "math"],
                    default="auto", help="PDF extraction mode (PDF input only)")
    ap.add_argument("--model", default="hy-mt2-7b")
    ap.add_argument("--host", default="http://localhost:1234")
    ap.add_argument("--mode", choices=["hymt", "generic"], default="hymt")
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--glossary-profile", default=None)
    ap.add_argument("--book-profile", default=None, help="exact reviewed cleaning/post-edit decisions")
    ap.add_argument("--split-chars", type=int, default=0)
    ap.add_argument("--context-per-request", type=int, default=0)
    ap.add_argument("--auto-glossary", action="store_true",
                    help="build glossary.md automatically from the extracted text "
                         "(recurring names/terms, translated once by the model)")
    ap.add_argument("--target-lang", default="Russian")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--vram-limit", type=int, default=0,
                    help="ограничить VRAM для MinerU (ГБ), напр. 8 — от зависаний на плотных страницах")
    ap.add_argument("--review-glossary", action="store_true",
                    help="пауза после автогенерации глоссария: правите файл и жмёте Enter")
    ap.add_argument("--parallel", type=int, default=1,
                    help="одновременных запросов к серверу (под слоты LM Studio, напр. 3)")
    ap.add_argument("--group-chars", type=int, default=0,
                    help="объединять мелкие соседние абзацы в один запрос "
                         "(0 = поабзацно). Для книг с тысячами коротких "
                         "абзацев это снимает фиксированные накладные сервера")
    ap.add_argument("--pages-per-chunk", type=int, default=200,
                    help="PDF в режиме math крупнее этого — извлекать частями "
                         "(0 = отключить чанкинг)")
    ap.add_argument("--force", action="store_true",
                    help="redo conversion/extraction even if markdown exists")
    ap.add_argument("--mineru-timeout", type=int, default=3600,
                    help="таймаут одного вызова mineru, сек (0 = без "
                         "таймаута); rc=124 = зависание VLM, перезапуск "
                         "безопасен")
    ap.add_argument("--no-lock", action="store_true",
                    help="не брать глобальный замок конвейера (осторожно: "
                         "два прогона душат друг друга за GPU/память)")
    ap.add_argument("--untranslated-tol", type=float, default=UNTRANSLATED_TOL,
                    help=f"доля блоков, что можно оставить исходником и всё "
                         f"равно собрать PDF (по умолчанию {UNTRANSLATED_TOL:.0%}; "
                         "0 = строго, 1 = никогда не падать из-за перевода)")
    ap.add_argument("--keep-source", help="explicit JSON decisions for unchanged masked/grouped units")
    ap.add_argument("--polish", action="store_true",
                    help="после перевода — литературная полировка "
                         "(mask -> translate --mode polish -> unmask)")
    ap.add_argument("--polish-model", default="qwen3.5:9b")
    ap.add_argument("--polish-host", default="http://localhost:11434")
    ap.add_argument("--no-plaques", action="store_true",
                    help="не спасать текстовые плашки, уехавшие картинками")
    ap.add_argument("--no-clean", action="store_true",
                    help="skip markdown sanitation (OCR TOC removal, running "
                         "heads, stray equation tags)")
    ap.add_argument("--layout", choices=["screen", "print", "compact"],
                    default="screen",
                    help="вёрстка: screen (по умолчанию, прежнее поведение), "
                         "print (twoside+openright+bindingoffset 8mm), "
                         "compact (10pt, поля 1.8cm, raggedbottom)")
    ap.add_argument("--title", default=None,
                    help="титул книги (default: имя входного файла)")
    ap.add_argument("--author", default="",
                    help="автор на титуле (default: пусто)")
    ap.add_argument("--heading-numbers", choices=["auto", "latex"],
                    default="auto",
                    help="auto: если заголовки несут родную нумерацию книги, "
                         "LaTeX-номера разделов подавляются; latex: всегда "
                         "нумеровать по-LaTeX'овски")
    a = ap.parse_args()
    if a.keep_source and a.polish:
        ap.error("--keep-source cannot be combined with --polish; preserved units must remain unchanged")
    if min(a.split_chars, a.group_chars, a.context_per_request) < 0:
        ap.error("split/group/context settings must be nonnegative")
    book_profile = load_book_profile(a.book_profile)
    if a.no_clean and book_profile.get("cleaning", {}).get("decisions"):
        ap.error("--no-clean cannot skip explicit cleaning decisions")
    if a.keep_source and book_profile.get("post_edits"):
        ap.error("post_edits cannot alter a --keep-source output; use a separate reviewed source revision")
    from glossary_selection import load_profile
    load_profile(a.glossary_profile)

    src = Path(a.book).resolve()
    if not src.exists():
        die(f"no such file: {src}")
    suffix = src.suffix.lower()
    root = Path(a.workdir or f"{src.stem}_ru").resolve()
    ex, wk, bl = root / "extract", root / "work", root / "build"
    for d in (ex, wk, bl):
        d.mkdir(parents=True, exist_ok=True)

    # глобальный замок: GPU один, параллельные прогоны душат друг друга,
    # а reap_mineru() добивает mineru по всей машине
    if not a.no_lock:
        PipelineLock(note=f"translate_book:{root.name}").acquire()

    input_hash = sha_file(src)
    preservation_hash = sha_file(a.keep_source) if a.keep_source else None
    book_profile_hash = sha_file(a.book_profile) if a.book_profile else None
    extraction_config = canonical_hash({"source": input_hash, "mode": a.extract,
                                       "chunk": a.pages_per_chunk, "script": sha_file(__file__)})
    extraction_state = root / "extraction.state.json"
    md = None
    if not a.force and extraction_state.is_file():
        state = json.loads(extraction_state.read_text(encoding="utf-8"))
        md = inside(root, state["markdown"])
        if state.get("config_sha256") != extraction_config or sha_file(md) != state.get("output_sha256"):
            die("extraction inputs/output changed: use --force or a separate workdir; existing files retained")
    elif not a.force and find_md(ex):
        die("legacy extraction has no identity: use --force or a separate workdir; cache retained")
    preflight(a.host, a.model, suffix, a.extract, have_md=md is not None)

    # 1) any format -> markdown
    if md:
        print(f">> markdown found, skipping conversion: {md}")
    else:
        ex = Path(tempfile.mkdtemp(prefix="candidate-", dir=ex))
        md = to_markdown(src, ex, a.extract, a.pages_per_chunk, a.vram_limit,
                         a.mineru_timeout)
        write_json(extraction_state, {"config_sha256": extraction_config,
                   "markdown": md.relative_to(root).as_posix(), "output_sha256": sha_file(md)})

    # Recover structure omitted by MinerU's default Markdown exporter BEFORE MT.
    # This is required even with --no-clean; raw OCR and JSON stay unchanged.
    if suffix == ".pdf" and a.extract == "math":
        md = prepare(md)

    # 1a-pre) детекция текстовых плашек, уехавших картинками (быстро, без GPU):
    # только отчёт. Восстановление текста — vision-агентом (см. АГЕНТ.md),
    # т.к. пере-OCR кропов через MinerU стохастически виснет.
    if not a.no_plaques and src.suffix.lower() == ".pdf":
        run([sys.executable, SCRIPTS / "rescue_plaques.py", "detect", md,
             "--json", wk / "plaques_candidates.json",
             "--report", wk / "plaques_report.md"])

    # Every cleaning decision is replayable against exact input bytes.
    cleaning_source = md
    coverage_path = wk / "cleaning.coverage.json"
    if not a.no_clean:
        clean_md = md.with_suffix(".clean.md")
        clean_with_coverage(md, clean_md, coverage_path, profile=book_profile)
        assert_markdown_structure(md.read_text(encoding="utf-8"), clean_md.read_text(encoding="utf-8"))
        md = clean_md
    assert_no_control_chars(md.read_text(encoding="utf-8"))
    coverage_hash = sha_file(coverage_path) if not a.no_clean else None
    rebuild_toc = any(row.get("action") == "rebuild"
                      for row in book_profile.get("cleaning", {}).get("decisions", []))

    structure_source = md.read_text(encoding="utf-8")

    # 1b) glossary: auto-build once if requested and missing
    gpath = Path(a.glossary).resolve() if a.glossary else None
    if a.auto_glossary:
        if gpath is None:
            gpath = wk / "glossary.md"
        if gpath.exists():
            print(f">> glossary found, keeping: {gpath}")
        else:
            if run([sys.executable, SCRIPTS / "make_glossary.py", md, gpath,
                    "--host", a.host, "--model", a.model,
                    "--target-lang", a.target_lang]).returncode:
                die("glossary generation failed")
            print(f"?? auto-glossary written: {gpath} — reviewing it before a long "
                  "run is recommended (edits reset the translation cache)")
            if a.review_glossary:
                try:
                    input(">> ПАУЗА: откройте и поправьте глоссарий, затем Enter для продолжения... ")
                except EOFError:
                    pass

    settings_hash = book_settings(a, gpath)
    # 2) mask
    masked, mapping = wk / "book.masked.md", wk / "book.map.json"
    if run([sys.executable, SCRIPTS / "mask_math.py", "mask",
            md, masked, mapping]).returncode:
        die("masking failed")

    # 3) translate (resumable via cache)
    cmd = [masked, wk / "book.ru.masked.md",
           "--mode", a.mode, "--model", a.model, "--host", a.host,
           "--target-lang", a.target_lang, "--cache-dir", wk / ".tcache",
           "--parallel", a.parallel, "--group-chars", str(a.group_chars),
           "--split-chars", str(a.split_chars), "--context-per-request", str(a.context_per_request)]
    if a.glossary_profile:
        cmd += ["--glossary-profile", a.glossary_profile]
    if gpath and gpath.exists():
        cmd += ["--glossary", gpath]
    if a.keep_source:
        cmd += ["--keep-source", a.keep_source]
    run_translate(cmd, wk / "book.ru.masked.md", what="translation",
                  tol=a.untranslated_tol)

    # 3a) приёмка: доля целевого алфавита; мало кириллицы = сервер падал и
    # блоки остались английскими («keeping source») — прогон не принимаем
    if a.target_lang.strip().lower() == "russian":
        _report = check_result(masked, wk / "book.ru.masked.md")
        _t = re.sub(r"⟦M\d{6}⟧", "", translatable_output(
                    (wk / "book.ru.masked.md").read_bytes().decode("utf-8"), _report))
        _c = len(re.findall(r"[А-Яа-яЁё]", _t))
        _l = len(re.findall(r"[A-Za-z]", _t))
        if _c + _l > 2000 and _c / (_c + _l) < 0.5:
            die(f"перевод неполный: кириллицы лишь {100 * _c // (_c + _l)}% — "
                "видимо, сервер перевода падал; перезапустите (кэш продолжит)")

    # 4) unmask (+гейт: unresolved строго 0)
    ru_md = wk / "book.ru.md"
    run_unmask(wk / "book.ru.masked.md", ru_md, mapping)

    # Book-specific changes require exact, content-bound decisions. Keep the
    # raw unmasked output as evidence and publish a distinct checked derivative.
    unmasked = ru_md
    _text = unmasked.read_bytes().decode("utf-8")
    _fixed, post_report = apply_book_edits(_text, book_profile)
    post_report_path = wk / "postprocess.coverage.json"
    write_json(post_report_path, post_report)
    if _fixed != _text:
        ru_md = wk / "book.ru.reviewed.md"
        ru_md.write_text(_fixed, encoding="utf-8", newline="")
    post_report_hash = sha_file(post_report_path)
    unmasked_hash = sha_file(unmasked)

    # 4b) полировка (опционально): литературная правка переведённого текста
    # по замаскированному файлу — формулы не уходят в полировщик; канон
    # описан в КОНФИГУРАЦИИ.md (слои П1–П3), тут он автоматизирован
    if a.polish:
        p_masked = wk / "polish.masked.md"
        p_map = wk / "polish.map.json"
        p_out = wk / "polish.ru.masked.md"
        polished = wk / "book.ru.polished.md"
        if run([sys.executable, SCRIPTS / "mask_math.py", "mask",
                ru_md, p_masked, p_map]).returncode:
            die("polish: masking failed")
        pcmd = [p_masked, p_out,
                "--mode", "polish", "--model", a.polish_model,
                "--host", a.polish_host, "--target-lang", a.target_lang,
                "--cache-dir", wk / ".tcache_polish", "--parallel", a.parallel,
                "--split-chars", a.split_chars, "--group-chars", a.group_chars,
                "--context-per-request", a.context_per_request]
        if a.glossary_profile:
            pcmd += ["--glossary-profile", a.glossary_profile]
        if gpath and gpath.exists():
            pcmd += ["--glossary", gpath]
        run_translate(pcmd, p_out, what="polish", tol=a.untranslated_tol)
        run_unmask(p_out, polished, p_map, what="polish")
        ru_md = polished

    assert_no_control_chars(ru_md.read_text(encoding="utf-8"))
    assert_markdown_structure(structure_source, ru_md.read_text(encoding="utf-8"))
    assembly_hash = canonical_hash({"md": sha_file(ru_md), "source": sha_file(src),
                                   "translation": sha_file(wk / "book.ru.masked.md")})
    bl = Path(tempfile.mkdtemp(prefix="candidate-", dir=bl))
    write_json(bl / "release.report.json", {"schema": 1, "status": "failed", "profile": "release",
                                            "reason": "candidate not yet accepted"})
    # 5) markdown -> LaTeX (figcaption.lua glues figures to their captions;
    # structure.lua demotes OCR front matter and, when headings carry the
    # original numbering, switches LaTeX section numbers off)
    # --no-highlight: без него pandoc на любом ``` -блоке выдаёт \NormalTok и
    # окружения Shaded/Highlighting, которых в преамбуле нет. На «New
    # Foundations for Classical Mechanics» это дало 30 «Undefined control
    # sequence» в одном приложении. Подсветка синтаксиса переводной книге по
    # математике не нужна, а окружения тянуть в преамбулу — лишняя связность.
    if run(["pandoc", ru_md,
            "--from=markdown+raw_tex+tex_math_dollars", "--to=latex",
            "--no-highlight",
            "--top-level-division=chapter", "--wrap=preserve",
            "--lua-filter", SKILL_ROOT / "assets" / "figcaption.lua",
            "--lua-filter", SKILL_ROOT / "assets" / "structure.lua",
            "-M", f"heading_numbers={a.heading_numbers}",
            "-o", bl / "body.tex"]).returncode:
        die("pandoc failed")
    assert_typeset_structure(ru_md.read_text(encoding="utf-8"),
                            (bl / "body.tex").read_text(encoding="utf-8"))
    preamble = (SKILL_ROOT / "assets" / "preamble.tex").read_text(encoding="utf-8")
    (bl / "main.tex").write_text(
        assemble_main_tex(preamble, layout=a.layout,
                          title=(a.title if a.title is not None else src.stem),
                          author=a.author),
        encoding="utf-8")
    copy_figures(md, bl)

    # 5b) комплектность картинок: чего не хватает — честно предупредить заранее
    refs = set(re.findall(r"\\(?:includegraphics[^{]*|FigWithCaption)\{([^}]+)\}",
                          (bl / "body.tex").read_text(encoding="utf-8", errors="replace")))
    missing = sorted(r for r in refs if not (bl / r).exists())
    if missing:
        print(f"?? не хватает {len(missing)} из {len(refs)} картинок — в PDF они "
              "станут рамками-заглушками; список: build/missing_images.txt")
        (bl / "missing_images.txt").write_text("\n".join(missing), encoding="utf-8")

    # 6) compile
    ok, rc = latex_build(bl)
    if not ok:
        log = bl / "main.log"
        if log.exists():
            tail = read_tex_log(log).splitlines()[-30:]
            print("\n".join(tail))
        die(f"LaTeX failed — inspect {log}. Typical fix: escape a special "
            "character in build/body.tex, then re-run (translation is cached).")
    final = root / f"{src.stem}.ru.pdf"
    # Publication follows mandatory technical acceptance below.

    # 7) consistency self-audit (diagnosis only; soft — never fails the run)
    try:
        import fitz  # noqa: F401
        if run([sys.executable, SCRIPTS / "audit_selfcheck.py", ru_md,
                bl / "body.tex", bl / "main.pdf",
                "--report", root / "audit_report.md"]).returncode:
            print("?? самоаудит нашёл проблемы гигиены PDF — "
                  "см. audit_report.md", flush=True)
    except ImportError:
        print("?? PyMuPDF не установлен — самоаудит пропущен "
              "(pip install pymupdf)")
    if rebuild_toc and r"\tableofcontents" not in (bl / "main.tex").read_text(encoding="utf-8"):
        die("TOC rebuild decision requires a generated table of contents")
    technical = inspect_pdf(bl / "main.pdf", bl / "main.log",
                            require_toc=rebuild_toc or bool(re.search(r"(?m)^#", ru_md.read_text(encoding="utf-8"))),
                            source_text=md.read_text(encoding="utf-8"))
    raw = check_result(masked, wk / "book.ru.masked.md")
    if book_settings(a, gpath) != settings_hash:
        die("translation settings/profile changed during build; candidate not published")
    if (sha_file(a.book_profile) if a.book_profile else None) != book_profile_hash:
        die("book profile changed during build; candidate not published")
    if not a.no_clean:
        verify_cleaning_coverage(cleaning_source, md, coverage_path, profile=book_profile)
        if sha_file(coverage_path) != coverage_hash:
            die("cleaning receipt changed during build")
    if sha_file(post_report_path) != post_report_hash or sha_file(unmasked) != unmasked_hash:
        die("postprocessing evidence changed during build")
    if ((sha_file(a.keep_source) if a.keep_source else None) != preservation_hash
            or raw.get("source_preservation_sha256") != preservation_hash):
        die("keep-source policy changed during build; published PDF unchanged")
    if (sha_file(src) != input_hash or assembly_hash != canonical_hash({"md": sha_file(ru_md),
            "source": sha_file(src), "translation": sha_file(wk / "book.ru.masked.md")})):
        die("assembly inputs changed during build; published PDF unchanged")
    partial = raw["status"] != "passed" or bool(raw.get("kept_source")) or bool(raw.get("failed_blocks"))
    if a.polish:
        partial |= check_result(p_masked, p_out)["status"] != "passed"
    release = {"schema": 1, "profile": "draft" if partial else "release",
               "status": "failed" if technical["status"] != "passed" or missing else "warning" if partial else "passed",
               "source_sha256": sha_file(src), "pdf_sha256": sha_file(bl / "main.pdf"),
               "settings_sha256": settings_hash,
               "compile": json.loads((bl / "compile.report.json").read_text(encoding="utf-8")),
               "technical": technical, "translation_report_sha256": sha_file(str(wk / "book.ru.masked.md") + ".report.json"),
               "preserved_source": raw.get("preserved_source", 0),
               "allowed_source": raw.get("allowed_source", []),
               "source_preservation_sha256": preservation_hash,
               "book_profile_sha256": book_profile_hash,
               "cleaning_coverage_sha256": coverage_hash,
               "postprocess_coverage_sha256": post_report_hash,
               "visual_review": "not_run", "semantic_review": "not_run"}
    write_json(bl / "release.report.json", release)
    if release["status"] != "passed":
        print(f"!! candidate only ({release['status']}), previous PDF untouched: {bl}")
        sys.exit(4 if partial else 3)
    publish_candidate(bl / "main.pdf", final, release)
    print(f"\nDONE: {final} (technical acceptance only)")


if __name__ == "__main__":
    main()
