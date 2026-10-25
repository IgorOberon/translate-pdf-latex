#!/usr/bin/env python3
"""Sanitize OCR-extracted book markdown before translation/typesetting.

The CLI preserves exact content and whitespace by default and records
replayable coverage. Reviewed exact removals and optional plain-prose blank-line
normalization are supplied through --book-profile. The older clean_text() rule API below remains available
for isolated diagnostics/regressions, not as the publication cleaning policy.

Legacy clean_text() rules (not CLI defaults):
  strip-toc           cut the book's own recognized table of contents: a block
                      of >=5 consecutive lines "text [leaders|spaces] NUMBER"
                      starting in the first --toc-zone share of the document;
                      an adjacent "Оглавление/Содержание/Contents" heading is
                      removed with it. Removed text goes to
                      <out>.removed_toc.md for audit.
  clean-headings      strip trailing dot leaders and a final 1-4 digit page
                      number from ATX headings — ONLY when the number is
                      preceded by leaders or >=2 spaces ("## Глава 4" and
                      years with a single space are left alone).
  strip-running-heads drop short lines repeated verbatim >= --min-repeats
                      times without final punctuation (OCR running heads).
                      Headings, captions (Рисунок/Figure/Таблица/Table),
                      images, math and table rows are never touched.
  attach-eq-tags      a lone "(N)" / "(N.M)" paragraph right AFTER a display
                      formula ($$..$$ or \\[..\\]) is merged into it as
                      \\tag{N} before the closing delimiter; formulas that
                      already carry \\tag are left alone. Otherwise (v2) the
                      lone number is a STATEMENT number: it is glued to the
                      end of the previous non-empty paragraph as " \\hfill(N)"
                      (unless that line is a heading/image/$$-line or N>999).
  normalize-unicode-math  in TEXT mode (outside $...$/$$..$$/\\(..\\)/\\[..\\])
                      replace ⟨ -> \\(\\langle\\), ⟩ -> \\(\\rangle\\),
                      ∘ -> \\(\\circ\\). Every other char outside
                      Latin/Cyrillic/Greek/punctuation is only counted in the
                      report, never touched.
  normalize-blank     squeeze runs of 3+ blank lines to one.

Idempotent: running the tool on its own output changes nothing.
Prints a JSON report with per-rule counters.

Usage:
  python scripts/clean_markdown.py book.md book.clean.md
  python scripts/clean_markdown.py book.md out.md --no-strip-toc --report r.json
"""
import argparse
import difflib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

# в отчёте бывают экзотические юникод-глифы (инвентарь правила f) — не дать
# консоли cp1251 уронить печать
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOC_NAMES = ("Оглавление", "Содержание", "Contents", "Table of Contents",
             "СОДЕРЖАНИЕ", "ОГЛАВЛЕНИЕ", "CONTENTS")

# "text  ....  123" / "text   123" / "text 123" — a TOC entry line
TOC_ENTRY_RE = re.compile(r"^\S.*?(?:\s*\.(?:\s*\.){1,}\s*|\s+)\d{1,4}$")
CAPTION_RE = re.compile(r"^(Рисунок|Рис\.|Figure|Fig\.|Таблица|Table)\b",
                        re.IGNORECASE)
LONE_TAG_RE = re.compile(r"^\((\d+(?:\.\d+)?)\)$")


def is_toc_entry(line):
    s = line.strip()
    if not s or len(s) > 120:
        return False
    if s.startswith(("#", "![", "$", "|", ">")):
        return False
    return bool(TOC_ENTRY_RE.match(s))


def is_toc_heading(line):
    s = line.strip()
    m = re.match(r"^#{1,6}\s+(.*?)\s*$", s)
    text = m.group(1) if m else s
    return text in TOC_NAMES


def strip_toc(lines, zone=0.10, min_entries=5):
    """Remove recognized book-TOC blocks; returns (lines, removed, n_blocks)."""
    zone_end = max(1, int(len(lines) * zone))
    remove = [False] * len(lines)
    removed_blocks = 0
    i = 0
    while i < zone_end:  # a TOC block must START inside the zone
        if is_toc_entry(lines[i]):
            # grow a block: entries + blanks + <=2 consecutive tolerance lines
            j, matches, last_match, tol = i, 0, i, 0
            while j < len(lines):
                if is_toc_entry(lines[j]):
                    matches += 1
                    last_match = j
                    tol = 0
                elif not lines[j].strip():
                    pass  # blank lines are neutral inside a TOC block
                elif (len(lines[j].strip()) < 120 and tol < 2
                      and not lines[j].lstrip().startswith(("#", "![", "$$"))):
                    tol += 1  # wrapped TOC title without its page number
                else:
                    break
                j += 1
            if matches >= min_entries:
                for k in range(i, last_match + 1):
                    remove[k] = True
                removed_blocks += 1
                # take an adjacent TOC heading above along with the block
                k = i - 1
                while k >= 0 and not lines[k].strip():
                    k -= 1
                if k >= 0 and i - k <= 3 and is_toc_heading(lines[k]):
                    for m in range(k, i):
                        remove[m] = True
                i = last_match + 1
                continue
        i += 1
    # collapse the seam: if the block sat between blank lines, absorb the
    # trailing blanks so exactly one blank line remains at the junction
    j = 0
    while j < len(lines):
        if remove[j]:
            a = j
            while j < len(lines) and remove[j]:
                j += 1
            before_blank = a == 0 or (not remove[a - 1]
                                      and not lines[a - 1].strip())
            if before_blank:
                while j < len(lines) and not lines[j].strip():
                    remove[j] = True
                    j += 1
        else:
            j += 1
    removed = [ln for ln, r in zip(lines, remove) if r]
    kept = [ln for ln, r in zip(lines, remove) if not r]
    return kept, removed, removed_blocks


