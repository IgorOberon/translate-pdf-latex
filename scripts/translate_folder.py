#!/usr/bin/env python3
"""Batch translation: translate every supported document in a folder.

Thin wrapper around translate_book.py — each file is processed independently,
so one failure does not stop the queue, and re-running the same command
resumes: finished books (final .ru.pdf present) are skipped, unfinished ones
continue from their translation cache, failed ones are retried.

Supported inputs: .pdf .epub .fb2 .docx .odt .html .htm .rst .txt .md
(.djvu .mobi .azw .azw3 too, when Calibre's ebook-convert is in PATH).

Usage:
  python scripts/translate_folder.py D:\\books --extract math --auto-glossary
  python scripts/translate_folder.py D:\\books -r --outdir D:\\done

Results: <outdir>/<book>_ru/<book>.ru.pdf for each book, plus a convenience
copy of every finished PDF directly in <outdir>. Default outdir:
<folder>/_translated.
"""
import json
import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from provenance import sha_file, book_settings
from build_release import publish_candidate

print(">> translate_folder.py v1.1 loaded", flush=True)

SCRIPTS = Path(__file__).resolve().parent
BOOK_SCRIPT = SCRIPTS / "translate_book.py"

PANDOC_EXT = {".epub", ".fb2", ".docx", ".odt", ".html", ".htm", ".rst"}
MD_EXT = {".md", ".markdown", ".txt"}
CALIBRE_EXT = {".djvu", ".djv", ".mobi", ".azw", ".azw3"}
BASE_EXT = {".pdf"} | PANDOC_EXT | MD_EXT


def discover(folder, recursive, calibre_ok):
    it = folder.rglob("*") if recursive else folder.iterdir()
    files, skipped_fmt = [], []
    for p in sorted(it):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in BASE_EXT or (calibre_ok and suf in CALIBRE_EXT):
            files.append(p)
        elif suf in CALIBRE_EXT:
            skipped_fmt.append(p.name + "  (нужен Calibre/ebook-convert)")
        elif suf:
            skipped_fmt.append(p.name)
    return files, skipped_fmt


