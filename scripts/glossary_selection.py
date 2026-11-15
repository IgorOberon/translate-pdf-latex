"""Optional book-owned glossary groups; critical entries are never dropped.

The book glossary is intentionally kept complete on disk and for validation,
but sending the complete glossary with every translation request wastes most of
the context window.  For a translation request we therefore select matching
base entries and keep the complete glossary only for validation
(``all_groups=True``).
"""
import json
from pathlib import Path
import re


MAX_REQUEST_GLOSSARY_CHARS = 6000


def _normalise(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _term_occurs(source, term):
    """Match a glossary source term without letting ``IR`` match ``FIR``."""
    source_n = _normalise(source)
    term_n = _normalise(term)
    if not term_n or len(term_n) < 2:
        return False
    if re.fullmatch(r"[a-z0-9_]+", term_n):
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(term_n)
                             + r"(?![a-z0-9_])", source_n))
    return term_n in source_n


def _compact_base(source, base):
    """Return only base glossary lines relevant to this request.

    The original glossary remains untouched.  If an unusually dense block
    matches more than the safe budget, longer/more specific terms win and the
    selected lines are restored to their original glossary order.
    """
    lines = [line.strip() for line in str(base or "").splitlines()
             if line.strip()]
    if not source or not lines:
        return str(base or "").strip()

    matched = []
    for position, line in enumerate(lines):
        term = line.split("=>", 1)[0].strip()
        if _term_occurs(source, term):
            term_n = _normalise(term)
            occurrences = _normalise(source).count(term_n)
            matched.append((position, line, len(term_n), occurrences))

    if sum(len(row[1]) + 1 for row in matched) <= MAX_REQUEST_GLOSSARY_CHARS:
        return "\n".join(row[1] for row in matched)

    # Prefer longer, more specific phrases and repeated terms under pressure.
    budget = MAX_REQUEST_GLOSSARY_CHARS
    chosen = set()
    used = 0
    for position, line, term_len, occurrences in sorted(
            matched, key=lambda row: (row[2] >= 4, row[2], row[3]),
            reverse=True):
        cost = len(line) + 1
        if used + cost > budget:
            continue
        chosen.add(position)
        used += cost
    return "\n".join(row[1] for row in matched if row[0] in chosen)


def load_profile(path=None):
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("glossary profile requires schema: 1")
    entries = value.get("critical", [])
    if not isinstance(entries, list) or not all(isinstance(x, str) for x in entries):
        raise ValueError("critical must be a list of glossary lines")
    for group in value.get("groups", []):
        re.compile(group["source_pattern"])
        if not isinstance(group["entries"], list) or not all(isinstance(x, str) for x in group["entries"]):
            raise ValueError("group entries must be glossary lines")
    return value


def select_glossary(source, base="", profile=None, all_groups=False):
    profile = profile or {}
    # Validation must see the complete approved glossary.  Translation prompts
    # receive only source-relevant base entries to avoid context overflow.
    base_text = (base.strip() if all_groups
                 else _compact_base(source, base))
    entries = [base_text, *profile.get("critical", [])]
    for group in profile.get("groups", []):
        if all_groups or re.search(group["source_pattern"], source):
            # Keep each entire group, including the paired/opposed renderings.
            entries.extend(group["entries"])
    return "\n".join(dict.fromkeys(e for e in entries if e))
