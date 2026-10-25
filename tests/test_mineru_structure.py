"""Synthetic reproductions from real local OCR -> MT -> PDF archive testing."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_mineru as prep
from mask_math import mask, unmask
from clean_markdown import clean_text
from translate import validate_candidate, ValidationPolicy
import procutils


TABLE = "<table><tr><td>Quantity</td><td>A</td></tr><tr><td>Mass (kg)</td><td>2</td></tr></table>"
RAW = "## 1 Force\n\nText with a note.<sup>1</sup> End.\n\n" + TABLE + "\n\n## 2 Energy\n\nMore text.<sup>1</sup>\n"
ITEMS = [
    {"type": "text", "page_idx": 0, "text": "Text with a note.<sup>1</sup> End."},
    {"type": "page_footnote", "page_idx": 0, "text": "<sup>1</sup>First note with $F=ma$."},
    {"type": "text", "page_idx": 1, "text": "More text.<sup>1</sup>"},
    {"type": "page_footnote", "page_idx": 1, "text": "<sup>1</sup>Second note, not a page number."},
]


class Structure(unittest.TestCase):
    def test_restore_page_scoped_notes_and_table(self):
        text, report = prep.normalize(RAW, ITEMS, "abc")
        self.assertEqual(len(report["footnotes"]), 2)
        self.assertEqual(report["tables_converted"], 1)
        self.assertIn("[^mu-abc-p1-n1]: First note", text)
        self.assertIn("[^mu-abc-p2-n2]: Second note", text)
        self.assertLess(text.index("First note"), text.index("## 2 Energy"))
        self.assertEqual(prep.pipe_tables(text), [[["Quantity", "A"], ["Mass (kg)", "2"]]])
        self.assertNotIn("<sup>", text)

    def test_mask_and_sanitize_preserve_notes_and_table(self):
        text, _ = prep.normalize(RAW, ITEMS, "abc")
        clean = clean_text(text, {})[0]
        prep.assert_markdown_structure(text, clean)
        masked, mapping = mask(clean)
        self.assertNotIn("[^mu-", masked)
        self.assertIn("First note", masked)  # body must reach MT
        self.assertNotIn("$F=ma$", masked)
        self.assertTrue(any(v.endswith("]:") for v in mapping.values()))
        restored = unmask(masked, mapping)[0]
        self.assertEqual(restored, clean)

    def test_prepare_keeps_raw_and_writes_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.md"
            src.write_text(RAW, encoding="utf-8")
            side = src.with_name("test_content_list.json")
            side.write_text(json.dumps(ITEMS), encoding="utf-8")
            before = src.read_bytes()
            result = prep.prepare(src)
            self.assertEqual(src.read_bytes(), before)
            report = json.loads(src.with_suffix(".prepare.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["output_sha256"], prep.sha_file(result))
            self.assertEqual(prep.prepare(src).read_bytes(), result.read_bytes())

    def test_missing_sidecar_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.md"
            src.write_text(RAW, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unresolved numeric superscript"):
                prep.prepare(src)
            self.assertFalse(src.with_suffix(".prepared.md").exists())
            report = json.loads(src.with_suffix(".prepare.json").read_text())
            self.assertEqual(report["status"], "failed")

    def test_ambiguous_or_missing_notes_fail(self):
        for raw, items in ((RAW, ITEMS + [ITEMS[1]]),
                           (RAW.replace("More text.<sup>1</sup>", "More text."), ITEMS),
                           (RAW + "\nMore text.<sup>1</sup>", ITEMS),
                           (RAW, [i for i in ITEMS if i is not ITEMS[2]])):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                prep.normalize(raw, items, "abc")

    def test_already_exported_body_does_not_duplicate(self):
        with self.assertRaisesRegex(ValueError, "already in raw"):
            prep.normalize(RAW + "\nFirst note with $F=ma$.", ITEMS, "abc")

    def test_chunk_namespaces_do_not_collide(self):
        a = prep.normalize(RAW, ITEMS, "chunk1")[0]
        b = prep.normalize(RAW, ITEMS, "chunk2")[0]
        self.assertFalse(set(prep.NOTE.findall(a)) & set(prep.NOTE.findall(b)))

    def test_complex_or_broken_tables_fail(self):
        for table in (TABLE.replace("Mass (kg)", "<b>Mass</b>"),
                      TABLE.replace("</table>", "")):
            with self.subTest(table=table), self.assertRaises(ValueError):
                prep.normalize(table, [], "abc")

    def test_merged_cells_are_expanded_without_losing_text(self):
        table = TABLE.replace("<td>A", '<td colspan="2">A')
        text, _ = prep.normalize(table, [], "abc")
        grid = prep.pipe_tables(text)[0]
        self.assertEqual(grid[0][0], "Quantity")
        self.assertEqual(grid[0][1], "A")
        self.assertEqual(grid[0][2], "")     # covered by the merge, not lost
        self.assertEqual(grid[1], ["Mass (kg)", "2", ""])

    def test_short_row_is_padded_not_dropped(self):
        table = TABLE.replace("<td>2</td>", "")
        text, _ = prep.normalize(table, [], "abc")
        self.assertEqual(prep.pipe_tables(text)[0][1], ["Mass (kg)", ""])

    def test_pipe_inside_cell_is_escaped(self):
        table = TABLE.replace("Mass (kg)", "Mass | unit")
        text, _ = prep.normalize(table, [], "abc")
        self.assertIn(r"Mass \| unit", text)
        self.assertEqual(prep.pipe_tables(text)[0][1][0], r"Mass \| unit")

    def test_plain_mark_note_serves_every_mark_of_the_page(self):
        raw = "Text with a note.\\* More text.\\*\n\nNext paragraph.\n"
        items = [{"type": "text", "page_idx": 0,
                  "text": "Text with a note.\\* More text.\\*"},
                 {"type": "page_footnote", "page_idx": 0,
                  "text": "\\* Mentioned by only some authors."}]
        text, report = prep.normalize(raw, items, "abc")
        self.assertEqual(len(report["footnotes"]), 1)
        self.assertEqual(report["footnotes"][0]["references"], 2)
        self.assertEqual(text.count("[^mu-abc-p1-n1]"), 3)   # two marks + definition
        self.assertIn("[^mu-abc-p1-n1]: Mentioned by only some authors.", text)
        self.assertEqual(report["unmatched_marks"], [])

    def test_note_without_text_mark_is_kept_as_page_note(self):
        raw = "A paragraph on the page.\n\nAnother paragraph.\n"
        items = [{"type": "text", "page_idx": 0, "text": "A paragraph on the page."},
                 {"type": "text", "page_idx": 0, "text": "Another paragraph."},
                 {"type": "page_footnote", "page_idx": 0,
                  "text": "\\* Mentioned by only some authors."}]
        text, report = prep.normalize(raw, items, "abc")
        self.assertEqual(len(report["page_notes"]), 1)
        self.assertIn(r"> \* Mentioned by only some authors.", text)
        self.assertNotIn("[^mu-", text)

    def test_mark_without_note_is_reported(self):
        raw = "Text with a mark.\\*\n\nNext paragraph.\n"
        items = [{"type": "text", "page_idx": 0, "text": "Text with a mark.\\*"},
                 {"type": "text", "page_idx": 0, "text": "Next paragraph."}]
        text, report = prep.normalize(raw, items, "abc")
        self.assertEqual(report["unmatched_marks"], [{"page_idx": 0, "label": "*"}])
        self.assertIn(r"\*", text)

    def test_model_table_damage_is_rejected(self):
        src = prep.normalize(TABLE, [], "abc")[0]
        good = src.replace("Quantity", "Величина").replace("Mass (kg)", "Масса (кг)")
        self.assertTrue(validate_candidate(src, good).ok)
        for bad in (good.replace("|", ""), good.replace("---", ""),
                    good.replace("| Масса (кг) | 2 |", "| 2 | Масса (кг) |"),
                    good + "\n| extra | 2 |"):
            self.assertFalse(validate_candidate(src, bad).ok, bad)
            with self.assertRaises(ValueError):
                prep.assert_markdown_structure(src, bad)

    def test_lost_note_definition_is_rejected(self):
        text = prep.normalize(RAW, ITEMS, "abc")[0]
        with self.assertRaises(ValueError):
            prep.assert_markdown_structure(text, text.replace("[^mu-abc-p1-n1]:", ""))

    @unittest.skipUnless(shutil.which("pandoc"), "Pandoc required")
    def test_real_pandoc_notes_and_table(self):
        text = prep.normalize(RAW, ITEMS, "abc")[0]
        out = subprocess.run(["pandoc", "-f", "markdown+raw_tex", "-t", "latex"],
                             input=text, capture_output=True, text=True, encoding="utf-8", check=True).stdout
        prep.assert_typeset_structure(text, out)
        with self.assertRaisesRegex(ValueError, "footnotes lost"):
            prep.assert_typeset_structure(text, out.replace("\\footnote", "\\lost"))
        with self.assertRaisesRegex(ValueError, "table structure lost"):
            prep.assert_typeset_structure(text, out.replace("longtable", "losttable"))
        # Each skeleton section must compile with its own adjacent definitions.
        for part in text.split("## ")[1:]:
            tex = subprocess.run(["pandoc", "-f", "markdown", "-t", "latex"], input=part,
                                 capture_output=True, text=True, encoding="utf-8", check=True).stdout
            prep.assert_typeset_structure(part, tex)

    @unittest.skipUnless(shutil.which("pandoc"), "Pandoc required")
    def test_short_native_heading_numbers(self):
        lua = Path(__file__).resolve().parents[1] / "assets" / "structure.lua"
        out = subprocess.run(["pandoc", "-f", "markdown", "-t", "latex", "--lua-filter", str(lua)],
                             input="# Chapter\n\n## 1 First\n\nText.\n\n## 2 Second\n",
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout
        self.assertIn("\\setcounter{secnumdepth}{0}", out)

    @unittest.skipUnless(shutil.which("pandoc"), "Pandoc required")
    def test_unbracketed_figure_caption_glues_to_image(self):
        lua = Path(__file__).resolve().parents[1] / "assets" / "figcaption.lua"
        for caption in ("Figure 1: A particle.", "Рисунок 1: Частица.", "Figure [1] A particle."):
            for gap in ("  \n", "\n\n"):
                with self.subTest(caption=caption, gap=gap):
                    out = subprocess.run(["pandoc", "-f", "markdown-implicit_figures", "-t", "latex", "--lua-filter", str(lua)],
                                         input="![](images/a.png)" + gap + caption,
                                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
                    self.assertIn("\\FigWithCaption{images/a.png}", out)
        out = subprocess.run(["pandoc", "-f", "markdown", "-t", "latex", "--lua-filter", str(lua)],
                             input="![](a.png)  \nFigure out how it works.", capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout
        self.assertNotIn("\\FigWithCaption", out)

    def test_cleanup_does_not_match_arbitrary_python(self):
        self.assertTrue(procutils.is_mineru_command(["python.exe", "D:/venv/Scripts/mineru.exe", "-p", "a.pdf"]))
        self.assertFalse(procutils.is_mineru_command(["python.exe", "pipeline.py", "--mineru-timeout", "600"]))
        self.assertFalse(procutils.is_mineru_command(["python.exe", "-c", "from multiprocessing.spawn import spawn_main"]))

    def test_handoff_waits_for_owned_children(self):
        import psutil
        p = Mock(pid=999999)
        with patch.object(procutils, "own_lineage", return_value={123}), \
                patch.object(psutil, "wait_procs", return_value=([], [p])):
            with self.assertRaisesRegex(RuntimeError, "handoff refused"):
                procutils.stop_owned_processes([p], timeout=0)
            p.kill.assert_called_once()

    def test_lms_still_loaded_blocks_extraction(self):
        from types import SimpleNamespace
        with patch.object(procutils.shutil, "which", return_value=__file__), \
                patch.object(procutils.subprocess, "run", side_effect=[
                    SimpleNamespace(returncode=0),
                    SimpleNamespace(returncode=0, stdout='[{"identifier":"other-model"}]')]):
            with self.assertRaisesRegex(RuntimeError, "still has loaded"):
                procutils.unload_llm()


if __name__ == "__main__":
    unittest.main()
