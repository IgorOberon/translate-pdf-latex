"""Visible-page image acceptance using fresh synthetic PDFs, no books/TeX/model."""
from pathlib import Path
import sys

import pytest

fitz = pytest.importorskip("fitz")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_release import inspect_pdf, publish_candidate
from provenance import sha_file


def make_pdf(tmp_path, pages, *, name="candidate"):
    pdf, log = tmp_path / (name + ".pdf"), tmp_path / (name + ".log")
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pixmap.clear_with(112)
    # A built-in MuPDF font buffer avoids depending on a system font install;
    # insert_font embeds those bytes, so ordinary font acceptance is exercised.
    font = fitz.Font("helv").buffer
    with fitz.open() as doc:
        for specification in pages:
            page = doc.new_page(width=specification.get("width", 160),
                                height=specification.get("height", 100))
            image_xref = 0
            for rect in specification.get("images", []):
                if image_xref:
                    page.insert_image(rect, xref=image_xref, keep_proportion=False)
                else:
                    image_xref = page.insert_image(rect, pixmap=pixmap, keep_proportion=False)
            if specification.get("clip"):
                # This is a genuine content-stream clipping path around the
                # image operation, not a fake bbox returned by a mock object.
                stream = page.get_contents()[-1]
                doc.update_stream(stream, b"q 10 10 80 80 re W n\n" + doc.xref_stream(stream) + b"\nQ")
            if "crop" in specification:
                page.set_cropbox(specification["crop"])
            page.insert_font(fontname="fixture", fontbuffer=font)
            page.insert_text((20, 16), "Synthetic fixture", fontname="fixture", fontsize=8)
            page.set_rotation(specification.get("rotation", 0))
        doc.save(pdf)
    log.write_text("Output written on candidate.pdf\n", encoding="utf-8")
    return pdf, log


def inspect_fixture(tmp_path, pages):
    pdf, log = make_pdf(tmp_path, pages)
    before = sha_file(pdf)
    result = inspect_pdf(pdf, log)
    assert sha_file(pdf) == before, "inspection must not rewrite the PDF"
    assert result["pdf_sha256"] == before
    return result


def test_no_images_and_inside_text_margin_are_valid(tmp_path):
    result = inspect_fixture(tmp_path, [{}, {"images": [(1, 25, 25, 70)]}])
    assert result["status"] == "passed", result
    geometry = result["image_geometry"]
    assert geometry["status"] == "passed" and geometry["placements"] == 1
    assert geometry["outside_count"] == 0 and geometry["affected_pages"] == []
    # An image one point from the page edge may violate a book's text margins;
    # this deliberately narrower gate must not claim to inspect those margins.
    assert geometry["text_margins"] == "not_checked"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_exact_page_cover_and_rotated_inside_image_pass(tmp_path, rotation):
    result = inspect_fixture(tmp_path, [{"rotation": rotation,
        "images": [(0, 0, 160, 100), (120, 20, 150, 60)]}])
    assert result["status"] == "passed", result
    assert result["image_geometry"]["placements"] == 2


@pytest.mark.parametrize("rect,side", [((-8, 20, 30, 60), "left"),
                                      ((20, -8, 60, 30), "top"),
                                      ((130, 20, 168, 60), "right"),
                                      ((20, 70, 60, 108), "bottom")])
def test_each_visible_page_edge_blocks_release(tmp_path, rect, side):
    result = inspect_fixture(tmp_path, [{"images": [rect]}])
    assert result["status"] == "failed", result
    geometry = result["image_geometry"]
    assert geometry["status"] == "needs_review"
    assert geometry["outside_count"] == 1 and geometry["affected_pages"] == [1]
    assert geometry["details"][0]["overflow_pt"][side] == pytest.approx(8)
    assert all("geometry needs review" in error for error in result["errors"])
    assert result["visual_review"]["status"] == "not_run"


