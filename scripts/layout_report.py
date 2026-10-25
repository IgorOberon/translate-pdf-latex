#!/usr/bin/env python3
"""Post-build layout report: metrics table, worst overfulls, page previews.

Reads build artifacts (main.log/main.toc/main.pdf) and prints:
  - metrics: pages, pdf_bytes, overfull count/max, underfull, missing
    characters WITH the list of glyphs, toc_links;
  - top-5 worst Overfull boxes with their PDF pages;
  - hygiene gates reused from audit_selfcheck/measure_build (not duplicated):
    M###### = 0, «⟦» = 0, duplicate_toc = 1, toc_trailing_digits = 0,
    «[рисунок не найден]» = 0; «??» is a WARNING with contexts;
  - renders build/preview/p_{first,mid,last}.png.

Usage:
  python scripts/layout_report.py <build_dir>
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from tex_logs import read_tex_log

# отчёт содержит экзотические глифы (список Missing characters) — не дать
# консоли cp1251 уронить печать
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print(">> layout_report.py v1", flush=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_build import toc_trailing_digits, pdf_metrics  # noqa: E402
from audit_selfcheck import audit_pdf                       # noqa: E402

TOKEN_RE = re.compile(
    r"Overfull \\[hv]box \((\d+(?:\.\d+)?)pt too \w+\)"
    r"(?: in paragraph at lines (\d+)--(\d+))?"
    r"|Underfull \\[hv]box"
    r"|Missing character: There is no (.) in font ([^!\n]*)"
    r"|\[(\d+)[\]\s]")

# Диагноз ставится по ШРИФТУ, а не по списку глифов. Кириллица, пропавшая в
# cmmi8/cmr10, значит «русский текст затянуло в математику»; латиница в rsfs —
# «\mathscr со строчной буквой, у этого шрифта строчных глифов нет». Список
# самих символов («нет о, нет е, нет с…») не говорит ни о том, ни о другом.
CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
MATH_FONT_RE = re.compile(r"^(?:cm(?:mi|sy|ex|r|bx|ti)|rsfs|msam|msbm|eufm)")


def diagnose_font(font, chars):
    f = (font or "").strip()
    cyr = [c for c in chars if CYRILLIC_RE.match(c)]
    if cyr and MATH_FONT_RE.match(f):
        return ("кириллица в математическом шрифте: русский текст попал "
                "внутрь $…$ — искать в body.tex")
    if f.startswith("rsfs"):
        return ("rsfs не имеет СТРОЧНЫХ глифов: это \\mathscr{строчная} — "
                "лечится заменой на \\mathit (clean_markdown repair-math)")
    if cyr:
        return "в шрифте нет кириллицы — сменить шрифт или вынести текст"
    return "глифов нет в шрифте"


def scan_log(text):
    overfulls, underfull, missing = [], 0, Counter()
    by_font = {}
    pending = []
    for m in TOKEN_RE.finditer(text):
        if m.group(1):
            pending.append({"pt": float(m.group(1)),
                            "src_lines": m.group(2) and f"{m.group(2)}–{m.group(3)}"})
        elif m.group(0).startswith("Underfull"):
            underfull += 1
        elif m.group(4):
            missing[m.group(4)] += 1
            by_font.setdefault((m.group(5) or "?").strip(),
                               Counter())[m.group(4)] += 1
        elif m.group(6):
            page = int(m.group(6))
            for o in pending:
                o["page"] = page
            overfulls.extend(pending)
            pending = []
    overfulls.extend(pending)
    pages = None
    mm = re.search(r"Output written on .*?\((\d+) pages", text, re.S)
    if mm:
        pages = int(mm.group(1))
    return pages, overfulls, underfull, missing, by_font


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("build_dir", help="directory with main.log/main.toc/main.pdf")
    a = ap.parse_args()
    build = Path(a.build_dir).resolve()
    log = build / "main.log"
    pdf = build / "main.pdf"
    if not log.exists() or not pdf.exists():
        sys.exit(f"!! main.log/main.pdf not found in {build}")

    pages, overfulls, underfull, missing, by_font = scan_log(
        read_tex_log(log))
    if pages is None:
        try:
            pages = pdf_metrics(pdf)["pages"]
        except (ImportError, OSError, ValueError):
            pass  # unavailable is not zero pages

    print("== layout report ==")
    print(f"pages            : {pages}")
    print(f"pdf_bytes        : {pdf.stat().st_size}")
    print(f"overfull         : {len(overfulls)}"
          + (f" (max {max(o['pt'] for o in overfulls):.1f}pt)" if overfulls else ""))
    print(f"underfull        : {underfull}")
    print(f"missing chars    : {sum(missing.values())}")
    # разбор по ШРИФТАМ: список глифов сам по себе диагноза не даёт, а шрифт
    # даёт — этим и нашли rsfs/\mathscr на книге Хестенса
    for font, chars in sorted(by_font.items(), key=lambda kv: -sum(kv[1].values())):
        top = ", ".join(f"{c}×{k}" for c, k in chars.most_common(8))
        print(f"   {font}: {sum(chars.values())} ({top})")
        print(f"      -> {diagnose_font(font, chars)}")

    gates_ok = True
    try:
        met = pdf_metrics(pdf)
        print(f"toc_links        : {met['toc_links']}")
        print("-- top-5 overfull --")
        for o in sorted(overfulls, key=lambda o: -o["pt"])[:5]:
            print(f"  {o['pt']:8.1f}pt  page {o.get('page', '?'):>4}"
                  f"  (body lines {o.get('src_lines') or '—'})")
        hard, contexts = audit_pdf(pdf)
        toc = build / "main.toc"
        trailing = toc_trailing_digits(
            toc.read_text(encoding="utf-8", errors="replace")) if toc.exists() else 0
        gates = [
            ("mask tokens M###### == 0", hard["mask tokens M######"] == 0),
            ("«⟦» == 0", hard["«⟦»"] == 0),
            ("[рисунок не найден] == 0", hard["[рисунок не найден]"] == 0),
            # Имя ключа обманывает: это НЕ число дублей, а сколько раз
            # заголовок «Содержание» встретился на первых 30 полосах PDF.
            # Единица — норма (оглавление одно); ноль значит, что его нет
            # вовсе, двойка и больше — что оно напечаталось дважды. На NFCM
            # «duplicate_toc=1» приняли за найденный дубль и полезли искать
            # его в main.toc, где ничего и не было.
            ("оглавление ровно одно (заголовков «Содержание» на первых "
             "полосах: %d)" % met["duplicate_toc"],
             met["duplicate_toc"] == 1),
            ("toc_trailing_digits == 0", trailing == 0),
        ]
        print("-- hygiene gates (audit_selfcheck/measure_build) --")
        for name, ok in gates:
            print(f"  {'OK' if ok else '!!'} {name}")
            gates_ok = gates_ok and ok
        if contexts:
            print(f"-- WARNING: «??» x{len(contexts)} (может быть аутентичным "
                  "текстом автора) --")
            for c in contexts[:5]:
                print(f"  …{c}…")

        import fitz
        doc = fitz.open(str(pdf))
        prev = build / "preview"
        prev.mkdir(exist_ok=True)
        n = len(doc)
        for tag, p in (("first", 0), ("mid", n // 2), ("last", n - 1)):
            doc[p].get_pixmap(dpi=90).save(str(prev / f"p_{tag}.png"))
        doc.close()
        print(f"preview          : {prev}\\p_first|p_mid|p_last.png")
    except ImportError:
        print("?? PyMuPDF не установлен — PDF-метрики и превью пропущены "
              "(pip install pymupdf)")

    sys.exit(0 if gates_ok else 1)


if __name__ == "__main__":
    main()
