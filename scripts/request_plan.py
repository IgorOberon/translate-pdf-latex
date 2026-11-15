"""Lossless request planning shared by MT and context estimation (no model I/O)."""
import re

PLANNER_VERSION = "sentences-v2"
# Suspect OCR punctuation is a review finding, not permission to rewrite prose.
OCR_STOP = re.compile(r"\b(?:[3z]\.\s*(?=B\.)|(?:sein|ihr|mein|dein|unser|euer)\.\s*(?=[A-ZÄÖÜ]))")
ABBREVIATION = re.compile(
    r"(?:\b(?:z|d|u|s|f|i|e|g|vgl|Bd|Th|Nr|Anm|Abschn|Aufl|Hr|Kap|cf|"
    r"resp|bzw|usw|etc|vol|pp|Fig|Eq|Dr|Mr|Mrs|т|д|е|п|им|рис|стр|см)\."
    r"|\b[A-ZÄÖÜА-ЯЁ]\."
    r"|(?:§§?\.?|\bS\.|\bстр\.)\s*\d+\.)\s*$", re.I)
PROTECTED = re.compile(
    r"⟦M\d{6}⟧|`[^`]*`|\[[^\]]*\]\([^)]*\)|"
    r"(?:§§?\.?|\bS\.)\s*\d+(?:[–−-]\d+)?\.?|"
    r"\b\d+[.,]\d+|«[^»]*»|“[^”]*”|„[^“]*“|\"[^\"]*\"", re.S)


def plan_fragments(text, limit=0):
    """Exact source slices. limit is a target, NEVER a promised hard ceiling.

    Prefer sentence boundaries.  If a long paragraph has no usable sentence
    stop (common in the formula inventory), use a protected whitespace stop so
    one pathological paragraph cannot occupy a full 600-second model request.
    Markdown structural blocks are kept whole. Whitespace belongs to the left
    slice, so ''.join(fragment['text']) reproduces the input byte-for-byte.
    """
    barriers = re.search(r"(?m)^\s*(?:#{1,6}\s|>|\[\^|[-*+]\s|```|\|)", text)
    spans = [m.span() for m in PROTECTED.finditer(text)]
    suspect_stops = [m.span() for m in OCR_STOP.finditer(text)]
    cuts = []
    if limit and len(text) > limit and not barriers:
        for m in re.finditer(r"[.!?]\s+", text):
            pos = m.end()
            if ABBREVIATION.search(text[:pos]):
                continue
            if any(lo < pos < hi for lo, hi in spans):
                continue
            if any(lo < pos <= hi for lo, hi in suspect_stops):
                continue
            cuts.append(pos)
        # Index lines and formula inventories may contain no sentence-ending
        # punctuation at all.  A safe whitespace fallback is preferable to a
        # single multi-kilobyte request; never cut inside a protected mask,
        # code span, link, or quoted string.  This changes only oversized
        # requests, so ordinary request-cache identities remain stable.
        if not cuts:
            for m in re.finditer(r"\s+", text):
                pos = m.end()
                if pos >= len(text):
                    continue
                if ABBREVIATION.search(text[:pos]):
                    continue
                if any(lo < pos < hi for lo, hi in spans):
                    continue
                cuts.append(pos)
    out, start = [], 0
    while limit and len(text) - start > limit and cuts:
        remaining = [c for c in cuts if c > start and c < len(text)]
        if not remaining:
            break
        before = [c for c in remaining if c - start <= limit]
        end = max(before) if before else min(remaining)
        out.append({"start": start, "end": end, "text": text[start:end],
                    "boundary": "sentence", "oversized": end - start > limit})
        start = end
    out.append({"start": start, "end": len(text), "text": text[start:],
                "boundary": "end", "oversized": bool(limit and len(text) - start > limit)})
    return out