@pytest.mark.parametrize("extension,expected", [(0.99, "passed"), (1.0, "passed"), (1.01, "failed")])
def test_one_point_tolerance_has_a_real_boundary(tmp_path, extension, expected):
    result = inspect_fixture(tmp_path, [{"images": [(130, 20, 160 + extension, 60)]}])
    assert result["status"] == expected, result
    assert result["image_geometry"]["tolerance_pt"] == 1.0


@pytest.mark.parametrize("rotation,visible_side", [(0, "right"), (90, "bottom"),
                                                   (180, "left"), (270, "top")])
def test_offset_cropbox_and_rotation_use_the_visible_page(tmp_path, rotation, visible_side):
    # Both images fit the physical MediaBox. The second crosses the smaller
    # visible CropBox by 20 pt. Rotation changes which visible edge is crossed.
    result = inspect_fixture(tmp_path, [{"width": 220, "height": 140,
        "crop": (30, 20, 190, 120), "rotation": rotation,
        "images": [(40, 30, 100, 80), (180, 40, 210, 80)]}])
    assert result["status"] == "failed", result
    geometry = result["image_geometry"]
    assert geometry["placements"] == 2 and geometry["outside_count"] == 1
    assert geometry["details"][0]["occurrence"] == 2
    assert geometry["details"][0]["overflow_pt"][visible_side] == pytest.approx(20)
    expected_rect = [0, 0, 100, 160] if rotation in (90, 270) else [0, 0, 160, 100]
    assert geometry["details"][0]["visible_page"] == expected_rect


def test_reused_image_is_checked_at_every_placement_on_every_page(tmp_path):
    result = inspect_fixture(tmp_path, [
        {"images": [(10, 20, 40, 60), (145, 20, 175, 60)]},
        {"images": [(10, 20, 40, 60)]},
        {"images": [(-20, 20, 10, 60), (145, 20, 175, 60)]}])
    geometry = result["image_geometry"]
    assert geometry["placements"] == 5 and geometry["outside_count"] == 3
    assert geometry["affected_pages"] == [1, 3]
    assert [(row["page"], row["occurrence"]) for row in geometry["details"]] == [(1, 2), (3, 1), (3, 2)]


def test_wholly_outside_image_is_not_omitted_from_acceptance(tmp_path):
    result = inspect_fixture(tmp_path, [{"images": [(180, 20, 210, 60)]}])
    assert result["status"] == "failed", result
    assert result["image_geometry"]["placements"] == 1
    assert result["image_geometry"]["outside_count"] == 1


def test_intentional_clip_remains_explicit_review_instead_of_an_automatic_exemption(tmp_path):
    pdf, log = make_pdf(tmp_path, [{"width": 100, "images": [(-10, -10, 110, 110)], "clip": True}])
    with fitz.open(pdf) as doc:
        rendered = doc[0].get_pixmap(alpha=False)
        assert rendered.pixel(5, 5) == (255, 255, 255)
        assert rendered.pixel(50, 50) != (255, 255, 255)
    result = inspect_pdf(pdf, log)
    assert result["status"] == "failed"
    geometry = result["image_geometry"]
    assert geometry["status"] == "needs_review" and geometry["outside_count"] == 1
    assert "intentional clipping" in geometry["clipping"]
    assert geometry["details"][0]["reason"] == "outside_visible_page"


def test_outside_candidate_cannot_replace_a_synthetic_destination(tmp_path):
    pdf, log = make_pdf(tmp_path, [{"images": [(145, 20, 175, 60)]}])
    technical = inspect_pdf(pdf, log)
    report = {"status": "passed", "profile": "release", "pdf_sha256": sha_file(pdf),
              "compile": {"status": "passed", "pdf_sha256": sha_file(pdf)}, "technical": technical}
    destination = tmp_path / "synthetic-existing.pdf"
    original = b"Synthetic protected destination; not a real or finished book."
    destination.write_bytes(original)
    with pytest.raises(ValueError, match="release acceptance"):
        publish_candidate(pdf, destination, report)
    assert destination.read_bytes() == original
    assert not Path(str(destination) + ".release.json").exists()