HEAD_NUM_RE = re.compile(
    r"^(#{1,6}\s+.*?)(?:(?:\s*\.){2,}\s*|\s{2,})(\d{1,4})\s*$")
HEAD_LEADER_RE = re.compile(r"^(#{1,6}\s+.*?)\s*\.{4,}\s*$")


def clean_headings(lines):
    out, cleaned = [], []
    for ln in lines:
        if ln.lstrip().startswith("#"):
            m = HEAD_NUM_RE.match(ln.rstrip()) or HEAD_LEADER_RE.match(ln.rstrip())
            if m:
                cleaned.append((ln.rstrip(), m.group(1).rstrip()))
                out.append(m.group(1).rstrip())
                continue
        out.append(ln)
    return out, cleaned


def running_head_candidate(line):
    s = line.strip()
    if not (5 <= len(s) < 60):
        return None
    if s[-1] in ".!?:;,":
        return None
    if s.startswith(("#", "![", "$", "|", ">", "-", "*", "+")):
        return None
    if CAPTION_RE.match(s):
        return None
    if not re.search(r"[^\W\d_]", s):  # must contain a letter
        return None
    return s


def strip_running_heads(lines, min_repeats=8):
    def math_adjacent(i):
        """Prose sandwiched next to display math is real text, not a head."""
        for step in (-1, 1):
            j = i + step
            while 0 <= j < len(lines) and not lines[j].strip():
                j += step
            if 0 <= j < len(lines) and ("$$" in lines[j] or "\\[" in lines[j]
                                        or "\\]" in lines[j]):
                return True
        return False

    occ = [(i, running_head_candidate(ln)) for i, ln in enumerate(lines)]
    valid = [(i, s) for i, s in occ if s and not math_adjacent(i)]
    counts = Counter(s for _, s in valid)
    heads = {s for s, n in counts.items() if n >= min_repeats}
    if not heads:
        return lines, []
    drop = {i for i, s in valid if s in heads}
    out = [ln for i, ln in enumerate(lines) if i not in drop]
    return out, sorted((s, counts[s]) for s in heads)


def attach_eq_tags(lines):
    """Attach a lone (N) paragraph to what it numbers.

    After display math -> \\tag{N} inside the formula (unless it already has
    one). After an ordinary paragraph (Needham numbers worded statements
    too) -> " \\hfill(N)" glued to that paragraph's last line.
    """
    out = list(lines)
    attached_tag, attached_hfill, skipped_tagged = 0, 0, 0
    state = "text"        # "text" | "dollars" | "bracket"
    open_idx = None
    last_block = None     # (open_idx, close_idx, closer) of last closed block
    token_re = re.compile(r"\$\$|\\\[|\\\]")

    def try_hfill(idx, num):
        """Glue " \\hfill(num)" to the previous non-blank line if eligible."""
        j = idx - 1
        while j >= 0 and (out[j] is None or not out[j].strip()):
            j -= 1
        if j < 0:
            return False
        prev = out[j].strip()
        if prev.startswith(("#", "![")) or "$$" in prev:
            return False
        if int(num.split(".")[0]) > 999:  # a year like (1959), not a number
            return False
        out[j] = out[j].rstrip() + " \\hfill(%s)" % num
        for g in range(j + 1, idx + 1):
            out[g] = None
        return True

    for idx, raw in enumerate(out):
        s = raw.strip()
        if state == "text" and s and LONE_TAG_RE.match(s):
            n = LONE_TAG_RE.match(s).group(1)
            if last_block:
                o, c, closer = last_block
                gap = out[c + 1:idx]
                if all(not g.strip() for g in gap):
                    block_text = "\n".join(out[o:c + 1])
                    if "\\tag{" in block_text:
                        skipped_tagged += 1
                    else:
                        line = out[c]
                        pos = line.rfind(closer)
                        out[c] = line[:pos] + "\\tag{%s}" % n + line[pos:]
                        for g in range(c + 1, idx + 1):
                            out[g] = None
                        attached_tag += 1
                last_block = None
                continue
            if try_hfill(idx, n):
                attached_hfill += 1
            continue
        for tok in token_re.findall(raw):
            if state == "text":
                if tok == "$$":
                    state, open_idx = "dollars", idx
                elif tok == "\\[":
                    state, open_idx = "bracket", idx
            elif state == "dollars" and tok == "$$":
                state = "text"
                if raw.rstrip().endswith("$$"):
                    last_block = (open_idx, idx, "$$")
            elif state == "bracket" and tok == "\\]":
                state = "text"
                if raw.rstrip().endswith("\\]"):
                    last_block = (open_idx, idx, "\\]")
        if s and not LONE_TAG_RE.match(s) and state == "text" \
                and not raw.rstrip().endswith(("$$", "\\]")):
            last_block = None  # non-blank text breaks formula->tag adjacency
    return ([ln for ln in out if ln is not None],
            attached_tag, attached_hfill, skipped_tagged)


# NB: wrapped in $...$, not \(...\) — pandoc's markdown reader (without
# tex_math_single_backslash) parses \( as an escaped parenthesis, which
# leaks \circ into text mode and breaks the build.
BIB_HEADINGS = ("Список литературы", "Литература", "References",
                "Bibliography")

INDEX_NAMES = ("Index", "Subject Index", "Name Index", "Указатель",
               "Предметный указатель", "Именной указатель")
# "irrotational field, 476, 479–481" / "Wallis, J., 2, 3, 423" / "Y&Y, Inc., viii"
INDEX_ENTRY_RE = re.compile(
    r"^\s*\S.*,\s*(?:\d{1,4}|[ivxlc]+)(?:[–-](?:\d{1,4}|[ivxlc]+))?"
    r"(?:[,;]\s*(?:\d{1,4}|[ivxlc]+)(?:[–-](?:\d{1,4}|[ivxlc]+))?)*\.?\s*$"
    r"|^\s*\d{1,4}(?:[–-]\d{1,4})?[,.]?\s*$")  # wrapped page-number tail


