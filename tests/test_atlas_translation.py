"""Portable regressions derived from Atlas metadata, never writable book fixtures.

Block numbers are 1-based report indices. 1171 has a preserved request-v5 cache
response (997641d...): 2nd/3rd and 4th/5th became 2/3 and 4/5. 817 has only the
source/request and failed-attempt reasons; its response was not retained. Its
tests therefore cover that source's numeric pattern with synthetic translations,
not an unverified claim to replay the historical failed response.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_mineru as prep
from mask_math import mask, unmask
from translate import group_blocks, split_blocks, validate_candidate


class AtlasNumbers(unittest.TestCase):
    def assert_numbers_rejected(self, source, target):
        reasons = validate_candidate(source, target).reasons
        self.assertTrue(any("numbers changed" in reason for reason in reasons), reasons)

    def test_block_1171_retained_ordinal_pair_becomes_bare_digits(self):
        # Exact numeric-bearing excerpts of the retained source and response.
        source = ("Ex-UE-7 ‘Lumbar Pair Point Two points on the dorsum of the hand, between the "
                  "2nd/3rd and 4th/5th metacarpal bones. S.I.-4 between the "
                  "5th metacarpal and the carpal bones. L.I.-5 T.B.-4 S.I.-5")
        target = ("Ex-UE-7 «Пара точек поясничной зоны». Два пункта на тыльной стороне кисти, между 2/3 и 4/5 "
                  "пальцевых костей. S.I.-4 между 5-й пальцевой костью и кости "
                  "запястья. L.I.-5 T.B.-4 S.I.-5")
        self.assertTrue(validate_candidate(source, target).ok)
        self.assert_numbers_rejected(source, target.replace("2/3", "2"))
        self.assert_numbers_rejected(source, target.replace("2/3", "3"))
        self.assert_numbers_rejected(source, target.replace("4/5", "4/6"))

    def test_block_817_source_pattern_decimal_localization(self):
        source = ("G.B.-14 is on the pupil line. The distance (3.1.1) is 3 cun "
                  "(2.2). G.B.-14 is one third of the distance or 1 cun above. "
                  "G.B.-14 is on the pupil line. Needling: 0.3–1 cun.")
        target = ("G.B.-14 находится на линии зрачка. Расстояние (3.1.1) равно "
                  "3 цуня (2.2). G.B.-14 расположена на одной трети расстояния "
                  "или на 1 цунь выше. G.B.-14 на линии зрачка. "
                  "Введение иглы: 0,3–1 цуня.")
        self.assertTrue(validate_candidate(source, target).ok)
        for broken in (target.replace("0,3", "0,4"),
                       target.replace("(3.1.1)", "(3.1)"),
                       target.replace("на 1 цунь", "на цунь"),
                       target.replace("G.B.-14", "Точка", 1)):
            self.assert_numbers_rejected(source, broken)

    def test_repeated_measurement_is_not_a_free_omission(self):
        self.assert_numbers_rejected("Insert 3 mm. Then move 3 mm.",
                                     "Введите на 3 мм. Затем переместите.")

    def test_repeated_point_code_is_not_a_free_omission(self):
        self.assert_numbers_rejected("HE-8 is here. HE-8 is on the hand.",
                                     "HE-8 расположена здесь. Точка на кисти.")

    def test_number_word_needs_one_evidence_per_occurrence(self):
        self.assertTrue(validate_candidate("Use 3 samples and 3 controls.",
                                           "Используйте 3 образца и три контроля.").ok)
        self.assert_numbers_rejected("Use 3 samples, 3 controls and 3 repeats.",
                                     "Используйте 3 образца и три контроля.")

    def test_preexisting_word_does_not_hide_missing_digit(self):
        self.assert_numbers_rejected("One sample is at 1 cm.",
                                     "Один образец находится рядом.")
        self.assert_numbers_rejected("One third of the samples need 3 controls.",
                                     "Одной трети образцов нужны контроли.")
        self.assertTrue(validate_candidate("One third of the samples need 3 controls.",
                                           "Одной трети образцов нужны три контроля.").ok)

    def test_ordinal_words_preserve_value_and_multiplicity(self):
        self.assertTrue(validate_candidate("Between the 2nd and 3rd bones.",
                                           "Между второй и третьей костями.").ok)
        self.assert_numbers_rejected("Between the 2nd and 3rd bones.",
                                     "Между второй и четвёртой костями.")
        self.assert_numbers_rejected("Between the 2nd and 3rd bones.",
                                     "Между второй и соседней костями.")

    def test_decimal_cannot_be_replaced_by_component_number_words(self):
        self.assert_numbers_rejected("The distance is 1.3 cm.",
                                     "Расстояние — один и три сантиметра.")

    def test_actual_list_marker_may_disappear_but_sentence_number_may_not(self):
        self.assertTrue(validate_candidate("1. First step\n2. Next step",
                                           "Первый шаг. Следующий шаг.").ok)
        self.assert_numbers_rejected("See section 12. Next instruction.",
                                     "См. раздел. Следующая инструкция.")
        self.assert_numbers_rejected("Figure 1. Then see Figure 2.",
                                     "Рисунок. Затем см. рисунок.")


class AtlasGrouping(unittest.TestCase):
    def test_grouping_reconstructs_every_paragraph_and_mask(self):
        blocks = ["# Heading", "Caption", "![](⟦M000001⟧)", "Body " * 100,
                  "[^n]: Note body", "Last paragraph"]
        original = list(blocks)
        grouped = group_blocks(blocks, 900)
        self.assertEqual(blocks, original)
        self.assertEqual(split_blocks("\n\n".join(grouped)), blocks)
        self.assertLess(len(grouped), len(blocks))
        self.assertTrue(all(len(unit) <= 900 for unit in grouped))

    def test_tables_are_barriers_and_oversized_paragraphs_stay_intact(self):
        table = "| Name | Value |\n| --- | --- |\n| Mass | 3 |"
        long = "Indivisible paragraph " * 60
        grouped = group_blocks(["First", table, "Next", long, "Last"], 900)
        self.assertIn(table, grouped)
        self.assertIn(long, grouped)
        self.assertEqual(split_blocks("\n\n".join(grouped)),
                         ["First", table, "Next", long, "Last"])

    def test_disabled_grouping_preserves_units(self):
        blocks = ["A", "B", "C"]
        self.assertEqual(group_blocks(blocks, 0), blocks)
        self.assertEqual(group_blocks(blocks, -1), blocks)


class AtlasFootnotes(unittest.TestCase):
    def fixture(self):
        first, second = r"First page\* and another mark\*.", r"Second page\*."
        raw = first + "\n\n" + second
        items = [{"type": "text", "page_idx": 0, "text": first},
                 {"type": "page_footnote", "page_idx": 0,
                  "text": r"\* First page note with $x=1$."},
                 {"type": "text", "page_idx": 1, "text": second},
                 {"type": "page_footnote", "page_idx": 1,
                  "text": r"\* Second page note."}]
        return prep.normalize(raw, items, "fixture")

    def test_repeated_plain_marks_remain_scoped_to_their_page(self):
        text, report = self.fixture()
        refs, defs = prep.note_parts(text)
        self.assertEqual(refs, {"mu-fixture-p1-n1": 2, "mu-fixture-p2-n2": 1})
        self.assertEqual(defs, {"mu-fixture-p1-n1": 1, "mu-fixture-p2-n2": 1})
        self.assertEqual(report["unmatched_marks"], [])
        masked, mapping = mask(text)
        self.assertEqual(unmask(masked, mapping)[0], text)

    def test_inline_reference_followed_by_colon_is_not_definition(self):
        text = "Paragraph[^n]: explanation.\n\n[^n]: Note body."
        self.assertEqual(prep.note_parts(text), ({"n": 1}, {"n": 1}))
        with self.assertRaisesRegex(ValueError, "footnote"):
            prep.assert_markdown_structure(text, "Paragraph[^n]: explanation.")

    def test_same_note_body_in_prose_does_not_suppress_note(self):
        para = r"According to some authors, a reference\* follows."
        items = [{"type": "text", "page_idx": 0, "text": para},
                 {"type": "page_footnote", "page_idx": 0,
                  "text": r"\* According to some authors"}]
        text, report = prep.normalize(para, items, "fixture")
        self.assertEqual(len(report["footnotes"]), 1)
        self.assertIn(": According to some authors", text)

    def test_figure_only_page_keeps_note_at_neighbor(self):
        raw = "![](images/figure.png)\n\nFollowing page text."
        items = [{"type": "page_footnote", "page_idx": 0,
                  "text": r"\* This mark lives inside the figure."},
                 {"type": "text", "page_idx": 1, "text": "Following page text."}]
        text, report = prep.normalize(raw, items, "fixture")
        self.assertIn(r"> \* This mark lives inside the figure.", text)
        self.assertEqual(report["page_notes"][0]["anchor_page_idx"], 1)

    @unittest.skipUnless(shutil.which("pandoc"), "Pandoc required")
    def test_repeated_notes_survive_real_pandoc(self):
        text, _ = self.fixture()
        tex = subprocess.run(["pandoc", "-f", "markdown", "-t", "latex"],
                             input=text, capture_output=True, text=True,
                             encoding="utf-8", check=True).stdout
        prep.assert_typeset_structure(text, tex)
        self.assertEqual(tex.count("\\footnote{"), 3)


if __name__ == "__main__":
    unittest.main()
