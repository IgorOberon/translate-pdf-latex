"""Loss-aware MinerU adapter: recover page notes and convert simple HTML tables.

Raw OCR remains immutable. Unsupported/ambiguous structures stop before MT;
never guess a note attachment or quietly flatten a complex table.
"""
import argparse
from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from provenance import atomic_text, sha_file, sha_text, write_json

VERSION = "mineru-structure-v1"
SUP = re.compile(r"<sup>\s*(\d+|[*†‡§¶])\s*</sup>", re.I)
# Books that print their notes with plain marks ("*", "**", a dagger) give
# MinerU the mark unescaped in the footnote body and the same plain mark in
# the running text. The mark is then a page note of the printed page, and one
# note may serve many marks on that page.
PLAIN_LABEL = re.compile(r"^((?:\\\*)+|[†‡§¶])")
PLAIN_MARK = re.compile(r"(?:\\\*)+|[†‡§¶]")
TABLE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
NOTE = re.compile(r"\[\^([^\]\s]+)\](:)?")
# A footnote DEFINITION starts a line in Markdown; "word[^id]: text" is a
# reference followed by the translator's colon, not a definition.
NOTE_DEF = re.compile(r"(?m)^\[\^([^\]\s]+)\]:")


def body_already_in_raw(raw, body):
    """True when the note body is already a line of the raw Markdown.

    A plain substring test would fire on common phrases ("According to some
    authors"), so only a whole line counts as an already attached note.
    """
    for line in raw.splitlines():
        stripped = line.strip()
        stripped = re.sub(r"^(?:>\s*)?", "", stripped)
        stripped = re.sub(r"^(?:(?:\\\*)+|[†‡])\s*", "", stripped)
        if stripped == body:
            return True
    return False


def plain_marks(text):
    """Plain note marks (not wrapped in <sup>) with their label."""
    out = []
    for m in PLAIN_MARK.finditer(text):
        mark = m.group(0)
        if mark.startswith("\\"):
            out.append(("*" * mark.count("\\*"), m))
        else:
            out.append((mark, m))
    return out


def plain_refs(text, label):
    """Positions of a plain note mark (not wrapped in <sup>) for one label."""
    return [m for lab, m in plain_marks(text) if lab == label]