def is_index_marker(line):
    s = line.strip().strip("#*").strip()
    return s in INDEX_NAMES


def strip_index(lines, zone=0.4, min_entries=8):
    """Cut the book's own back-of-book index (page numbers of the ORIGINAL
    edition are useless in a re-typeset translation). Returns
    (lines, removed, entries)."""
    n = len(lines)
    zone_start = int(n * (1 - zone))
    start = None
    for i in range(zone_start, n):
        if is_index_marker(lines[i]):
            start = i
            break
    if start is None:  # no marker: look for a long run of entry lines
        run = 0
        for i in range(zone_start, n):
            s = lines[i]
            if INDEX_ENTRY_RE.match(s) and len(s.strip()) < 90:
                run += 1
                if run >= min_entries:
                    start = i - run + 1
                    break
            elif s.strip():
                run = 0
        if start is None:
            return lines, [], 0
    j, matches, tol, last = start, 0, 0, start
    while j < n:
        s = lines[j]
        if not s.strip() or is_index_marker(s):
            pass
        elif INDEX_ENTRY_RE.match(s) and len(s.strip()) < 90:
            matches += 1
            tol = 0
            last = j
        elif s.strip().startswith("#"):
            break
        elif len(s.strip()) < 60 and tol < 2:
            tol += 1  # parent entries without numbers ("work"), see-refs
        else:
            break
        j += 1
    if matches < min_entries:
        return lines, [], 0
    removed = lines[start:last + 1]
    kept = lines[:start] + lines[last + 1:]
    while kept and not kept[-1].strip():
        kept.pop()
    return kept, removed, matches


def strip_pagehead_headings(lines):
    """Drop headings that are OCR'd running heads: '## 152 <chapter title>'
    where the number looks like a page number and the text repeats the
    current '# ' chapter heading."""
    out, removed = [], []
    chapter = None
    eat_blank = False
    for ln in lines:
        if eat_blank:
            eat_blank = False
            if not ln.strip() and out and not out[-1].strip():
                continue  # collapse the seam to a single blank line
        if ln.startswith("# ") and not ln.startswith("## "):
            chapter = ln[2:].strip().casefold()
        m = re.match(r"#{2,6}\s+(\d{1,4})\s+(.+?)\s*$", ln)
        if m and chapter and m.group(2).strip().casefold() == chapter:
            removed.append(ln.rstrip())
            eat_blank = True
            continue
        out.append(ln)
    return out, removed


def demote_bib_author_headings(lines):
    """Inside the bibliography (after a 'Список литературы' heading, to the
    end of the chapter) author names became '## ' headings — they pollute
    the TOC and get section numbers. Demote them to bold paragraphs."""
    out, demoted = [], 0
    in_bib = False
    for ln in lines:
        s = ln.strip()
        m = re.match(r"(#{1,6})\s+(.*?)\s*$", s)
        if m and m.group(2) in BIB_HEADINGS:
            in_bib = True
            out.append(ln)
            continue
        if in_bib and ln.startswith("# ") and not ln.startswith("## "):
            in_bib = False  # a new chapter ends the bibliography
        if in_bib and m and len(m.group(1)) >= 2:
            out.append("**%s**" % m.group(2))
            demoted += 1
            continue
        out.append(ln)
    return out, demoted


UNI_MATH_MAP = {"⟨": "$\\langle$", "⟩": "$\\rangle$",
                "∘": "$\\circ$",
                # найдено сборкой Тейлора: глифы в прозе, которых нет в
                # текстовых шрифтах (Missing character) — только базовый LaTeX
                "★": "$\\star$", "♦": "$\\diamondsuit$", "❑": "$\\diamond$",
                "⋅": "$\\cdot$", "≈": "$\\approx$", "∇": "$\\nabla$"}
# греческие SYMBOL-варианты, которые NFKC даёт для плоскости-1
GREEK_SYMBOL_CMD = {"ϵ": "\\epsilon", "ϑ": "\\vartheta", "ϕ": "\\phi",
                    "ϰ": "\\varkappa", "ϱ": "\\varrho", "ϖ": "\\varpi",
                    "∂": "\\partial", "∇": "\\nabla"}


def plane1_math_char(ch):
    """𝛺/𝜖/𝑥 (U+1D400–U+1D7FF) в прозе -> $\\Omega$/$\\epsilon$/$x$.
    Текстовые шрифты этих глифов не имеют (Missing character в сборке).

    pymupdf4llm при записи UTF-8 иногда теряет старший байт plane-1 символа,
    и он падает в диапазон хангыля 0xD400–0xD7FF (U+1D703 THETA -> U+D703).
    Для латинско-кириллического источника хангыль — почти наверняка такой
    повреждённый символ: восстанавливаем +0x10000 и обрабатываем как plane-1."""
    o = ord(ch)
    if 0xD400 <= o <= 0xD7FF:
        ch = chr(o + 0x10000)
        o = ord(ch)
    if not 0x1D400 <= o <= 0x1D7FF:
        return None
    base = unicodedata.normalize("NFKC", ch)
    if len(base) != 1:
        return None
    if base in GREEK_SYMBOL_CMD:
        return "$" + GREEK_SYMBOL_CMD[base] + "$"
    name = unicodedata.name(base, "")
    if "GREEK" in name and "LETTER" in name:
        word = name.split()[-1].lower()
        cmd = word.capitalize() if "CAPITAL" in name else word
        return "$\\" + cmd + "$"
    if base.isascii() and base.isalnum():
        return "$" + base + "$"
    return None
