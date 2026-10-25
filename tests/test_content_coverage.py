"""Synthetic source coverage checks; never modify a finished book fixture."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import content_coverage as coverage


BOOK = """# Preface

The author explains the scope.

# Contents

First chapter .... 1
Appendix .... 9

# First chapter

The first paragraph has a note.[^n1]

The second paragraph must survive independently.

![Diagram](images/diagram.png)

[^n1]: A complete footnote body.

| Quantity | Value |
| --- | --- |
| Mass | 2 |

# Appendix A

Supplementary proof.

# Bibliography

Author. Original title. 1999.

# Index

Energy, 2
Mass, 1
"""


def decision_profile(text, span, action="omit"):
    start = text.index(span)
    return {"schema": 1, "cleaning": {
        "source_sha256": coverage.sha_text(text),
        "decisions": [{"id": "review-01", "start": start, "end": start + len(span),
                       "source_sha256": coverage.sha_text(span), "action": action,
                       "reason": "Checked against the source; explicit editorial decision."}]}}


def post_profile(text, old, new):
    return {"schema": 1, "post_edits": [{
        "id": "edit-01", "source_sha256": coverage.sha_text(text),
        "old": old, "new": new, "reason": "Exact correction checked in source.",
        "expected_matches": 1}]}


def test_default_preserves_preface_appendix_bibliography_index_notes_images_tables():
    cleaned, report = coverage.clean_with_report(BOOK)
    assert cleaned == BOOK
    assert report["status"] == "passed"
    assert report["default_decision"] == "keep"
    assert report["before"] == report["after"]
    assert {"front", "toc", "body", "appendix", "bibliography", "index"} <= set(
        report["before"]["matter_counts"])
    assert report["before"]["counts"]["table"] == 1
    assert len(report["before"]["images"]) == 1
    assert [row["kind"] for row in report["before"]["notes"]] == ["reference", "definition"]


def test_default_keeps_repetition_cjk_and_suspicious_ocr():
    text = "# Book\n\n" + ("Repeated running head\n\n" * 10)
    text += "漢字\n\n2S§«=| | =| Ac] |e 3 - ———_ os=i == L-=\n"
    assert coverage.clean_with_report(text)[0] == text


def test_default_preserves_literal_blank_lines_indent_and_terminal_newline():
    text = "Start.\r\n\r\n \r\n\r\n    indented code\r\nA  B\n"
    cleaned, _ = coverage.clean_with_report(text)
    assert cleaned == text
    assert coverage.clean_with_report(cleaned)[0] == cleaned
    assert coverage.clean_with_report("No final newline")[0] == "No final newline"


def test_explicit_plain_prose_blank_normalization_keeps_nonblank_spaces():
    text = "Start.\r\n\r\n \r\n\r\nOrdinary prose.\r\nA  B"
    profile = {"schema": 1, "cleaning": {"normalize_blank": True}}
    cleaned, _ = coverage.clean_with_report(text, profile)
    assert cleaned == "Start.\n\nOrdinary prose.\r\nA  B\n"
    assert coverage.clean_with_report(cleaned, profile)[0] == cleaned


@pytest.mark.parametrize("text", [
    '```python\ns = """first\n\n\n\nlast"""\n```\n',
    "~~~text\nfirst\n\n\n\nlast\n~~~\n",
    "$$\na=b\n\n\n\nc=d\n$$\n",
    "\\[a=b\n\n\n\nc=d\\]\n",
    "    literal code\n\n\n\n    next line\n",
    "<pre>first\n\n\n\nlast</pre>\n",
])
def test_literal_content_preserved_and_protected_from_blank_normalization(text):
    assert coverage.clean_with_report(text)[0] == text
    with pytest.raises(ValueError, match="normalize_blank cannot operate"):
        coverage.clean_with_report(text, {"schema": 1, "cleaning": {"normalize_blank": True}})


def test_replay_detects_drop_and_reordering_even_if_counts_match(tmp_path):
    src, dst = tmp_path / "source.md", tmp_path / "clean.md"
    src.write_bytes(BOOK.encode())
    receipt = coverage.clean_with_coverage(src, dst)
    assert coverage.verify_cleaning_coverage(src, dst) == receipt
    changed = BOOK.replace("Supplementary proof.\n\n", "")
    dst.write_bytes(changed.encode())
    with pytest.raises(ValueError, match="lost or reordered"):
        coverage.verify_cleaning_coverage(src, dst)
    first = "The first paragraph has a note.[^n1]"
    second = "The second paragraph must survive independently."
    dst.write_bytes(BOOK.replace(first, "TEMP").replace(second, first).replace("TEMP", second).encode())
    with pytest.raises(ValueError, match="lost or reordered"):
        coverage.verify_cleaning_coverage(src, dst)
    assert src.read_bytes() == BOOK.encode()


def test_count_preserving_tampered_receipt_is_rejected(tmp_path):
    src, dst = tmp_path / "source.md", tmp_path / "clean.md"
    src.write_bytes(BOOK.encode())
    receipt = coverage.clean_with_coverage(src, dst)
    modified = BOOK.replace("Supplementary proof.", "Deleted substantive material.")
    dst.write_bytes(modified.encode())
    receipt["output_sha256"] = coverage.sha_text(modified)
    receipt["after"] = coverage.inventory(modified)
    dst.with_suffix(".coverage.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="does not replay"):
        coverage.verify_cleaning_coverage(src, dst)


def test_reviewed_omit_and_keep_are_content_bound(tmp_path):
    span = "The author explains the scope.\n\n"
    profile = decision_profile(BOOK, span)
    result, report = coverage.clean_with_report(BOOK, profile)
    assert result == BOOK.replace(span, "")
    assert report["decisions"][0]["removed_text"] == span
    profile["cleaning"]["decisions"][0]["action"] = "keep"
    assert coverage.clean_with_report(BOOK, profile)[0] == BOOK
    with pytest.raises(ValueError, match="stale cleaning"):
        coverage.clean_with_report(BOOK + "Extra source text.\n", profile)
    changed = copy.deepcopy(profile)
    changed["cleaning"]["decisions"][0]["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="stale decision"):
        coverage.clean_with_report(BOOK, changed)


def test_toc_rebuilding_is_explicit_and_cannot_claim_index_rebuild():
    toc = "# Contents\n\nFirst chapter .... 1\nAppendix .... 9\n\n"
    profile = decision_profile(BOOK, toc, "rebuild")
    result, report = coverage.clean_with_report(BOOK, profile)
    assert "# Contents" not in result
    assert "# Index" in result
    assert report["decisions"][0]["action"] == "rebuild"
    index = "# Index\n\nEnergy, 2\nMass, 1\n"
    with pytest.raises(ValueError, match="rebuild only supports"):
        coverage.clean_with_report(BOOK, decision_profile(BOOK, index, "rebuild"))


@pytest.mark.parametrize("change", ["reason", "duplicate", "overlap", "whole_hash", "bounds", "action"])
def test_bad_cleaning_decisions_fail(change):
    profile = decision_profile(BOOK, "Supplementary proof.")
    row = profile["cleaning"]["decisions"][0]
    if change == "reason":
        row["reason"] = " "
    elif change == "duplicate":
        profile["cleaning"]["decisions"].append(copy.deepcopy(row))
    elif change == "overlap":
        second = dict(row, id="review-02")
        profile["cleaning"]["decisions"].append(second)
    elif change == "whole_hash":
        del profile["cleaning"]["source_sha256"]
    elif change == "bounds":
        row["end"] = len(BOOK) + 1
    else:
        row["action"] = "auto"
    with pytest.raises(ValueError):
        coverage.clean_with_report(BOOK, profile)


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x0b", "\x1f", "\x7f", "\x85", "\ufeff"])
def test_unknown_controls_stop_without_overwriting_output(tmp_path, control):
    src, dst = tmp_path / "source.md", tmp_path / "clean.md"
    src.write_bytes(("A" + control + "B").encode())
    dst.write_text("previous candidate", encoding="utf-8")
    with pytest.raises(ValueError, match="unresolved control"):
        coverage.clean_with_coverage(src, dst)
    assert dst.read_text() == "previous candidate"
    receipt = json.loads(dst.with_suffix(".coverage.json").read_text())
    assert receipt["status"] == "failed"


def test_book_edits_default_leaves_atlas_names_and_title_untouched():
    text = "# Акупунктура 0\n\n# Каналы Клаудия Фокс, Ульрих Мерц\n"
    assert coverage.apply_book_edits(text)[0] == text
    profile = post_profile(text, "# Акупунктура 0", "# Атлас акупунктуры")
    result, report = coverage.apply_book_edits(text, profile)
    assert result.startswith("# Атлас акупунктуры\n")
    assert "Клаудия Фокс, Ульрих Мерц" in result
    assert report["edits"][0]["old"] == "# Акупунктура 0"
    assert report["source_sha256"] == coverage.sha_text(text)


def test_exact_control_correction_is_audited():
    text = "A\x01B"
    with pytest.raises(ValueError, match="unresolved control"):
        coverage.apply_book_edits(text)
    result, report = coverage.apply_book_edits(text, post_profile(text, "A\x01B", "A > B"))
    assert result == "A > B"
    assert report["edits"][0]["old"] == text


@pytest.mark.parametrize("change", ["stale", "old", "ambiguous", "reason", "count", "duplicate", "overlap", "new_control"])
def test_bad_post_edits_fail_and_unused_edits_cannot_pass(change):
    text = "Original title.\nSecond author.\n"
    profile = post_profile(text, "Original title.", "Correct title.")
    row = profile["post_edits"][0]
    if change == "stale":
        row["source_sha256"] = "0" * 64
    elif change == "old":
        row["old"] = "Absent phrase."
    elif change == "ambiguous":
        row["old"] = "."
    elif change == "reason":
        row["reason"] = ""
    elif change == "count":
        row["expected_matches"] = 2
    elif change == "duplicate":
        profile["post_edits"].append(copy.deepcopy(row))
    elif change == "overlap":
        profile["post_edits"].append(dict(row, id="edit-02", old="title."))
    else:
        row["new"] = "Bad\x01text"
    with pytest.raises(ValueError):
        coverage.apply_book_edits(text, profile)


def test_profile_paths_and_unknown_fields(tmp_path):
    path = tmp_path / "book.profile.json"
    profile = post_profile(BOOK, "Supplementary proof.", "Checked supplementary proof.")
    path.write_text(json.dumps(profile), encoding="utf-8")
    assert coverage.load_profile(path) == profile
    assert "Checked supplementary proof." in coverage.apply_book_edits(BOOK, path)[0]
    with pytest.raises(ValueError, match="unknown book profile"):
        coverage.load_profile({"schema": 1, "post_edit": []})


def test_source_cannot_be_overwritten(tmp_path):
    src = tmp_path / "source.md"
    src.write_text(BOOK, encoding="utf-8")
    with pytest.raises(ValueError, match="separate source"):
        coverage.clean_with_coverage(src, src)
    assert src.read_text(encoding="utf-8") == BOOK


def test_cli_uses_conservative_policy_and_writes_receipt(tmp_path):
    src, dst = tmp_path / "source.md", tmp_path / "clean.md"
    src.write_bytes(BOOK.encode())
    script = Path(__file__).resolve().parents[1] / "scripts" / "clean_markdown.py"
    result = subprocess.run([sys.executable, str(script), str(src), str(dst)],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert dst.read_bytes() == BOOK.encode()
    assert coverage.verify_cleaning_coverage(src, dst)["status"] == "passed"