class SimpleTable(HTMLParser):
    """Deliberately bounded subset, not a general HTML-to-Markdown converter.

    Merged cells (rowspan/colspan) are expanded into a rectangular grid: the
    text of the merged cell stays in its top-left position, the covered
    positions become empty cells. No cell text is dropped or duplicated.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell, self.stack = [], None, None, []
        self.span = (1, 1)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            if self.cell is None:
                raise ValueError("unsupported table markup <img>: review extraction")
            src = dict(attrs).get("src", "")
            if not src:
                raise ValueError("image without source in table cell: review extraction")
            self.cell.append(f"![]({src})")
            return
        if tag not in {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}:
            raise ValueError(f"unsupported table markup <{tag}>: review extraction")
        attrs = dict(attrs)
        parent = self.stack[-1] if self.stack else None
        allowed = {"table": {None}, "thead": {"table"}, "tbody": {"table"},
                   "tfoot": {"table"}, "tr": {"table", "thead", "tbody", "tfoot"},
                   "td": {"tr"}, "th": {"tr"}}
        if parent not in allowed[tag]:
            raise ValueError("nested/malformed HTML table")
        self.stack.append(tag)
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = []
            try:
                rowspan = max(1, int(attrs.get("rowspan", "1")))
                colspan = max(1, int(attrs.get("colspan", "1")))
            except ValueError:
                raise ValueError("invalid rowspan/colspan: review extraction")
            self.span = (rowspan, colspan)

    def handle_startendtag(self, tag, attrs):
        if tag == "img":
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("unbalanced HTML table")
        if tag in {"td", "th"}:
            value = " ".join("".join(self.cell).split())
            if "\n" in value or re.search(r"<[a-zA-Z/!][^>]*>", value):
                raise ValueError("ambiguous table cell: explicit normalization required")
            # A pipe inside a cell would split the Markdown grid: escape it.
            value = value.replace("|", r"\|")
            self.row.append((value, self.span[0], self.span[1]))
            self.cell = None
            self.span = (1, 1)
        elif tag == "tr":
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        elif data.strip():
            raise ValueError("text outside table cells")

    def grid(self):
        """Rows of cells with rowspan/colspan expanded."""
        values = []
        for index, row in enumerate(self.rows):
            while len(values) <= index:
                values.append([])
            line = values[index]
            col = 0
            for text, rowspan, colspan in row:
                while col < len(line) and line[col] is not None:
                    col += 1
                for r in range(rowspan):
                    while len(values) <= index + r:
                        values.append([])
                    target = values[index + r]
                    while len(target) < col + colspan:
                        target.append(None)
                    for c in range(colspan):
                        target[col + c] = text if (r == 0 and c == 0) else ""
                col += colspan
        return values

    def markdown(self):
        if self.stack or not self.rows or not self.rows[0]:
            raise ValueError("empty/incomplete HTML table")
        grid = self.grid()
        width = max(len(row) for row in grid)
        rows = [["" if cell is None else cell for cell in row] + [""] * (width - len(row))
                for row in grid]
        out = [rows[0], ["---"] * width, *rows[1:]]
        return "\n".join("| " + " | ".join(row) + " |" for row in out)


def pipe_tables(text):
    """Ordered cell grids (excluding Markdown header separators)."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    tables, rows = [], []
    for line in text.splitlines() + [""]:
        if line.strip().startswith("|") and line.strip().endswith("|"):
            rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]])
        else:
            if len(rows) >= 2 and all(re.fullmatch(r":?-{3,}:?", c) for c in rows[1]):
                tables.append([rows[0], *rows[2:]])
            rows = []
    return tables


def table_signature(text):
    # Numeric and masked cells must stay in their original row/column, not
    # merely retain the same document-wide digit count.
    return [[[re.findall(r"\d+|⟦M\d{6}⟧", c) for c in row] for row in t]
            for t in pipe_tables(text)]


def note_parts(text):
    """(references, definitions) with definitions only at the start of a line."""
    defs = Counter(NOTE_DEF.findall(text))
    refs = Counter(re.findall(r"\[\^([^\]\s]+)\]", NOTE_DEF.sub("", text)))
    return refs, defs


def assert_markdown_structure(source, target):
    if TABLE.search(target):
        raise ValueError("raw HTML table would lose structure in LaTeX")
    if note_parts(source) != note_parts(target):
        raise ValueError("footnote IDs/definitions changed during preparation or translation")
    if table_signature(source) != table_signature(target):
        raise ValueError("table rows/columns/numeric cells changed")


def assert_typeset_structure(markdown, tex):
    definitions = set(NOTE_DEF.findall(markdown))
    refs = Counter(re.findall(r"\[\^([^\]\s]+)\]", NOTE_DEF.sub("", markdown)))
    if set(refs) != definitions:
        raise ValueError("unresolved/orphan Markdown footnotes before typesetting")
    expected_notes = sum(refs.values())
    if expected_notes and len(re.findall(r"\\footnote\s*\{", tex)) < expected_notes:
        raise ValueError("footnotes lost during Pandoc conversion")
    if len(re.findall(r"\\begin\{(?:longtable|tabular|tabularx)\}", tex)) < len(pipe_tables(markdown)):
        raise ValueError("table structure lost during Pandoc conversion")


