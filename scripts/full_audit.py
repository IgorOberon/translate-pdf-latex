#!/usr/bin/env python3
"""Full typesetting audit of a built book (machine layer, m1-m8).

Writes a markdown report and PNG evidence for every flagged page.
  m1 hygiene gates (reused from audit_selfcheck / measure_build / layout_report)
  m2 overfull: full main.log parse, top-20 with pages, each rendered
  m3 geometry: per-page content bbox vs the median text frame (>4pt overflow)
  m4 voids: pages filled <25% (chapter ends marked separately) and
     vertical gaps >90pt inside a page
  m5 caption integrity for ALL figures: caption on the image's page, gap <20pt
  m6 duplicate adjacent paragraphs (normalized, >60 chars) in the source md
  m7 hyphenation garbage: 'word- continuation' with a space, list only
  m8 bibliography/index: page range, short-line stats

Usage:
  python scripts/full_audit.py <build_dir> <book.ru.md>
                               [--out fixtures/full_audit] [--dpi 90]
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from tex_logs import read_tex_log
from statistics import median

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print(">> full_audit.py v1", flush=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_build import toc_trailing_digits, pdf_metrics   # noqa: E402
from audit_selfcheck import audit_pdf                        # noqa: E402
from layout_report import diagnose_font, scan_log            # noqa: E402


def render(doc, pno, outdir, tag, dpi):
    f = outdir / f"p{pno + 1:03d}_{tag}.png"
    if not f.exists():
        doc[pno].get_pixmap(dpi=dpi).save(str(f))
    return f.name


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("build_dir")
    ap.add_argument("md")
    ap.add_argument("--out", default="fixtures/full_audit")
    ap.add_argument("--dpi", type=int, default=90)
    a = ap.parse_args()
    build = Path(a.build_dir).resolve()
    out = Path(a.out).resolve()
    pages_dir = out / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    import fitz
    doc = fitz.open(str(build / "main.pdf"))
    n = len(doc)
    L = ["# Полный аудит вёрстки — машинный слой", "",
         f"- сборка: {build}\\main.pdf, страниц {n}", ""]

    # ---------- m1: gates ----------
    hard, contexts = audit_pdf(build / "main.pdf")
    met = pdf_metrics(build / "main.pdf")
    trailing = toc_trailing_digits((build / "main.toc").read_text(
        encoding="utf-8", errors="replace"))
    gates = [("mask tokens M######", hard["mask tokens M######"], 0),
             ("«⟦»", hard["«⟦»"], 0),
             ("literal \\real{", hard["literal \\real{"], 0),
             ("literal pandocbounded", hard["literal pandocbounded"], 0),
             ("literal \\tag", hard["literal \\tag"], 0),
             ("literal \\hfill", hard["literal \\hfill"], 0),
             ("[рисунок не найден]", hard["[рисунок не найден]"], 0),
             ("duplicate_toc", met["duplicate_toc"], 1),
             ("toc_trailing_digits", trailing, 0)]
    L += ["## m1. Гейты гигиены", ""]
    m1_bad = 0
    for name, val, want in gates:
        ok = val == want
        m1_bad += not ok
        L.append(f"- {'OK' if ok else '!!'} {name} = {val} (норма {want})")
    L += [f"- WARNING «??»: {len(contexts)} контекст(ов)", ""]

    # ---------- m2: overfull ----------
    log_text = read_tex_log(build / "main.log")
    pages_cnt, overfulls, underfull, missing, by_font = scan_log(log_text)
    top = sorted(overfulls, key=lambda o: -o["pt"])[:20]
    L += ["## m2. Overfull (полный лог)", "",
          f"всего: {len(overfulls)}, underfull: {underfull}; топ-20:", ""]
    for o in top:
        p = o.get("page")
        png = render(doc, p - 1, pages_dir, "overfull", a.dpi) if p else "—"
        L.append(f"- {o['pt']:.1f}pt, стр. {p or '?'} → {png}")
    L.append("")

    # ---------- m2b: пропавшие глифы, разбор по шрифтам ----------
    # Диагноз даёт шрифт, а не список символов: кириллица в cmmi8 — это
    # русский текст, затянутый внутрь $…$; латиница в rsfs — \mathscr со
    # строчной буквой.
    L += ["## m2b. Пропавшие глифы (по шрифтам)", "",
          f"всего: {sum(missing.values())}", ""]
    for font, chars in sorted(by_font.items(),
                              key=lambda kv: -sum(kv[1].values())):
        top_ch = ", ".join(f"{c}×{k}" for c, k in chars.most_common(8))
        L.append(f"- **{font}**: {sum(chars.values())} ({top_ch})")
        L.append(f"  - {diagnose_font(font, chars)}")
    if not by_font:
        L.append("- пропавших глифов нет")
    L.append("")

    # ---------- m3: geometry ----------
    boxes = []
    for i in range(n):
        bb = None
        for b in doc[i].get_text("dict")["blocks"]:
            r = fitz.Rect(b["bbox"])
            bb = r if bb is None else bb | r
        boxes.append(bb)
    lefts = [b.x0 for b in boxes if b]
    rights = [b.x1 for b in boxes if b]
    ml, mr = median(lefts), median(rights)
    m3 = []
    for i, b in enumerate(boxes):
        if not b:
            continue
        over = max(ml - b.x0, b.x1 - mr)
        if over > 4:
            m3.append((i + 1, round(over, 1)))
    L += ["## m3. Геометрия (рамка по медиане: "
          f"x {ml:.1f}..{mr:.1f}pt; порог 4pt)", "",
          f"выходов за поля: {len(m3)}", ""]
    for p, over in m3[:40]:
        png = render(doc, p - 1, pages_dir, "geom", a.dpi)
        L.append(f"- стр. {p}: выход {over}pt → {png}")
    L.append("")

    # ---------- m4: voids ----------
    m4_empty, m4_gaps = [], []
    page_area_cache = {}
    for i in range(n):
        page = doc[i]
        blocks = [fitz.Rect(b["bbox"]) for b in page.get_text("dict")["blocks"]]
        area = sum(r.get_area() for r in blocks)
        frame = (mr - ml) * (page.rect.height * 0.82)
        fill = area / frame if frame else 1
        if fill < 0.25:
            nxt_chapter = False
            if i + 1 < n:
                t = doc[i + 1].get_text()[:200]
                nxt_chapter = bool(re.match(r"\s*Глава \d+", t))
            m4_empty.append((i + 1, round(100 * fill), nxt_chapter))
        rows = sorted(blocks, key=lambda r: r.y0)
        for r1, r2 in zip(rows, rows[1:]):
            gap = r2.y0 - r1.y1
            if gap > 90:
                m4_gaps.append((i + 1, round(gap)))
                break
    L += ["## m4. Пустоты", "",
          f"страниц с заполнением <25%: {len(m4_empty)} "
          f"(из них перед новой главой: {sum(1 for _, _, c in m4_empty if c)})", ""]
    for p, fill, chap in m4_empty[:40]:
        png = render(doc, p - 1, pages_dir, "empty", a.dpi)
        L.append(f"- стр. {p}: {fill}%{' (конец главы)' if chap else ''} → {png}")
    L += ["", f"вертикальные разрывы >90pt: {len(m4_gaps)}", ""]
    for p, gap in m4_gaps[:40]:
        png = render(doc, p - 1, pages_dir, "gap", a.dpi)
        L.append(f"- стр. {p}: разрыв {gap}pt → {png}")
    L.append("")

    # ---------- m5: caption integrity (all) ----------
    cap_total, violations = 0, []
    for i in range(n):
        page = doc[i]
        d = page.get_text("dict")
        imgs = [fitz.Rect(b["bbox"]) for b in d["blocks"] if b["type"] == 1]
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            first = "".join(s["text"] for s in b["lines"][0]["spans"]).strip() \
                if b["lines"] else ""
            if not first.startswith("Рисунок ["):
                continue
            r = fitz.Rect(b["bbox"])
            best = None
            for ir in imgs:
                gap = r.y0 - ir.y1
                xov = min(r.x1, ir.x1) - max(r.x0, ir.x0)
                if xov > 0 and -5 <= gap < 1000:
                    best = gap if best is None else min(best, gap)
            cap_total += 1
            if best is None or best >= 20:
                violations.append((i + 1, first[:40],
                                   None if best is None else round(best, 1)))
    L += ["## m5. Целостность подписей (все фигуры)", "",
          f"подписей найдено: {cap_total}; нарушений (нет картинки сверху "
          f"или зазор ≥20pt): {len(violations)}", ""]
    for p, cap, gap in violations[:40]:
        png = render(doc, p - 1, pages_dir, "caption", a.dpi)
        L.append(f"- стр. {p}: «{cap}» gap={gap} → {png}")
    L.append("")

    # ---------- m6: duplicate paragraphs (md) ----------
    md_text = Path(a.md).read_text(encoding="utf-8")
    paras = [" ".join(p.split()).casefold()
             for p in re.split(r"\n\s*\n", md_text)]
    m6 = []
    for k, (p1, p2) in enumerate(zip(paras, paras[1:])):
        if len(p1) > 60 and p1 == p2:
            m6.append((k, p1[:70]))
    L += ["## m6. Дубли соседних абзацев (md, >60 симв.)", "",
          f"найдено пар: {len(m6)}", ""]
    L += [f"- #{k}: «{s}…»" for k, s in m6[:20]] + [""]

    # ---------- m7: hyphenation garbage ----------
    full_text = "\n".join(doc[i].get_text() for i in range(n))
    m7 = Counter(m.group(0) for m in re.finditer(
        r"\b[а-яё]{2,}- [а-яё]{2,}\b", full_text))
    L += ["## m7. Мусор переносов («слово- продолжение», только список)", "",
          f"уникальных: {len(m7)}, всего вхождений: {sum(m7.values())}", ""]
    L += [f"- {s} (x{c})" for s, c in m7.most_common(25)] + [""]

    # ---------- m8: bibliography / index ----------
    bib_start = None
    for i in range(n - 1, max(0, n - 120), -1):
        if "Список литературы" in doc[i].get_text():
            bib_start = i + 1
    short_lines = 0
    if bib_start:
        for i in range(bib_start - 1, n):
            for ln in doc[i].get_text().splitlines():
                s = ln.strip()
                if 0 < len(s) < 15 and not s.isdigit():
                    short_lines += 1
        render(doc, bib_start - 1, pages_dir, "bib", a.dpi)
        render(doc, n - 1, pages_dir, "bib", a.dpi)
    L += ["## m8. Библиография/указатель", "",
          f"- начало: стр. {bib_start or 'не найдено'}; конец книги: стр. {n}",
          f"- коротких (<15 симв.) строк в зоне: {short_lines}",
          f"- рендеры: p{bib_start:03d}_bib.png, p{n:03d}_bib.png"
          if bib_start else "- рендеры: —", ""]

    # ---------- summary ----------
    L += ["## Сводка машинного слоя", "",
          f"| слой | найдено |", "|---|---|",
          f"| m1 гейты | {m1_bad} провалов |",
          f"| m2 overfull | {len(overfulls)} (топ-20 отрендерены) |",
          f"| m3 геометрия >4pt | {len(m3)} |",
          f"| m4 пустоты <25% / разрывы >90pt | {len(m4_empty)} / {len(m4_gaps)} |",
          f"| m5 подписи | {len(violations)} нарушений из {cap_total} |",
          f"| m6 дубли абзацев | {len(m6)} |",
          f"| m7 мусор переносов | {len(m7)} уникальных |", ""]
    (out / "machine.md").write_text("\n".join(L), encoding="utf-8")
    print(f">> report: {out}\\machine.md; evidence: {pages_dir} "
          f"({len(list(pages_dir.glob('*.png')))} PNG)")
    doc.close()


if __name__ == "__main__":
    main()