# chars we consider ordinary text: word chars (Latin/Cyrillic/Greek via \w),
# whitespace and common punctuation/typography. Anything else is inventoried.
KNOWN_CHAR_RE = re.compile(
    r"[\w\s.,;:!?()\[\]{}<>\"'«»„“”‘’‚’—–\-…/\\|@#%&*+=~^$№§°·×÷′″†‡©®™]")
INLINE_MATH_RE = re.compile(
    r"\$\$.*?\$\$|(?<!\\)\$(?:\\.|[^$\\])+?\$|\\\(.*?\\\)|\\\[.*?\\\]")


def normalize_unicode_math(lines):
    """Replace ⟨ ⟩ ∘ with inline math outside math spans; inventory the rest."""
    replaced, inventory = Counter(), Counter()
    out = []
    in_display = False

    def process(seg):
        res = []
        for ch in seg:
            if ch in UNI_MATH_MAP:
                replaced[ch] += 1
                res.append(UNI_MATH_MAP[ch])
                continue
            p1 = plane1_math_char(ch)
            if p1 is not None:
                replaced[ch] += 1
                res.append(p1)
                continue
            if not KNOWN_CHAR_RE.match(ch):
                inventory[ch] += 1
            res.append(ch)
        return "".join(res)

    for line in lines:
        if in_display:
            if line.count("$$") % 2 == 1:
                in_display = False
            out.append(line)
            continue
        parts, pos = [], 0
        for m in INLINE_MATH_RE.finditer(line):
            parts.append(process(line[pos:m.start()]))
            parts.append(m.group(0))
            pos = m.end()
        tail = line[pos:]
        if tail.count("$$") % 2 == 1:      # an unpaired $$ opens a block
            cut = tail.rfind("$$")
            parts.append(process(tail[:cut]))
            parts.append(tail[cut:])
            in_display = True
        else:
            parts.append(process(tail))
        out.append("".join(parts))
    return out, replaced, inventory


def normalize_blank(lines):
    out, blanks, squeezed = [], 0, 0
    for ln in lines:
        if ln.strip():
            if blanks:
                out.extend([""] if blanks >= 3 else [""] * blanks)
                if blanks >= 3:
                    squeezed += 1
            blanks = 0
            out.append(ln)
        else:
            blanks += 1
    if blanks:
        out.extend([""] if blanks >= 3 else [""] * blanks)
        if blanks >= 3:
            squeezed += 1
    return out, squeezed


def _paragraphs(lines):
    """[(начало, конец, нормализованный текст)] — абзацы как группы непустых
    строк; конец не включается (срез lines[s:e])."""
    paras, i, n = [], 0, len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        j = i
        while j < n and lines[j].strip():
            j += 1
        paras.append((i, j, " ".join(" ".join(lines[i:j]).split())))
        i = j
    return paras


MATH_SPAN_RE = re.compile(r"\$\$.*?\$\$|\$[^$]*\$", re.S)
DUP_RATIO = 0.90


def dedup_prose(norm):
    """Отпечаток абзаца для сравнения копий: только проза, без математики.
    Две копии одних и тех же страниц из разных окон MinerU распознаются
    НЕЗАВИСИМО, и разметка формул расходится (S против $\\mathcal{S}$),
    хотя проза совпадает буква в букву — сравнивать надо именно её."""
    t = MATH_SPAN_RE.sub(" ", norm)
    t = re.sub(r"[^0-9A-Za-zА-Яа-яЁё ]+", " ", t.lower())
    return " ".join(w for w in t.split() if len(w) >= 3)


def _similar(a, b):
    if a == b:
        return True
    if not a or not b or min(len(a), len(b)) / max(len(a), len(b)) < DUP_RATIO:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= DUP_RATIO


def dedup_repeated_runs(lines, min_paras=4, min_chars=500, anchor_chars=40):
    """Дубли на стыке окон извлечения: гибридный бэкенд MinerU обрабатывает
    документ окнами по 64 страницы и на стыке способен выдать одни и те же
    страницы дважды (боевой случай: раздел 15.2 Тейлора — глава на 71 стр.,
    окон 2). Второе распознавание независимо, поэтому копии не побайтные;
    после перевода они расходятся ещё сильнее (два разных MT-варианта) —
    ловить дубль надо здесь, по совпадению ПРОЗЫ.

    Удаляется вторая копия последовательности из >= min_paras абзацев общей
    длиной >= min_chars, повторяющей более раннюю. Якорь — только абзацы от
    anchor_chars символов прозы: короткие повторы («Доказательство.»,
    однострочные формулы) в учебнике легитимны."""
    paras = [(s, e, norm, dedup_prose(norm)) for s, e, norm in _paragraphs(lines)]
    first = {}
    for k, (_s, _e, _n, pr) in enumerate(paras):
        if len(pr) >= anchor_chars:
            first.setdefault(pr, k)
    drop, runs, k = set(), 0, 0
    while k < len(paras):
        j = first.get(paras[k][3], k)
        if j >= k:                              # первое вхождение — не трогаем
            k += 1
            continue
        run = 0
        while (k + run < len(paras) and j + run < k
               and _similar(paras[j + run][3], paras[k + run][3])):
            run += 1
        chars = sum(len(paras[k + t][3]) for t in range(run))
        if run >= min_paras and chars >= min_chars:
            drop.update(range(k, k + run))
            runs += 1
            k += run
        else:
            k += 1
    if not drop:
        return lines, [], 0
    cut, removed = set(), []
    for t in sorted(drop):
        s, e = paras[t][0], paras[t][1]
        # вместе с абзацем уходит пустая строка-разделитель перед ним:
        # иначе на месте дубля остаётся лишний пробел
        gap = paras[t - 1][1] if t else s
        cut.update(range(gap, e))
        removed.extend(lines[s:e] + [""])
    kept = [ln for i, ln in enumerate(lines) if i not in cut]
    return kept, removed, runs


