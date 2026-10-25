#!/usr/bin/env python3
"""Auditable, conservative Markdown cleaning and exact book-specific edits.

Coverage begins at the supplied Markdown, not at the PDF: it cannot certify
OCR completeness or the semantics of a translation. Reports are replayable;
counts alone never authorize a deletion, replacement or reordering.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata


SCHEMA = 1
POLICY = "preserve-content-v1"
_HEADING = re.compile(r"(?m)^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^\n]+?\)")
_NOTE = re.compile(r"\[\^([^\]\s]+)\](:)?")
_TOC_NAMES = {"contents", "table of contents", "содержание", "оглавление"}
_TOC_ENTRY = re.compile(r"^\S.*?(?:\s*\.(?:\s*\.){1,}\s*|\s+)\d{1,4}$")


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assert_no_control_chars(text: str) -> None:
    """Do not silently erase a potentially lost relation, digit or punctuation."""
    bad = [(i, f"U+{ord(ch):04X}") for i, ch in enumerate(text)
           if (unicodedata.category(ch) in {"Cc", "Cs"} and ch not in "\r\n\t")
           or ch == "\ufeff"]
    if bad:
        sample = ", ".join(f"{code} at {offset}" for offset, code in bad[:8])
        raise ValueError(f"unresolved control characters ({len(bad)}): {sample}; "
                         "check extraction or supply a reviewed exact edit")


def load_book_profile(profile=None) -> dict:
    if profile is None:
        return {"schema": SCHEMA}
    if isinstance(profile, (str, Path)):
        profile = json.loads(Path(profile).read_bytes().decode("utf-8"))
    if not isinstance(profile, dict) or profile.get("schema") != SCHEMA:
        raise ValueError("book profile must be a schema 1 JSON object")
    if set(profile) - {"schema", "cleaning", "post_edits"}:
        raise ValueError("unknown book profile fields")
    if "cleaning" in profile and not isinstance(profile["cleaning"], dict):
        raise ValueError("cleaning must be an object")
    if "post_edits" in profile and not isinstance(profile["post_edits"], list):
        raise ValueError("post_edits must be a list")
    # Copy so callers cannot accidentally mutate the snapshot in a report.
    return json.loads(json.dumps(profile, ensure_ascii=False))


def _profile_hash(profile: dict) -> str:
    return sha_text(json.dumps(profile, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":")))


# Short spelling used by the pipeline settings validator.
load_profile = load_book_profile


def _matter(title: str, previous: str) -> str:
    name = re.sub(r"^[\d.\s]+", "", title).strip(" *#").casefold()
    if name in _TOC_NAMES:
        return "toc"
    if re.match(r"^(?:appendix|appendices|приложени[ея])\b", name):
        return "appendix"
    if name in {"bibliography", "references", "литература", "список литературы"}:
        return "bibliography"
    if name in {"index", "subject index", "name index", "указатель",
                "предметный указатель", "именной указатель"}:
        return "index"
    if name in {"preface", "foreword", "acknowledgments", "acknowledgements",
                "предисловие", "благодарности", "dedication", "посвящение"}:
        return "front"
    return "body" if previous in {"front", "toc"} else previous


def inventory(text: str) -> dict:
    """Ordered recognized objects with offsets/hashes; no inferred OCR objects."""
    objects = []
    # Blank-delimited blocks, split at headings/definitions even without a blank.
    boundaries = {0, len(text)}
    for match in re.finditer(r"\n[ \t\r]*\n+", text):
        boundaries.update((match.start(), match.end()))
    for match in re.finditer(r"(?m)^ {0,3}(?:#{1,6}[ \t]+|\[\^[^\]\s]+\]:)", text):
        boundaries.add(match.start())
        if match.group(0).lstrip().startswith("#"):
            newline = text.find("\n", match.end())
            boundaries.add(len(text) if newline < 0 else newline + 1)
    points = sorted(boundaries)
    matter = "front"
    for start, end in zip(points, points[1:]):
        raw = text[start:end]
        if not raw.strip():
            continue
        body = raw.strip()
        match = _HEADING.fullmatch(body)
        marker = body.strip("*# ").casefold()
        if match:
            kind = "heading"
            matter = _matter(match.group(2), matter)
        elif marker in _TOC_NAMES or marker in {"index", "subject index", "name index",
                                                "указатель", "предметный указатель"}:
            kind = "heading"
            matter = _matter(marker, matter)
        elif re.match(r"\[\^[^\]\s]+\]:", body):
            kind = "note"
        elif body.startswith("|") or re.search(r"<table\b", body, re.I):
            kind = "table"
        elif body.startswith(("$$", "\\[")):
            kind = "math"
        elif body.startswith(("```", "~~~")):
            kind = "code"
        elif _IMAGE.fullmatch(body):
            kind = "image"
        else:
            kind = "paragraph"
        objects.append({"id": f"o{len(objects) + 1:06d}", "kind": kind,
                        "matter": matter, "start": start, "end": end,
                        "source_sha256": sha_text(raw),
                        "excerpt": body[:100]})
    images = [{"start": m.start(), "end": m.end(), "source_sha256": sha_text(m.group())}
              for m in _IMAGE.finditer(text)]
    notes = [{"start": m.start(), "id": m.group(1),
              "kind": "definition" if m.group(2) else "reference"}
             for m in _NOTE.finditer(text)]
    return {"source_sha256": sha_text(text), "objects": objects,
            "counts": dict(Counter(o["kind"] for o in objects)),
            "matter_counts": dict(Counter(o["matter"] for o in objects)),
            "images": images, "notes": notes}


def _exact_reason(row: dict, seen: set) -> None:
    if not isinstance(row, dict):
        raise ValueError("decision/edit must be an object")
    ident, reason = row.get("id"), row.get("reason")
    if not isinstance(ident, str) or not ident.strip() or ident in seen:
        raise ValueError("decision/edit id must be nonempty and unique")
    seen.add(ident)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{ident}: a review reason is required")


def _is_toc_span(text: str) -> bool:
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if not rows or rows[0].strip("#* ").casefold() not in _TOC_NAMES:
        return False
    return len(rows) > 1 and all(_TOC_ENTRY.fullmatch(row) for row in rows[1:])


def clean_with_report(text: str, profile=None) -> tuple[str, dict]:
    """Keep exact content by default; whitespace changes require an explicit choice."""
    profile = load_book_profile(profile)
    assert_no_control_chars(text)
    cfg = profile.get("cleaning", {})
    if set(cfg) - {"source_sha256", "decisions", "normalize_blank"}:
        raise ValueError("unknown cleaning profile fields")
    if cfg.get("source_sha256", sha_text(text)) != sha_text(text):
        raise ValueError("stale cleaning source_sha256")
    rows = cfg.get("decisions", [])
    if not isinstance(rows, list):
        raise ValueError("cleaning decisions must be a list")
    if rows and cfg.get("source_sha256") != sha_text(text):
        raise ValueError("cleaning decisions require the whole source_sha256")
    seen, checked = set(), []
    for row in rows:
        _exact_reason(row, seen)
        if set(row) - {"id", "start", "end", "source_sha256", "action", "reason"}:
            raise ValueError(f"{row['id']}: unknown cleaning decision fields")
        start, end = row.get("start"), row.get("end")
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(text)):
            raise ValueError(f"{row['id']}: invalid source offsets")
        span = text[start:end]
        if sha_text(span) != row.get("source_sha256"):
            raise ValueError(f"{row['id']}: stale decision source_sha256")
        if row.get("action") not in {"keep", "omit", "rebuild"}:
            raise ValueError(f"{row['id']}: expected keep, omit or rebuild")
        if row["action"] == "rebuild" and not _is_toc_span(span):
            raise ValueError(f"{row['id']}: rebuild only supports an exact recognized TOC; "
                             "an index requires a separate verified rebuilding workflow")
        checked.append(dict(row, removed_text=span if row["action"] != "keep" else None))
    checked.sort(key=lambda row: row["start"])
    if any(a["end"] > b["start"] for a, b in zip(checked, checked[1:])):
        raise ValueError("overlapping cleaning decisions")
    cleaned = text
    for row in reversed(checked):
        if row["action"] != "keep":
            cleaned = cleaned[:row["start"]] + cleaned[row["end"]:]
    # Even blank lines may be literal data (e.g. a triple-quoted code string).
    # Optional normalization is intentionally limited to plain prose inputs.
    normalize = cfg.get("normalize_blank", False)
    if type(normalize) is not bool:
        raise ValueError("normalize_blank must be a boolean")
    if normalize:
        if (re.search(r"(?m)^(?: {0,3}(?:`{3,}|~{3,})| {4}|\t)", cleaned)
                or "$$" in cleaned or "\\[" in cleaned or "\\]" in cleaned
                or "\\begin{" in cleaned or re.search(r"<pre\b", cleaned, re.I)):
            raise ValueError("normalize_blank cannot operate on code, display math or "
                             "indented content; preserve it or use exact reviewed edits")
        cleaned = re.sub(r"\r?\n(?:[ \t]*\r?\n){3,}", "\n\n", cleaned)
        if cleaned and not cleaned.endswith("\n"):
            cleaned += "\n"
    report = {"schema": SCHEMA, "status": "passed", "policy": POLICY,
              "source_sha256": sha_text(text), "output_sha256": sha_text(cleaned),
              "profile_sha256": _profile_hash(profile),
              "default_decision": "keep", "decisions": checked,
              "normalizations": {"blank_lines": normalize},
              "before": inventory(text), "after": inventory(cleaned),
              "limits": "Coverage starts at supplied Markdown; no OCR or semantic completeness claim."}
    return cleaned, report


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_with_coverage(src, dst, report_path=None, profile=None) -> dict:
    src, dst = Path(src), Path(dst)
    report_path = Path(report_path) if report_path else dst.with_suffix(".coverage.json")
    if src.resolve() in {dst.resolve(), report_path.resolve()} or dst.resolve() == report_path.resolve():
        raise ValueError("cleaning requires separate source, output and coverage report paths")
    text = src.read_bytes().decode("utf-8")
    try:
        cleaned, report = clean_with_report(text, profile)
    except (ValueError, TypeError, KeyError) as exc:
        _write_report(report_path, {"schema": SCHEMA, "status": "failed", "policy": POLICY,
                                   "source_sha256": sha_text(text), "reason": str(exc)})
        raise
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(cleaned.encode("utf-8"))
    _write_report(report_path, report)
    verify_cleaning_coverage(src, dst, report_path, profile)
    return report


def verify_cleaning_coverage(src, dst, report_path=None, profile=None) -> dict:
    src, dst = Path(src), Path(dst)
    report_path = Path(report_path) if report_path else dst.with_suffix(".coverage.json")
    text = src.read_bytes().decode("utf-8")
    expected_text, expected_report = clean_with_report(text, profile)
    report = json.loads(report_path.read_bytes().decode("utf-8"))
    if report != expected_report:
        raise ValueError("cleaning coverage report does not replay from source/profile")
    if dst.read_bytes() != expected_text.encode("utf-8"):
        raise ValueError("cleaning output changed, lost or reordered content")
    return report


def apply_book_edits(text: str, profile=None, stage="postprocess") -> tuple[str, dict]:
    """Apply explicit unique replacements bound to the unedited whole input.

    All anchors locate against the original input; overlaps and chained edits
    are rejected, so profile order cannot create a match for a later edit.
    """
    if stage != "postprocess":
        raise ValueError("book edits currently support only postprocess")
    profile = load_book_profile(profile)
    source_hash, seen, edits = sha_text(text), set(), []
    for row in profile.get("post_edits", []):
        _exact_reason(row, seen)
        if set(row) - {"id", "source_sha256", "old", "new", "reason", "expected_matches"}:
            raise ValueError(f"{row['id']}: unknown post-edit fields")
        if row.get("source_sha256") != source_hash:
            raise ValueError(f"{row['id']}: stale post-edit source_sha256")
        old, new = row.get("old"), row.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError(f"{row['id']}: exact old and new text are required")
        if row.get("expected_matches", 1) != 1 or text.count(old) != 1:
            raise ValueError(f"{row['id']}: old text must match exactly once")
        start = text.index(old)
        edits.append(dict(row, start=start, end=start + len(old)))
    edits.sort(key=lambda row: row["start"])
    if any(a["end"] > b["start"] for a, b in zip(edits, edits[1:])):
        raise ValueError("overlapping post-edits")
    result = text
    for row in reversed(edits):
        result = result[:row["start"]] + row["new"] + result[row["end"]:]
    assert_no_control_chars(result)
    return result, {"schema": SCHEMA, "status": "passed", "stage": stage,
                    "source_sha256": source_hash, "output_sha256": sha_text(result),
                    "profile_sha256": _profile_hash(profile), "edits": edits,
                    "before": inventory(text), "after": inventory(result)}