def normalize(raw, content, namespace):
    notes, text_items = {}, []
    for item in content:
        kind, page, text = item.get("type"), item.get("page_idx"), item.get("text", "")
        if kind == "page_footnote":
            if not isinstance(page, int):
                raise ValueError("page footnote has no unambiguous label/page")
            body_text = text.strip()
            match = SUP.match(body_text)
            if match:
                label, style = match.group(1), "sup"
                body = body_text[match.end():].strip()
            else:
                plain = PLAIN_LABEL.match(body_text)
                if not plain:
                    raise ValueError("page footnote has no unambiguous label/page")
                mark = plain.group(1)
                label = ("*" * mark.count("\\*")) if mark.startswith("\\") else mark
                style, body = "plain", body_text[plain.end():].strip()
            key = (page, label)
            if key in notes or not body or "\n" in body or SUP.search(body):
                raise ValueError("duplicate, multiline or ambiguous page footnote: review JSON")
            # Hash avoids collisions when page numbering restarts in chunks.
            ident = f"mu-{namespace}-p{page + 1}-n{len(notes) + 1}"
            notes[key] = {"id": ident, "body": body, "page_idx": page,
                          "label": label, "style": style}
        elif kind == "text":
            text_items.append(item)
    # Digit superscripts left over: a lost footnote reference or notation
    # (exponent, charge). A page that carries notes is treated strictly.
    allowed_sup = 0
    for item in text_items:
        page = item.get("page_idx")
        text = item.get("text", "")
        page_labels = {n["label"] for n in notes.values() if n["page_idx"] == page}
        page_notes = [n for n in notes.values() if n["page_idx"] == page]
        for m in SUP.finditer(text):
            label = m.group(1)
            if not label.isdigit() or label in page_labels:
                continue
            before = text[:m.start()].rstrip()
            if not page_notes and before and (before[-1].isalnum() or before[-1] == "/"):
                allowed_sup += 1     # exponent or charge: notation, not a note
                continue
            raise ValueError("unresolved numeric superscript: missing note data "
                             "or exponent; review before MT")
    if not text_items:
        # Merged book: the parts were prepared already and there is no content
        # list. Only notation (exponent, charge) may stay unresolved.
        for m in SUP.finditer(raw):
            if not m.group(1).isdigit():
                continue
            before = raw[:m.start()].rstrip()
            if before and (before[-1].isalnum() or before[-1] == "/"):
                allowed_sup += 1
                continue
            raise ValueError("unresolved numeric superscript: missing note data "
                             "or exponent; review before MT")
    edits, recovered, page_notes, unmatched, already = [], [], [], [], []

    def anchor_of(item):
        """Raw offset of a text item; the anchor must be unique in the raw text."""
        anchor = item["text"]
        if raw.count(anchor) != 1:
            raise ValueError("footnote anchor not unique in raw Markdown; review extraction")
        return raw.index(anchor), anchor

    def page_anchor(page):
        """Nearest text item that can carry a note of a page without marks.

        A printed note whose mark sits inside a figure has no text reference.
        The note is then kept next to the closest text: the page itself, the
        following page, or the preceding one.
        """
        def usable(item):
            text = item.get("text", "")
            return bool(text.strip()) and raw.count(text) == 1

        same = [it for it in text_items if it.get("page_idx") == page]
        later = [it for it in text_items if (it.get("page_idx") or 0) > page]
        earlier = [it for it in text_items if (it.get("page_idx") or 0) < page]
        for group in (reversed(same), later, reversed(earlier)):
            for item in group:
                if usable(item):
                    return item
        return None

    for key, note in notes.items():
        page, label, style = key[0], note["label"], note["style"]
        if style == "sup":
            candidates = [(item, m) for item in text_items if item.get("page_idx") == page
                          for m in SUP.finditer(item.get("text", "")) if m.group(1) == label]
            if len(candidates) != 1:
                page_text = [it for it in text_items if it.get("page_idx") == page
                             and it.get("text", "").strip()]
                if candidates or page_text:
                    raise ValueError(f"note page {page + 1}, label {label}: "
                                     "reference is missing/ambiguous")
                # A figure-only page: the note is kept next to the closest text.
                candidates = []
        else:
            candidates = [(item, m) for item in text_items if item.get("page_idx") == page
                          for m in plain_refs(item.get("text", ""), label)]
        if body_already_in_raw(raw, note["body"]):
            if style == "sup":
                raise ValueError("footnote body already in raw Markdown: review duplicate/attachment")
            # A printed page note is already a line of the Markdown: keep it
            # where the exporter put it instead of copying it as a definition.
            already.append({"page_idx": page, "label": label,
                            "body_sha256": sha_text(note["body"])})
            continue
        if not candidates:
            # The printed mark sits inside a figure: no text anchor to attach to.
            # Keep the note as a page note instead of dropping it.
            anchor_item = page_anchor(page)
            if anchor_item is None:
                raise ValueError(f"note page {page + 1}, label {label}: "
                                 "no reference and no anchor for a page note")
            start, anchor = anchor_of(anchor_item)
            mark = label.replace("*", r"\*")
            edits.append((start + len(anchor), start + len(anchor),
                          f"\n\n> {mark} {note['body']}"))
            page_notes.append({"page_idx": page, "label": label,
                               "anchor_page_idx": anchor_item.get("page_idx"),
                               "body_sha256": sha_text(note["body"])})
            continue
        placed = []
        for item, match in candidates:
            start, anchor = anchor_of(item)
            edits.append((start + match.start(), start + match.end(), f"[^{note['id']}]"))
            placed.append((start, item, match))
        # Definitions next to their paragraph survive skeleton section splitting.
        last_start, last_item, _ = max(placed, key=lambda p: p[0])
        start, anchor = anchor_of(last_item)
        edits.append((start + len(anchor), start + len(anchor),
                      f"\n\n[^{note['id']}]: {note['body']}"))
        recovered.append({k: v for k, v in note.items() if k != "body"} |
                         {"body_sha256": sha_text(note["body"]),
                          "references": len(candidates)})
    # Marks without a note of their own: MinerU may have lost the body.
    # Reported, never silently accepted.
    seen = set()
    for item in text_items:
        page = item.get("page_idx")
        for label, _m in plain_marks(item.get("text", "")):
            if (page, label) not in notes and (page, label) not in seen:
                seen.add((page, label))
                unmatched.append({"page_idx": page, "label": label})
    text = raw
    for start, end, replacement in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        text = text[:start] + replacement + text[end:]
    if len([m for m in SUP.finditer(text) if m.group(1).isdigit()]) > allowed_sup:
        raise ValueError("unresolved numeric superscript: missing note data or exponent; review before MT")
    count = 0

    def table(match):
        nonlocal count
        parser = SimpleTable()
        parser.feed(match[0])
        parser.close()
        out = parser.markdown()
        count += 1
        return out

    text = TABLE.sub(table, text)
    if re.search(r"</?(?:table|tr|td|th)\b", text, re.I):
        raise ValueError("incomplete/unsupported HTML table remains")
    return text, {"footnotes": recovered, "tables_converted": count,
                  "page_notes": page_notes, "notes_already_present": already,
                  "unmatched_marks": unmatched}