CJK_RE = re.compile(r"[　-ヿ㐀-䶿一-鿿豈-﫿]")


def strip_cjk_garbage(lines, max_inline=3):
    """OCR of a Latin-script source must not emit CJK: MinerU's vision model
    occasionally hallucinates Chinese, including degenerate repetition loops
    («证明： ... 为 ... 为 ...» x289). A line with more than max_inline CJK
    chars is removed wholesale (to the audit file); scattered lone glyphs are
    deleted in place."""
    out, removed, stripped = [], [], 0
    for ln in lines:
        hits = CJK_RE.findall(ln)
        if len(hits) > max_inline:
            removed.append(ln)
            continue
        if hits:
            ln = CJK_RE.sub("", ln)
            stripped += len(hits)
        out.append(ln)
    return out, removed, stripped


OCR_JUNK_CHARS = "|—=§«»£€¬~_^\\/[]{}<>"
LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")


def strip_ocr_garbage(lines, min_len=15, junk_ratio=0.35, max_letters=10):
    """Строки-мусор от «распознавания» текста ВНУТРИ векторных фигур
    (pymupdf4llm digital: спектры, диаграммы) — высокая плотность рамочно-
    осевых символов при почти полном отсутствии букв, напр.
    «2S§«=| | =| Ac] |e 3 - ———_ os=i == L-=». Удаляются целиком (в аудит).

    Консервативно: только когда мусор ДОМИНИРУЕТ (доля >= junk_ratio) И букв
    <= max_letters. Формулы (`$`), markdown-таблицы (строка с `|` в начале) и
    подписи-с-мусором (много букв) не трогаем — их лучше оставить, чем съесть
    реальный текст; для книг с фигурами штатно советуем --extract auto/math."""
    out, removed = [], []
    for ln in lines:
        s = ln.strip()
        if (len(s) >= min_len and "$" not in ln and s[:1] != "|"
                and len(LETTER_RE.findall(s)) <= max_letters
                and sum(c in OCR_JUNK_CHARS for c in s) / len(s) >= junk_ratio):
            removed.append(ln)
            continue
        out.append(ln)
    return out, removed


TAG_RE = re.compile(r"\\tag\s*\{([^{}]*)\}")
PSEUDO_MATH_RE = re.compile(r"\\\$(.{1,300}?)\\\$", re.S)

# --------------------------------------------------------------------------
# Мусор извлечения MinerU, разобранный на «New Foundations for Classical
# Mechanics» (Hestenes, 718 стр. плотной геометрической алгебры).
#
# Главный здесь — switch+accent. ТЕКСТОВЫЙ переключатель шрифта поверх
# МАТЕМАТИЧЕСКОГО акцента («{\bf \dot{\theta}}^2», «\mathbf{\bar{\alpha}}»)
# роняет XeTeX намертво: «! Internal error: bad native font flag in
# 'map_char_to_glyph'», rc=3, PDF не пишется. Ошибка идёт в STDERR, в
# main.log её НЕТ — лог просто обрывается на текущей странице, и найти место
# можно только бисектом по body.tex. На этой книге таких мест было 49, и
# поиск стоил часов. Лечение: \boldsymbol{\<акцент>{…}}.
# Что именно роняет — выяснено матрицей микротестов на боевой преамбуле, и
# это НЕ «переключатель + акцент» вообще. Рвёт акцент над глифом, которого в
# затребованном шрифте НЕТ:
#     \mathbf{\bar{\alpha}}   КРАШ   строчная греческая в жирном прямом
#     \mathbf{\dot{\cal A}}   КРАШ   каллиграфия
#     \mathrm{\bar{\alpha}}   КРАШ   она же в прямом
#     \mathbf{\bar{x}}        ok     латиница
#     \mathbf{\hat{\Gamma}}   ok     ПРОПИСНАЯ греческая глиф имеет
#     \boldsymbol{\bar{\alpha}} ok   (то есть починка верна)
# Переписываем ВСЕ вхождения — безобидным это не вредит, а угадывать, у
# какого шрифта какой глиф есть, дороже, чем переписать.
FONT_SWITCH_ALT = "|".join((
    "mathbf", "mathsf", "mathtt", "mathit", "mathrm", "textrm", "textbf",
    "texttt", "emph", "pmb", "bf", "rm", "it", "sf", "tt", "sc"))
# «Прямые» переключатели: у них замена на \boldsymbol была бы враньём —
# \mathrm просит ПРЯМОЕ начертание, а не жирное. Для строчной греческой
# прямого глифа не существует в принципе, поэтому честная починка —
# снять переключатель и оставить акцент (проверено: компилируется).
UPRIGHT_SWITCHES = {"mathrm", "textrm", "rm"}
MATH_ACCENT_ALT = "|".join((
    "ddot", "dot", "hat", "tilde", "bar", "vec", "check", "breve", "acute",
    "grave"))

# Форма A: \mathbf{ \bar{x} }, \mathbf{ \vec{F}^{\prime} } — у переключателя
#          своя скобка. Требовать, чтобы акцент был ЕДИНСТВЕННЫМ содержимым,
#          нельзя: в книге есть «\mathbf{\vec{F}^{\prime}}», и ручной проход
#          его как раз пропустил. Достаточно, чтобы акцент внутри был.
# Форма B: {\bf \dot{x}} — переключатель объявлением, без своей скобки.
#
# Альтернация ОБЯЗАНА быть в группе: "\\" + "a|b" привязывает бэкслеш только
# к первой альтернативе — классическая ошибка, стоившая отладки.
SWITCH_OPEN = re.compile(
    r"\\(" + FONT_SWITCH_ALT + r")(?![A-Za-z])\s*\{")