def workdir_names(files):
    """Map each file to a unique workdir stem (handle name collisions)."""
    seen = {}
    for p in files:
        seen.setdefault(p.stem.lower(), []).append(p)
    names = {}
    for stem, group in seen.items():
        for p in group:
            names[p] = p.stem if len(group) == 1 else f"{p.stem}_{p.suffix[1:].lower()}"
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="папка с книгами")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="включая подпапки")
    ap.add_argument("--outdir", default=None,
                    help="куда складывать результаты (по умолчанию <папка>/_translated)")
    # passthrough options for translate_book.py
    ap.add_argument("--extract", choices=["auto", "digital", "scan", "math"],
                    default="auto", help="режим извлечения для PDF")
    ap.add_argument("--model", default="hy-mt2-7b")
    ap.add_argument("--host", default="http://localhost:1234")
    ap.add_argument("--mode", choices=["hymt", "generic"], default="hymt")
    ap.add_argument("--glossary", default=None,
                    help="общий глоссарий для всех книг (например, для серии)")
    ap.add_argument("--auto-glossary", action="store_true",
                    help="строить глоссарий автоматически для каждой книги")
    ap.add_argument("--target-lang", default="Russian")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--split-chars", type=int, default=0)
    ap.add_argument("--group-chars", type=int, default=0)
    ap.add_argument("--context-per-request", type=int, default=0)
    ap.add_argument("--glossary-profile", default=None)
    ap.add_argument("--pages-per-chunk", type=int, default=200)
    ap.add_argument("--vram-limit", type=int, default=0)
    ap.add_argument("--force", action="store_true",
                    help="переделывать конвертацию/извлечение заново")
    # сквозные флаги translate_book.py (на всю партию)
    ap.add_argument("--layout", choices=["screen", "print", "compact"],
                    default=None)
    ap.add_argument("--author", default=None)
    ap.add_argument("--heading-numbers", choices=["auto", "latex"],
                    default=None)
    ap.add_argument("--no-clean", action="store_true")
    ap.add_argument("--no-plaques", action="store_true")
    ap.add_argument("--mineru-timeout", type=int, default=None)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--polish", action="store_true",
                    help="полировка каждой книги после перевода")
    ap.add_argument("--polish-model", default=None)
    ap.add_argument("--polish-host", default=None)
    a = ap.parse_args()

    folder = Path(a.folder).resolve()
    if not folder.is_dir():
        print(f"!! нет такой папки: {folder}")
        sys.exit(1)
    outroot = Path(a.outdir).resolve() if a.outdir else folder / "_translated"
    outroot.mkdir(parents=True, exist_ok=True)

    calibre_ok = shutil.which("ebook-convert") is not None
    files, skipped_fmt = discover(folder, a.recursive, calibre_ok)
    # не подхватывать собственные результаты при повторных прогонах
    files = [p for p in files if outroot not in p.parents]
    if not files:
        print("!! в папке нет поддерживаемых документов")
        sys.exit(1)
    names = workdir_names(files)

    print(f">> найдено книг: {len(files)}; результаты: {outroot}")
    if skipped_fmt:
        print(f">> пропущено по формату: {len(skipped_fmt)} "
              f"({', '.join(skipped_fmt[:5])}{'...' if len(skipped_fmt) > 5 else ''})")

    done, skipped, failed = [], [], []
    for i, src in enumerate(files, 1):
        wd = outroot / f"{names[src]}_ru"
        final = wd / f"{src.stem}.ru.pdf"
        head = f"[{i}/{len(files)}] {src.name}"
        release_path = Path(str(final) + ".release.json")
        valid_release = False
        if final.exists() and release_path.exists():
            try:
                release = json.loads(release_path.read_text(encoding="utf-8"))
                expected_glossary = a.glossary or (wd / "work" / "glossary.md" if a.auto_glossary else None)
                valid_release = (release.get("status") == "passed" and release.get("profile") == "release"
                                 and release.get("source_sha256") == sha_file(src)
                                 and release.get("settings_sha256") == book_settings(a, expected_glossary)
                                 and release.get("pdf_sha256") == sha_file(final))
            except (OSError, ValueError):
                pass
        if valid_release and not a.force:
            print(f"\n===== {head}: уже переведена, пропуск =====")
            skipped.append(src.name)
            continue
        print(f"\n===== {head} =====", flush=True)
        cmd = [sys.executable, BOOK_SCRIPT, src, "--workdir", wd,
               "--extract", a.extract, "--model", a.model, "--host", a.host,
               "--mode", a.mode, "--target-lang", a.target_lang,
               "--parallel", a.parallel, "--pages-per-chunk", a.pages_per_chunk,
               "--split-chars", a.split_chars, "--group-chars", a.group_chars,
               "--context-per-request", a.context_per_request]
        if a.glossary_profile:
            cmd += ["--glossary-profile", a.glossary_profile]
        if a.vram_limit:
            cmd += ["--vram-limit", a.vram_limit]
        if a.glossary:
            cmd += ["--glossary", a.glossary]
        if a.auto_glossary:
            cmd += ["--auto-glossary"]
        if a.force:
            cmd += ["--force"]
        if a.layout:
            cmd += ["--layout", a.layout]
        if a.author:
            cmd += ["--author", a.author]
        if a.heading_numbers:
            cmd += ["--heading-numbers", a.heading_numbers]
        if a.no_clean:
            cmd += ["--no-clean"]
        if a.no_plaques:
            cmd += ["--no-plaques"]
        if a.mineru_timeout is not None:
            cmd += ["--mineru-timeout", a.mineru_timeout]
        if a.no_lock:
            cmd += ["--no-lock"]
        if a.polish:
            cmd += ["--polish"]
            if a.polish_model:
                cmd += ["--polish-model", a.polish_model]
            if a.polish_host:
                cmd += ["--polish-host", a.polish_host]
        rc = subprocess.run(list(map(str, cmd))).returncode
        accepted = False
        if rc == 0 and final.exists() and release_path.exists():
            try:
                release = json.loads(release_path.read_text(encoding="utf-8"))
                expected_glossary = a.glossary or (wd / "work" / "glossary.md" if a.auto_glossary else None)
                if (release.get("source_sha256") == sha_file(src)
                        and release.get("settings_sha256") == book_settings(a, expected_glossary)):
                    publish_candidate(final, outroot / f"{names[src]}.ru.pdf", release)
                    accepted = True
            except (OSError, ValueError):
                pass
        if accepted:
            done.append(src.name)
        else:
            failed.append(src.name)
            print(f"!! {src.name}: не завершена (код {rc}) — очередь продолжается")

    print("\n" + "=" * 60)
    print(f"ИТОГО: переведено {len(done)}, пропущено (готовые) {len(skipped)}, "
          f"с ошибкой {len(failed)}")
    if done:
        print("готово: " + ", ".join(done))
    if failed:
        print("не удалось: " + ", ".join(failed))
        print("повторный запуск той же команды продолжит неудачные с их кэша")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
