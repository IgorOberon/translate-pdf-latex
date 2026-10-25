"""Explicit, content-bound decisions to retain translation units verbatim.

This is separate from cache/request identity and never repairs failed reports.
Indices are one-based units AFTER grouping, in the masked MT source.
"""
import json
from pathlib import Path
import re

from provenance import sha_text


def load_policy(path):
    if not path:
        return {"schema": 1, "blocks": []}
    policy = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if (not isinstance(policy, dict) or policy.get("schema") != 1
            or not isinstance(policy.get("blocks"), list)):
        raise ValueError("keep-source requires schema 1 and a blocks array")
    return policy


def select_preserved(blocks, policy):
    selected = {}
    if (not isinstance(policy, dict) or policy.get("schema") != 1
            or not isinstance(policy.get("blocks"), list)):
        raise ValueError("invalid keep-source policy")
    for row in policy["blocks"]:
        if not isinstance(row, dict):
            raise ValueError("invalid keep-source entry")
        index, reason = row.get("index"), row.get("reason")
        if (type(index) is not int or not 1 <= index <= len(blocks)
                or index in selected or not isinstance(reason, str) or not reason.strip()
                or row.get("source_sha256") != sha_text(blocks[index - 1])):
            raise ValueError("keep-source entry is duplicate, stale or lacks a reason")
        selected[index] = {"index": index, "source_sha256": row["source_sha256"],
                           "reason": reason.strip()}
    return selected


def render_preserved(block):
    """Retain paragraph/note boundaries as well as the original wording."""
    return block


def verify_preserved(source, output, report):
    """Validate decisions and exact output spans; never accept rejected replies."""
    from translate import group_blocks, split_blocks
    rows = report.get("allowed_source", [])
    if not isinstance(rows, list) or report.get("preserved_source", 0) != len(rows):
        raise ValueError("invalid preserved-source count")
    if not rows:
        return
    if not re.fullmatch(r"[0-9a-f]{64}", report.get("source_preservation_sha256") or ""):
        raise ValueError("preserved-source policy identity missing")
    units = group_blocks(split_blocks(source), report["request_config"].get("group_chars", 0))
    selected = select_preserved(units, {"schema": 1, "blocks": rows})
    failed = {r["index"] for r in report.get("failed_blocks", [])}
    if failed.intersection(selected):
        raise ValueError("a failed unit cannot also be explicitly preserved")
    previous_end = -1
    for row in sorted(rows, key=lambda r: r["index"]):
        start, end = row.get("output_start"), row.get("output_end")
        if (type(start) is not int or type(end) is not int
                or not 0 <= start <= end <= len(output) or start < previous_end
                or output[start:end] != render_preserved(units[row["index"] - 1])
                or (start and output[max(0, start - 2):start] != "\n\n")
                or (end < len(output) and output[end:end + 2] != "\n\n")):
            raise ValueError("preserved unit differs from source or has invalid output span")
        previous_end = end


def translatable_output(output, report):
    """Exclude deliberate retained units from the target-script heuristic."""
    for row in sorted(report.get("allowed_source", []), key=lambda r: r["output_start"], reverse=True):
        output = output[:row["output_start"]] + output[row["output_end"]:]
    return output


def remap_preserved(before, after, report):
    """Follow unchanged units through recorded edits outside those units.

    Exact paragraph-delimited occurrences, in their original order, avoid fuzzy
    matching and quadratic character diffs on book-sized texts. Ambiguous edits
    that change the number of identical occurrences are conservatively refused.
    """
    if before == after or not report.get("allowed_source"):
        return report

    def occurrences(text, literal):
        positions, cursor = [], 0
        while True:
            start = text.find(literal, cursor)
            if start < 0:
                return positions
            end = start + len(literal)
            if ((start == 0 or text[max(0, start - 2):start] == "\n\n")
                    and (end == len(text) or text[end:end + 2] == "\n\n")):
                positions.append(start)
            cursor = start + 1

    rows = []
    for row in report["allowed_source"]:
        literal = before[row["output_start"]:row["output_end"]]
        old, new = occurrences(before, literal), occurrences(after, literal)
        if not literal or len(old) != len(new) or row["output_start"] not in old:
            raise ValueError("post-edit changes or ambiguously relocates an explicitly preserved unit")
        start = new[old.index(row["output_start"])]
        rows.append(dict(row, output_start=start, output_end=start + len(literal)))
    if [r["output_start"] for r in rows] != sorted(r["output_start"] for r in rows):
        raise ValueError("post-edit reorders explicitly preserved units")
    return dict(report, allowed_source=rows)