def prepare(md):
    """Always derive from raw bytes, never modify MinerU output or trust mtime."""
    md = Path(md)
    sidecar = md.with_name(md.stem + "_content_list.json")
    dst = md.with_suffix(".prepared.md")
    report_path = md.with_suffix(".prepare.json")
    raw = md.read_text(encoding="utf-8")
    report = {"schema": 1, "version": VERSION, "status": "failed",
              "source_sha256": sha_file(md),
              "content_list_sha256": sha_file(sidecar) if sidecar.exists() else None}
    try:
        content = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else []
        if not isinstance(content, list):
            raise ValueError("unsupported MinerU content-list schema")
        text, details = normalize(raw, content, report["source_sha256"][:12])
        atomic_text(dst, text)
        report.update(details, status="passed", output_sha256=sha_file(dst))
        write_json(report_path, report)
        print(f">> MinerU structure: {len(details['footnotes'])} notes, "
              f"{details['tables_converted']} HTML tables -> {dst}", flush=True)
        return dst
    except (ValueError, KeyError, TypeError) as exc:
        report["error"] = str(exc)
        write_json(report_path, report)
        raise ValueError(f"MinerU structure not accepted: {exc}; see {report_path}") from exc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("markdown", type=Path)
    prepare(ap.parse_args().markdown)
