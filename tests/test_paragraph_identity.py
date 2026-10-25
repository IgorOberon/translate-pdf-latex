"""Synthetic grouped-MT regressions: no model, finished book or shared cache."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import paragraph_units as pu
import translate as mt
from mask_math import mask, unmask
from provenance import atomic_text, check_result, record_edit, sha_file, write_json


def reply_text(messages):
    text = messages[-1]["content"]
    marker = pu.MARKER.search(text)
    return text[marker.start():] if marker else text


class ParagraphIdentity(unittest.TestCase):
    def plan(self, source, split=0):
        rows = pu.source_paragraphs(mt.split_blocks(source))
        return rows, pu.plan_group(rows, split)

    def translate(self, root, source, response=None, extra=()):
        src, dst = root / "source.md", root / "target.md"
        atomic_text(src, source)
        args = ["translate", str(src), str(dst), "--cache-dir", str(root / "cache"),
                "--group-chars", "900", *extra]

        def answer(host, model, messages, temperature, top_p):
            request = reply_text(messages)
            return response(request) if response else request.replace("First", "Первый").replace(
                "Second", "Второй").replace("paragraph", "абзац").replace("sentence", "предложение")

        with patch.object(sys, "argv", args), patch.object(mt, "call_api", side_effect=answer) as api:
            try:
                mt.main()
                rc = 0
            except SystemExit as exc:
                rc = exc.code
        report = json.loads(Path(str(dst) + ".report.json").read_text(encoding="utf-8"))
        return src, dst, report, rc, api

    def test_group_mock_translate_render_and_provenance_preserve_two_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, rc, api = self.translate(Path(tmp), "First paragraph.\n\nSecond paragraph.")
            self.assertEqual((rc, api.call_count, report["blocks_total"]), (0, 1, 1))
            self.assertEqual(dst.read_text(encoding="utf-8"), "Первый абзац.\n\nВторой абзац.")
            self.assertEqual(len(report["paragraph_coverage"]), 2)
            self.assertEqual(check_result(src, dst), report)
            self.assertNotIn("⟦P", dst.read_text(encoding="utf-8"))

    def test_source_occurrences_have_stable_distinct_ids_before_grouping(self):
        rows = pu.source_paragraphs(["Same text.", "Same text.", "Last text."])
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual(rows[0]["source_sha256"], rows[1]["source_sha256"])
        for size in (10, 900):
            groups = pu.assign_groups(mt.group_blocks([r["text"] for r in rows], size), rows)
            self.assertEqual([p["id"] for g in groups for p in g], [p["id"] for p in rows])

    def test_missing_duplicate_empty_reordered_and_merged_paragraphs_rejected(self):
        _, planned = self.plan("First paragraph.\n\nSecond paragraph.")
        fragment, parts = planned[0], planned[0]["paragraphs"]
        corrupt = [pu.encode(parts[:1]), pu.encode(parts + parts[:1]),
                   pu.encode(parts, ["Первый абзац.", ""]),
                   pu.encode(list(reversed(parts))),
                   pu.encode(parts, ["Первый абзац. Второй абзац.", ""]),
                   "Here is the translation:\n" + pu.encode(parts),
                   pu.encode(parts).rsplit("⟦/", 1)[0]]
        for text in corrupt:
            with self.subTest(text=text):
                self.assertFalse(mt.validate_fragment(fragment, text).ok)

    def test_dropped_paragraph_cannot_be_cached_or_published_as_success(self):
        source = "First paragraph.\n\nSecond paragraph."

        def dropped(request):
            end = next(m.end() for m in pu.MARKER.finditer(request) if m[1] == "/")
            return request[:end].replace("First", "Первый")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, report, rc, api = self.translate(root, source, dropped)
            self.assertEqual((rc, api.call_count, report["kept_source"]), (4, 3, 1))
            self.assertEqual(dst.read_text(encoding="utf-8"), source)
            self.assertFalse(list((root / "cache").glob("*.json")))
            self.assertEqual(check_result(src, dst), report)

    def test_numbers_cannot_migrate_between_paragraphs(self):
        _, planned = self.plan("First result is 42.\n\nSecond result is 57.")
        fragment = planned[0]
        migrated = pu.encode(fragment["paragraphs"], ["Первый результат 57.", "Второй результат 42."])
        self.assertTrue(mt.validate_candidate("First result is 42.\n\nSecond result is 57.",
                                              "Первый результат 57.\n\nВторой результат 42.").ok)
        self.assertFalse(mt.validate_fragment(fragment, migrated).ok)

    def test_masks_cannot_migrate_between_paragraphs(self):
        _, planned = self.plan("First formula ⟦M000001⟧.\n\nSecond formula.")
        fragment = planned[0]
        migrated = pu.encode(fragment["paragraphs"], ["Первая формула.", "Вторая формула ⟦M000001⟧."])
        self.assertFalse(mt.validate_fragment(fragment, migrated).ok)

    def test_structural_boundaries_are_not_crossed_by_requests(self):
        source = ["Before text.", "# Heading", "After text.", "[^n]: Note body.",
                  "Following text.", "| Name |\n| --- |\n| Value |", "Last text.",
                  "⟦M000123⟧ Masked note body.", "Final text."]
        rows, plan = self.plan("\n\n".join(source))
        self.assertEqual([p["text"] for f in plan for p in f["paragraphs"]], source)
        for fragment in plan:
            if any(pu.is_barrier(p["text"]) for p in fragment["paragraphs"]):
                self.assertEqual(len(fragment["paragraphs"]), 1)

    def test_long_prose_after_heading_does_not_inherit_heading_length_exemption(self):
        source = "# Heading\n\n" + "A long informative sentence. " * 30
        policy = mt.ValidationPolicy(min_ratio=0.8)
        self.assertIn("too short", mt.validate_candidate(source, "# Заголовок\n\nКратко.", policy).reasons)
        rows, plan = self.plan(source)
        self.assertEqual(len(plan), 2)
        short = pu.encode(plan[1]["paragraphs"], ["Кратко."])
        self.assertTrue(any("too short" in reason for reason in mt.validate_fragment(plan[1], short, policy).reasons))

    def test_split_fragments_reassemble_into_their_original_paragraph(self):
        source = "First sentence. Second sentence. First sentence.\n\nSecond paragraph."
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, rc, api = self.translate(Path(tmp), source, extra=("--split-chars", "20"))
            self.assertEqual(rc, 0)
            self.assertGreater(api.call_count, 2)
            self.assertEqual(len(mt.split_blocks(dst.read_text(encoding="utf-8"))), 2)
            self.assertEqual(check_result(src, dst), report)

    def test_cached_protocol_replies_are_revalidated_and_boundaries_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = "First paragraph.\n\nSecond paragraph."
            self.translate(root, source)
            src, dst, report, rc, api = self.translate(root, source)
            self.assertEqual((rc, api.call_count, report["from_cache"]), (0, 0, 1))
            self.assertEqual(len(report["paragraph_coverage"]), 2)
            self.assertEqual(check_result(src, dst), report)

    def test_repeated_identical_paragraphs_remain_two_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, rc, api = self.translate(Path(tmp), "First paragraph.\n\nFirst paragraph.")
            self.assertEqual((rc, api.call_count), (0, 1))
            self.assertEqual(dst.read_text(encoding="utf-8"), "Первый абзац.\n\nПервый абзац.")
            rows = report["paragraph_coverage"]
            self.assertEqual(rows[0]["output_sha256"], rows[1]["output_sha256"])
            self.assertNotEqual(rows[0]["id"], rows[1]["id"])
            self.assertEqual(check_result(src, dst), report)

    def test_recorded_prose_edit_retains_coverage_and_merge_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, _, _ = self.translate(Path(tmp), "First paragraph.\n\nSecond paragraph.")
            receipt = sha_file(str(dst) + ".report.json")
            edited = dst.read_text(encoding="utf-8").replace("Первый", "Дополнительно вычитанный первый")
            record_edit(src, dst, edited, [{"reason": "synthetic prose edit"}])
            self.assertEqual(check_result(src, dst), report)
            self.assertEqual(sha_file(str(dst) + ".report.json"), receipt)
            before, chain = dst.read_bytes(), Path(str(dst) + ".lineage.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "paragraph alignment"):
                record_edit(src, dst, edited.replace("\n\n", " "), [{"reason": "merge"}])
            self.assertEqual(dst.read_bytes(), before)
            self.assertEqual(Path(str(dst) + ".lineage.json").read_bytes(), chain)

    def test_corrupt_coverage_rejected_even_when_output_and_request_hashes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, _, _ = self.translate(Path(tmp), "First paragraph.\n\nSecond paragraph.")
            path = Path(str(dst) + ".report.json")
            for rows in (report["paragraph_coverage"][:1], list(reversed(report["paragraph_coverage"]))):
                bad = dict(report, paragraph_coverage=rows)
                write_json(path, bad)
                with self.assertRaises(ValueError):
                    check_result(src, dst)
            bad = copy.deepcopy(report)
            bad["paragraph_coverage"][1]["source_index"] = 1
            write_json(path, bad)
            with self.assertRaises(ValueError):
                check_result(src, dst)

    def test_grouped_note_boundaries_survive_mask_translation_and_unmask(self):
        source = "First paragraph[^n].\n\n[^n]: Second paragraph."
        masked, mapping = mask(source)
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, rc, api = self.translate(Path(tmp), masked)
            self.assertEqual((rc, api.call_count), (0, 2))
            text, _, _, unresolved = unmask(dst.read_text(encoding="utf-8"), mapping)
            self.assertEqual(unresolved, 0)
            self.assertIn("\n\n[^n]: Второй абзац.", text)
            self.assertEqual(check_result(src, dst), report)

    def test_group_zero_keeps_existing_request_identity_config(self):
        args = SimpleNamespace(host="http://localhost:1234", source_lang="English",
            target_lang="Russian", model="hy-mt2-7b", mode="hymt", think="off",
            split_chars=0, group_chars=0, temperature=0.7, top_p=0.6)
        config = mt.request_config(args, "")
        self.assertNotIn("paragraph_protocol", config)
        block = "First paragraph."
        self.assertEqual(mt.plan_translation_fragments(block), mt.plan_fragments(block))
        self.assertNotIn(pu.RULE, mt.build_messages(block, "hymt", "Russian", "")[0]["content"])
        args.group_chars = 900
        grouped = mt.request_config(args, "")
        self.assertEqual(grouped["paragraph_protocol"], pu.PROTOCOL)
        self.assertNotEqual(config, grouped)


if __name__ == "__main__":
    unittest.main()