ACCENT_INSIDE = re.compile(r"\\(?:" + MATH_ACCENT_ALT + r")(?![A-Za-z])")
SWITCH_ACCENT_B = re.compile(
    r"\\(" + FONT_SWITCH_ALT + r")(?![A-Za-z])\s*"
    r"\\(" + MATH_ACCENT_ALT + r")(?![A-Za-z])\s*\{")
# то же для прескана: без требования открывающей скобки у акцента
SWITCH_ACCENT_ANY = re.compile(
    r"\\(?:" + FONT_SWITCH_ALT + r")(?![A-Za-z])\s*\{?\s*"
    r"\\(?:" + MATH_ACCENT_ALT + r")(?![A-Za-z])")

CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
HUSH = "\x00"           # заглушка необработанного совпадения, снимается в конце


def _match_brace(s, i):
    """Индекс скобки, закрывающей открывающую в позиции i, или -1."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def fix_switch_accent(text):
    """Переключатель шрифта поверх математического акцента -> \\boldsymbol.

    Совпадение, которое не удалось разобрать (не нашлась парная скобка),
    ГЛУШИТСЯ маркером и цикл идёт дальше: `break` на первом таком месте
    терял бы все последующие, а их в книге десятки.
    """
    n = 0
    # Форма A: переключатель со своей скобкой.
    pos = 0
    while True:
        m = SWITCH_OPEN.search(text, pos)
        if not m:
            break
        sw_open = m.end() - 1
        sw_close = _match_brace(text, sw_open)
        arg = text[sw_open + 1:sw_close] if sw_close != -1 else ""
        # аргумент без акцента переписывать незачем, а с кириллицей —
        # нельзя: «\textbf{жирный текст}» это проза, и \boldsymbol уронит её
        # вернее, чем оставленный \textbf
        if (sw_close != -1 and ACCENT_INSIDE.search(arg)
                and not CYRILLIC_RE.search(arg)):
            repl = (arg if m.group(1) in UPRIGHT_SWITCHES
                    else "\\boldsymbol{" + arg + "}")
            text = text[:m.start()] + repl + text[sw_close + 1:]
            n += 1
            # с того же места: внутри аргумента может сидеть ещё один
            # переключатель («\pmb{\mathrm{\pmb{\hat{\varepsilon}}}}»)
            pos = m.start()
            continue
        # НЕ подходит — просто идём дальше. Прежняя реализация глушила такое
        # совпадение, подменяя первый символ маркером; при обобщённой форме A
        # под это попадал каждый обычный «\mathbf{x}», и у него съедался
        # бэкслеш.
        pos = m.end()

    # Форма B: переключатель объявлением, без своей скобки.
    pos = 0
    while True:
        m = SWITCH_ACCENT_B.search(text, pos)
        if not m:
            break
        acc_open = m.end() - 1
        acc_close = _match_brace(text, acc_open)
        if acc_close == -1:
            pos = m.end()
            continue
        inner = text[acc_open + 1:acc_close]
        text = (text[:m.start()]
                + "\\boldsymbol{\\" + m.group(2) + "{" + inner + "}}"
                + text[acc_close + 1:])
        n += 1
        pos = m.start()
    return text, n


# --- прочий зоопарк MinerU, все примеры из той же книги -------------------

# \overset{△}{<}, \overset{\cdot}{\geqslant} — акцент поверх ЗНАКА ОТНОШЕНИЯ.
# Смысла не несёт (артефакт распознавания), оставляем само отношение.
OVERSET_REL = re.compile(
    r"\\overset\s*\{\s*(?:[^{}]|\{[^{}]*\})*\s*\}\s*"
    r"\{\s*(<|>|=|\\leq|\\geq|\\le|\\ge|\\leqslant|\\geqslant"
    r"|\\ll|\\gg|\\pm|\\mp)\s*\}")
# \overset{\cdot}{\ }, \overset{\cdot}{8} — акцент поверх пустоты или цифры.
# Верх ограничен точкой намеренно: эталонное правило смотрело на ЛЮБОЙ верх и
# тем самым съедало законное «\overset{\rightarrow}{AB}», оставляя «AB».
# Внешние скобки необязательны — в добыче встречаются обе формы.
OVERSET_GARBAGE = re.compile(
    r"(\{\s*)?\\overset\s*\{\s*(?:\\cdot|\\bullet|\\centerdot|\.)\s*\}\s*"
    r"\{\s*((?:[^{}]|\{[^{}]*\})*?)\s*\}(\s*\})?")
MATHBF_OVERSET_DDOTS = re.compile(
    r"\\mathbf\s*\{\s*\\overset\s*\\ddots\s*\}")
# \textbf{\em{X}} и \textbf{\em X} — жирный курсив из добычи; в математике
# это \boldsymbol. Обе формы встречались в одной книге.
TEXTBF_EM_BRACED = re.compile(r"\\textbf\s*\{\s*\\em\s*\{([^{}]*)\}\s*\}")
TEXTBF_EM_BARE = re.compile(
    r"\\textbf\s*\{\s*\\em\s+((?:\\[A-Za-z]+|[^{}\s])[^{}]*?)\s*\}")
# \mathsf{\bar{(2.16)}} — номер уравнения, попавший под бар и под шрифт.
# Пробелы стоят МЕЖДУ ЦИФРАМИ: добыча отдаёт «( 2 . 1 6 )», а не «(2.16)».
MATHSF_BAR_EQNO = re.compile(
    r"\\math(?:sf|bf|rm|it)\s*\{\s*\\bar\s*\{\s*"
    r"\(\s*([\d.\s]+?)\s*\)\s*\}\s*\}")
# rsfs (\mathscr) не имеет СТРОЧНЫХ глифов: \mathscr{r} печатается пустотой
# и даёт «Missing character». Строчные уводим в \mathit.
MATHSCR = re.compile(r"\\mathscr\s*\{([^{}]*)\}")

_LETTER_OR_CMD = re.compile(r"[A-Za-z]|\\[A-Za-z]+")


def repair_math_artifacts(text):
    """Дефекты извлечения, найденные сборкой Тейлора: (1) кратные
    \\tag {N}\\tag{N.MM} у одной формулы — остаётся последний (книжный);
    (2) голый & внутри \\tag{[Eqs. (a) & (b)]} — экранируется; (3) формула,
    у которой доллары экранированы в литеральные \\$...\\$ — возвращается
    в настоящую математику. Всё это иначе доезжает до XeLaTeX и валит сборку."""
    n_tags, n_amp, n_dollar = 0, 0, 0
    prev = None
    while prev != text:                       # \tag{a}\tag{b}\tag{c} -> tag{c}
        prev = text
        text, k = re.subn(r"\\tag\s*\{[^{}]*\}\s*(?=\\tag\s*\{)", "", text)
        n_tags += k

    def _amp(m):
        nonlocal n_amp
        body = m.group(1)
        if "&" in body and r"\&" not in body:
            n_amp += 1
            return "\\tag{" + body.replace("&", r"\&") + "}"
        return m.group(0)
    text = TAG_RE.sub(_amp, text)

    def _dollar(m):
        nonlocal n_dollar
        body = m.group(1)
        stripped = body.strip()
        # только НАСТОЯЩАЯ формула, экранированная извлекателем как \$…\$:
        # обязана содержать команду TeX, не быть валютой (\$10 → знак доллара,
        # а не открытие мат-режима) и либо начинаться с команды/символа, либо
        # быть короткой. Иначе проза со знаком $ («the \$10 billion … \approx»)
        # маскировалась как формула на десятки КБ и не переводилась.
        if not re.search(r"\\[A-Za-z]{2,}", body):
            return m.group(0)
        if not stripped or stripped[0].isdigit():      # валюта: \$5, \$10 …
            return m.group(0)
        if stripped[0] == "\\" or len(body) <= 40:      # начинается с TeX / коротко
            n_dollar += 1
            return "$" + body + "$"
        return m.group(0)
    text = PSEUDO_MATH_RE.sub(_dollar, text)

    counts = {}

    def _sub(rx, repl, key):
        """Считает ЗАМЕНЫ, а не совпадения.

        subn() возвращает число совпадений, а правила, которые часть
        совпадений намеренно оставляют как есть (\\mathscr с прописной
        буквой законен), завышали бы счётчик и создавали впечатление, что
        проход неидемпотентен.
        """
        nonlocal text
        hits = [0]

        def wrapped(m):
            out = repl(m)
            if out != m.group(0):
                hits[0] += 1
            return out
        text = rx.sub(wrapped, text)
        counts[key] = hits[0]

    # Номер уравнения под баром идёт ДО switch+accent: иначе тот увидит
    # «\mathsf{\bar{…}}» как свой случай и выдаст \boldsymbol{\bar{(2.16)}}
    # вместо простого «(2.16)».
    _sub(MATHSF_BAR_EQNO,
         lambda m: "(" + re.sub(r"\s+", "", m.group(1)) + ")",
         "mathsf_bar_eqno")

    text, n_switch = fix_switch_accent(text)

    _sub(OVERSET_REL, lambda m: m.group(1), "overset_rel")

    def _overset_garbage(m):
        body = m.group(2).strip()
        wrapped = bool(m.group(1)) and bool(m.group(3))
        if _LETTER_OR_CMD.fullmatch(body):
            # тело-буква или тело-команда: точка была осмысленным акцентом
            out = "\\dot{" + body + "}"
        elif body in ("", "\\", "\\,", "\\;", "\\quad", "\\qquad"):
            # акцент поверх ПУСТОТЫ. Отдавать «тело как есть» нельзя:
            # у «\overset{\cdot}{\ }» после strip() остаётся голый бэкслеш,
            # и он валит сборку вернее исходного мусора. Пустая ГРУППА, а
            # не пустая строка: «$…$», схлопнувшийся в «$$», для TeX уже
            # открытие выключной формулы.
            out = "{}"
        else:
            out = body
        return "{" + out + "}" if wrapped and out else out
    _sub(OVERSET_GARBAGE, _overset_garbage, "overset_garbage")
    _sub(MATHBF_OVERSET_DDOTS, lambda m: "\\ddots", "mathbf_overset_ddots")
    def _textbf_em(m):
        # «\textbf{\em жирный курсив}» в прозе — не математика: \boldsymbol
        # уронит русский текст вернее, чем оставленный \textbf
        if CYRILLIC_RE.search(m.group(1)):
            return m.group(0)
        return "\\boldsymbol{" + m.group(1).strip() + "}"
    _sub(TEXTBF_EM_BRACED, _textbf_em, "textbf_em")
    _sub(TEXTBF_EM_BARE, _textbf_em, "textbf_em_bare")

    def _mathscr(m):
        arg = m.group(1)
        if re.search(r"[a-z]", arg):
            return "\\mathit{" + arg + "}"
        return m.group(0)
    _sub(MATHSCR, _mathscr, "mathscr_lowercase")

    report = {"extra_tags_removed": n_tags, "tag_amp_escaped": n_amp,
              "pseudo_math_unescaped": n_dollar,
              "switch_accent_fixed": n_switch}
    report.update(counts)
    return text, report


def clean_text(text, opts):
    """Apply enabled rules to markdown text; returns (text, report, removed_toc)."""
    if opts.get("repair_math", True):
        text, rep = repair_math_artifacts(text)
        report_repair = rep
    else:
        report_repair = None
    lines = text.splitlines()
    report = {}
    if report_repair is not None:
        report["repair_math"] = report_repair
    removed_toc = []
    removed_index = []
    removed_cjk = []
    removed_ocr = []
    if opts.get("strip_cjk", True):
        lines, removed_cjk, n_glyphs = strip_cjk_garbage(lines)
        report["strip_cjk"] = {"lines_removed": len(removed_cjk),
                               "lone_glyphs_stripped": n_glyphs}
    if opts.get("strip_ocr_garbage", True):
        lines, removed_ocr = strip_ocr_garbage(lines)
        report["strip_ocr_garbage"] = {"lines_removed": len(removed_ocr)}
    if opts.get("strip_toc", True):
        lines, removed_toc, nb = strip_toc(lines, zone=opts.get("toc_zone", 0.10))
        report["strip_toc"] = {"blocks": nb, "lines_removed": len(removed_toc)}
    if opts.get("strip_index", True):
        lines, removed_index, n_entries = strip_index(lines)
        report["strip_index"] = {"entries": n_entries,
                                 "lines_removed": len(removed_index)}
    if opts.get("strip_running_heads", True):
        lines, heads = strip_running_heads(
            lines, min_repeats=opts.get("min_repeats", 8))
        report["strip_running_heads"] = {
            "distinct": len(heads),
            "heads": [f"{s}  (x{n})" for s, n in heads]}
    if opts.get("clean_headings", True):
        lines, cleaned = clean_headings(lines)
        report["clean_headings"] = {
            "cleaned": len(cleaned),
            "examples": [f"{a!r} -> {b!r}" for a, b in cleaned[:5]]}
    if opts.get("strip_pagehead_headings", True):
        lines, removed_heads = strip_pagehead_headings(lines)
        report["strip_pagehead_headings"] = {
            "removed": len(removed_heads), "lines": removed_heads[:10]}
    if opts.get("demote_bib_headings", True):
        lines, demoted = demote_bib_author_headings(lines)
        report["demote_bib_headings"] = {"demoted": demoted}
    if opts.get("attach_eq_tags", True):
        lines, a_tag, a_hfill, skipped = attach_eq_tags(lines)
        report["attach_eq_tags"] = {"attached_tag": a_tag,
                                    "attached_hfill": a_hfill,
                                    "skipped_existing_tag": skipped}
    if opts.get("normalize_unicode_math", True):
        lines, replaced, inventory = normalize_unicode_math(lines)
        report["normalize_unicode_math"] = {
            "replaced": dict(replaced),
            "inventory": dict(inventory.most_common(30))}
    # дедуп — после нормализации: колонтитулы и page-head-заголовки, которые
    # различались номерами страниц, уже сняты и не рвут повторяющийся ран
    removed_dup = []
    if opts.get("strip_dup_runs", True):
        lines, removed_dup, n_runs = dedup_repeated_runs(
            lines,
            min_paras=opts.get("dup_min_paras", 4),
            min_chars=opts.get("dup_min_chars", 500))
        report["strip_dup_runs"] = {"runs": n_runs,
                                    "lines_removed": len(removed_dup)}
    if opts.get("normalize_blank", True):
        lines, squeezed = normalize_blank(lines)
        report["normalize_blank"] = {"runs_squeezed": squeezed}
    return ("\n".join(lines) + "\n", report, removed_toc, removed_index,
            removed_cjk, removed_dup, removed_ocr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="markdown in")
    ap.add_argument("output", help="markdown out")
    ap.add_argument("--book-profile", help="schema 1 JSON with exact reviewed cleaning decisions")
    ap.add_argument("--no-repair-math", action="store_true")
    ap.add_argument("--no-strip-cjk", action="store_true")
    ap.add_argument("--no-strip-ocr-garbage", action="store_true")
    ap.add_argument("--no-strip-toc", action="store_true")
    ap.add_argument("--no-strip-index", action="store_true")
    ap.add_argument("--no-clean-headings", action="store_true")
    ap.add_argument("--no-strip-running-heads", action="store_true")
    ap.add_argument("--no-strip-pagehead-headings", action="store_true")
    ap.add_argument("--no-demote-bib-headings", action="store_true")
    ap.add_argument("--no-attach-eq-tags", action="store_true")
    ap.add_argument("--no-normalize-unicode-math", action="store_true")
    ap.add_argument("--no-strip-dup-runs", action="store_true")
    ap.add_argument("--no-normalize-blank", action="store_true",
                    help="preserve blank lines even if profile explicitly enables normalization (default: preserve)")
    ap.add_argument("--toc-zone", type=float, default=0.10,
                    help="share of the document where a book TOC may start (0.10)")
    ap.add_argument("--min-repeats", type=int, default=8,
                    help="repetitions for a line to count as a running head (8)")
    ap.add_argument("--dup-min-paras", type=int, default=4,
                    help="paragraphs in a repeated run before it is a "
                         "duplicate, not a legitimate repetition (4)")
    ap.add_argument("--dup-min-chars", type=int, default=500,
                    help="characters in a repeated run before it counts (500)")
    ap.add_argument("--report", default=None, help="write JSON report here")
    a = ap.parse_args()

    from content_coverage import clean_with_coverage, load_book_profile

    src, dst = Path(a.input), Path(a.output)
    report_path = Path(a.report) if a.report else dst.with_suffix(".coverage.json")
    try:
        profile = load_book_profile(a.book_profile)
        if a.no_normalize_blank:
            profile.setdefault("cleaning", {})["normalize_blank"] = False
        report = clean_with_coverage(src, dst, report_path, profile)
    except (ValueError, OSError) as exc:
        print(f"!! cleaning stopped: {exc}", file=sys.stderr)
        return 4
    print(json.dumps({"status": report["status"], "policy": report["policy"],
                      "report": str(report_path),
                      "before": report["before"]["counts"],
                      "after": report["after"]["counts"],
                      "decisions": len(report["decisions"])}, ensure_ascii=False))
    print(f">> written: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
