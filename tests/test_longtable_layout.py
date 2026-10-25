"""Isolated layout regressions; never compile or mutate an existing book."""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from translate_book import assemble_main_tex

FILTER = ROOT / "assets" / "figcaption.lua"
PANDOC = shutil.which("pandoc")
XELATEX = shutil.which("xelatex")


def pandoc(text, target="latex", source="markdown-implicit_figures", filtered=True):
    command = [PANDOC, "-f", source, "-t", target]
    if filtered:
        command += ["--lua-filter", str(FILTER)]
    return subprocess.run(command, input=text, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def write_png(path, width, height, color):
    """Small opaque raster with known natural size, without Pillow dependency."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = (b"\0" + bytes(color) * width) * height
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def write_layout_fixture(directory, *, filtered=True, preamble=None):
    """Build fresh synthetic inputs; caller owns the new directory."""
    directory = Path(directory)
    write_png(directory / "small.png", 129, 126, (20, 100, 180))
    write_png(directory / "wide.png", 712, 165, (120, 50, 120))
    write_png(directory / "tall.png", 712, 1101, (30, 130, 70))
    # Default (natural-width) columns used to allow three 45%-page-width images.
    table = "| A | B | C |\n|---|---|---|\n" + "| ![](wide.png) | ![](wide.png) | ![](wide.png) |\n" * 4
    table += "\n| A | B | C | D | E | F |\n|---|---|---|---|---|---|\n"
    table += "| " + " | ".join(["![](wide.png)"] * 6) + " |\n"
    # The atlas defect was also present in mixed-size, justified image chains,
    # including one footnote. Preserve every image and all surrounding text.
    chain = " ".join(["![](small.png)", "![](wide.png)", "![](tall.png)"] * 3)
    markdown = table + "\nText before. " + chain + " Text after.\n\nNote.[^n]\n\n[^n]: Note before. " + chain + " Note after.\n"
    (directory / "fixture.md").write_text(markdown, encoding="utf-8")
    body = (r"\chapter{A deliberately long chapter title with many words to exercise the running head after the compact geometry profile changes the page width}" + "\n"
            + pandoc(markdown, filtered=filtered) + "\n\\newpage\nEnd of fixture.\n")
    (directory / "body.tex").write_text(body, encoding="utf-8")
    if preamble is None:
        preamble = (ROOT / "assets" / "preamble.tex").read_text(encoding="utf-8")
    (directory / "main.tex").write_text(assemble_main_tex(preamble, layout="compact"), encoding="utf-8")
    return 36


@unittest.skipUnless(PANDOC, "Pandoc required")
class LayoutFilterTests(unittest.TestCase):
    def test_default_table_columns_are_bounded(self):
        text = "| A | B | C |\n|---|---|---|\n| ![](a.png) | ![](b.png) | ![](c.png) |\n"
        tex = pandoc(text)
        self.assertEqual(tex.count(r"p{(\linewidth - 4\tabcolsep)"), 3)
        self.assertEqual(tex.count(r"\includegraphics"), 3)

    def test_explicit_column_widths_are_preserved(self):
        doc = json.loads(pandoc("| A | B |\n|---|---|\n| Left | Right |\n", target="json", filtered=False))
        specs = doc["blocks"][0]["c"][2]
        specs[0][1] = {"t": "ColWidth", "c": 0.25}
        specs[1][1] = {"t": "ColWidth", "c": 0.75}
        tex = pandoc(json.dumps(doc), source="json")
        self.assertIn(r"\real{0.2500}", tex)
        self.assertIn(r"\real{0.7500}", tex)

    def test_image_runs_keep_prose_and_footnotes_in_order(self):
        text = "Before. ![](a.png) ![](b.png) After.[^n]\n\n[^n]: Note. ![](c.png) ![](d.png) Tail.\n"
        tex = pandoc(text)
        self.assertEqual(tex.count(r"\begingroup\raggedright"), 2)
        self.assertEqual(tex.count(r"\includegraphics"), 4)
        self.assertEqual(tex.count(r"\footnote"), 1)
        positions = [tex.index(s) for s in ("Before.", "a.png", "b.png", "After.", "Note.", "c.png", "d.png", "Tail.")]
        self.assertEqual(positions, sorted(positions))


@unittest.skipUnless(PANDOC and XELATEX and importlib.util.find_spec("fitz"), "Pandoc, XeLaTeX and PyMuPDF required")
class RealLayoutTests(unittest.TestCase):
    def test_images_and_running_heads_stay_in_text_area(self):
        import fitz
        import tempfile
        with tempfile.TemporaryDirectory(prefix="pipeline-layout-") as td:
            directory = Path(td)
            expected = write_layout_fixture(directory)
            for _ in range(3):
                result = subprocess.run([XELATEX, "-interaction=nonstopmode", "-file-line-error",
                                         "-halt-on-error", "-no-shell-escape", "main.tex"],
                                        cwd=directory, capture_output=True, timeout=180)
                self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", errors="replace")[-4000:])
            log = (directory / "main.log").read_text(encoding="utf-8", errors="replace")
            self.assertNotRegex(log, r"Overfull \\hbox|Missing character|(?m:^! )")
            image_count = 0
            margin = 18 / 25.4 * 72
            with fitz.open(directory / "main.pdf") as doc:
                for page in doc:
                    for image in page.get_image_info():
                        image_count += 1
                        left, _, right, _ = image["bbox"]
                        self.assertGreaterEqual(left, margin - 1)
                        self.assertLessEqual(right, page.rect.width - margin + 1)
                text = "\n".join(page.get_text() for page in doc)
                self.assertIn("Text before.", text)
                self.assertIn("Text after.", text)
                self.assertIn("Note before.", text)
                self.assertIn("Note after.", text)
            self.assertEqual(image_count, expected)
            from build_release import inspect_pdf
            technical = inspect_pdf(directory / "main.pdf", directory / "main.log")
            self.assertEqual(technical["status"], "passed", technical["errors"])
            self.assertEqual(technical["image_geometry"]["placements"], expected)


if __name__ == "__main__":
    unittest.main()
