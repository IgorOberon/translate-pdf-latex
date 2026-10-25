"""Versioned, lossless paragraph envelopes for grouped translation requests.

Outer groups keep their historical indices (including keep-source decisions).
Request envelopes and coverage receipts identify paragraphs before grouping.
No model/network/file operations are performed here.
"""
import hashlib
import re

from request_plan import plan_fragments

PROTOCOL = "paragraph-ids-v1"
MARKER = re.compile(r"⟦(/?)(P\d{8}_[0-9a-f]{12}_F\d{6})⟧")
RULE = ("\nPreserve every paragraph envelope exactly: each opening ⟦P...⟧ and "
        "matching closing ⟦/P...⟧ must remain on its own line, in the same order. "
        "Translate only its enclosed text. Never omit, combine, repeat or move "
        "paragraphs, and output no text outside the envelopes.")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_paragraphs(blocks):
    """Position disambiguates repeated text without relying on request groups."""
    rows = []
    for index, text in enumerate(blocks, 1):
        if MARKER.search(text):
            raise ValueError("source contains reserved paragraph protocol markers")
        hash_ = digest(text)
        rows.append({"id": f"P{index:08d}_{hash_[:12]}", "source_index": index,
                     "source_sha256": hash_, "text": text})
    return rows


def assign_groups(groups, paragraphs):
    """Reproduce historical outer groups; never infer identity from equal text."""
    result, cursor = [], 0
    for group in groups:
        members, joined = [], ""
        while cursor < len(paragraphs):
            row = paragraphs[cursor]
            joined += ("\n\n" if members else "") + row["text"]
            members.append(row)
            cursor += 1
            if joined == group:
                break
            if not group.startswith(joined):
                raise ValueError("paragraph grouping does not reproduce source")
        if joined != group:
            raise ValueError("paragraph grouping is incomplete")
        result.append(members)
    if cursor != len(paragraphs):
        raise ValueError("paragraph grouping has an uncovered source tail")
    return result


def is_barrier(text):
    # A leading masked token may be a MinerU note-definition marker. Since the
    # mapping is not available here, conservatively isolate all such paragraphs.
    return bool(re.search(r"(?m)^\s*(?:#{1,6}\s|\||\[\^|⟦M\d{6}⟧|!\[|"
                          r">|[-*+]\s|\d+[.)]\s|```|~~~|§|"
                          r"(?:Zusatz|Anmerkung)\b)", text))


def encode(parts, bodies=None):
    bodies = bodies if bodies is not None else [p["text"] for p in parts]
    return "\n\n".join(f"⟦{p['fragment_id']}⟧\n{body.strip()}\n⟦/{p['fragment_id']}⟧"
                         for p, body in zip(parts, bodies))


def parse_response(text, parts):
    """Strict framing: exact count/order/IDs, nonempty bodies, no outside text."""
    if not isinstance(text, str):
        raise ValueError("paragraph reply is not text")
    tokens = list(MARKER.finditer(text))
    expected = [(slash, p["fragment_id"]) for p in parts for slash in ("", "/")]
    if [(m[1], m[2]) for m in tokens] != expected:
        raise ValueError("paragraph IDs/count/order changed")
    bodies, cursor = [], 0
    for opening, closing in zip(tokens[::2], tokens[1::2]):
        if text[cursor:opening.start()].strip():
            raise ValueError("text outside paragraph envelope")
        if (opening.start() and text[opening.start() - 1] != "\n"
                or text[opening.end():opening.end() + 1] != "\n"
                or text[closing.start() - 1:closing.start()] != "\n"
                or closing.end() < len(text) and text[closing.end()] != "\n"):
            raise ValueError("paragraph markers must occupy separate lines")
        body = text[opening.end():closing.start()].strip()
        if not body:
            raise ValueError("empty paragraph body")
        if "⟦P" in body or "⟦/P" in body:
            raise ValueError("unknown or damaged paragraph marker")
        bodies.append(body)
        cursor = closing.end()
    if text[cursor:].strip():
        raise ValueError("text outside paragraph envelope")
    return bodies


def plan_group(members, split_chars=0):
    """Pack adjacent prose only; split long paragraphs without losing ownership."""
    requests, pending, size = [], [], 0

    def flush():
        nonlocal pending, size
        if pending:
            requests.append({"text": encode(pending), "paragraphs": pending,
                             "boundary": "paragraph", "oversized": any(p["oversized"] for p in pending)})
            pending, size = [], 0

    for row in members:
        fragments = plan_fragments(row["text"], split_chars)
        barrier = is_barrier(row["text"]) or len(fragments) > 1
        if barrier:
            flush()
        for index, fragment in enumerate(fragments, 1):
            part = dict(fragment, id=row["id"], source_index=row["source_index"],
                        source_sha256=row["source_sha256"],
                        fragment_id=f"{row['id']}_F{index:06d}", fragment_index=index)
            if pending and split_chars and size + len(fragment["text"]) + 2 > split_chars:
                flush()
            pending.append(part)
            size += len(fragment["text"]) + 2
            if barrier:
                flush()
    flush()
    return requests


def render_paragraph(text):
    """Retain a source paragraph, preserving Markdown note-definition syntax."""
    return re.sub(r"\n(\[\^[^\]\s]+\]:)", r"\n\n\1",
                  re.sub(r"\n\s*\n+", "\n", text.strip()))


def coverage_rows(group_members, rendered_groups):
    """Bind each original paragraph to its exact published output span."""
    rows, offset = [], 0
    for group_index, (members, bodies) in enumerate(zip(group_members, rendered_groups), 1):
        if len(members) != len(bodies):
            raise ValueError("paragraph output coverage mismatch")
        for source, body in zip(members, bodies):
            rows.append({k: source[k] for k in ("id", "source_index", "source_sha256")}
                        | {"group_index": group_index, "output_start": offset,
                           "output_end": offset + len(body), "output_sha256": digest(body)})
            offset += len(body) + 2
    return rows


def verify_coverage(source_blocks, grouped_blocks, raw_output, rows):
    expected = source_paragraphs(source_blocks)
    members = assign_groups(grouped_blocks, expected)
    grouped = [(group_index, p) for group_index, group in enumerate(members, 1) for p in group]
    if not isinstance(rows, list) or len(rows) != len(grouped):
        raise ValueError("missing paragraph coverage")
    cursor = 0
    for row, (group_index, source) in zip(rows, grouped):
        if any(row.get(k) != source[k] for k in ("id", "source_index", "source_sha256")):
            raise ValueError("paragraph coverage identity/order mismatch")
        end = row.get("output_end")
        if (row.get("group_index") != group_index or row.get("output_start") != cursor
                or type(end) is not int or not cursor < end <= len(raw_output)
                or digest(raw_output[cursor:end]) != row.get("output_sha256")):
            raise ValueError("paragraph output span/hash mismatch")
        if end < len(raw_output) and raw_output[end:end + 2] != "\n\n":
            raise ValueError("paragraph boundary missing")
        cursor = end + 2
    if cursor - 2 != len(raw_output) and (rows or raw_output):
        raise ValueError("uncovered paragraph output")
