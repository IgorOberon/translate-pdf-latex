"""Explicit retained-source decisions: no model, cache deletion or book data."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline
import translate
from provenance import atomic_text, check_result, record_edit, sha_file, sha_text, write_json
from source_preservation import select_preserved, verify_preserved, translatable_output


class SourcePreservationTests(unittest.TestCase):
    def translate_fixture(self, root, source, indices, group=0, answer="Перевод текста."):
        src, dst, policy = root / "source.md", root / "target.md", root / "keep.json"
        atomic_text(src, source)
        units = translate.group_blocks(translate.split_blocks(source), group)
        write_json(policy, {"schema": 1, "blocks": [
            {"index": index, "source_sha256": sha_text(units[index - 1]),
             "reason": "Bibliographic spelling checked against source"} for index in indices]})
        args = ["translate", str(src), str(dst), "--cache-dir", str(root / "cache"),
                "--group-chars", str(group), "--keep-source", str(policy)]
        with patch.object(sys, "argv", args), patch.object(translate, "call_api", return_value=answer) as api:
            try:
                translate.main()
                rc = 0
            except SystemExit as exc:
                rc = exc.code
        return src, dst, check_result(src, dst), rc, api.call_count

    def test_deliberate_preservation_passes_and_only_translates_other_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, report, rc, calls = self.translate_fixture(
                root, "Reference ABC 23.\n\nA sentence to translate.", [1])
            self.assertEqual((rc, calls, report["status"]), (0, 1, "passed"))
            self.assertEqual(report["preserved_source"], 1)
            self.assertEqual(report["kept_source"], 0)
            self.assertEqual(report["failed_blocks"], [])
            self.assertTrue(dst.read_text(encoding="utf-8").startswith("Reference ABC 23."))
            self.assertEqual(len(list((root / "cache").glob("*.json"))), 1)
            self.assertNotIn("Reference", translatable_output(dst.read_text(encoding="utf-8"), report))
            self.assertEqual(report["source_preservation_sha256"], sha_file(root / "keep.json"))

    def test_grouped_notes_preserved_without_api_or_cache_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, rc, calls = self.translate_fixture(
                Path(tmp), "Reference[^n].\n\n[^n]: Note body.\n\nSecond reference.", [1], group=900)
            self.assertEqual((rc, calls, report["blocks_total"]), (0, 0, 1))
            self.assertIn("\n\n[^n]: Note body.", dst.read_text(encoding="utf-8"))
            self.assertEqual(src.read_bytes(), dst.read_bytes())
            self.assertFalse(list((Path(tmp) / "cache").glob("*.json")))

    def test_unlisted_failure_still_blocks_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, report, rc, calls = self.translate_fixture(
                Path(tmp), "Reference ABC.\n\nThe measurement is 57.", [1], answer="Измерение.")
            self.assertEqual((rc, report["status"], report["kept_source"]), (4, "failed", 1))
            self.assertEqual(calls, 3)
            self.assertEqual(report["failed_blocks"][0]["index"], 2)

    def test_stale_duplicate_unreasoned_decisions_fail(self):
        row = {"index": 1, "source_sha256": sha_text("Reference."), "reason": "Checked"}
        for change in (dict(row, index=True), dict(row, index=2), dict(row, source_sha256="bad"),
                       dict(row, reason="  ")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                select_preserved(["Reference."], {"schema": 1, "blocks": [change]})
        with self.assertRaises(ValueError):
            select_preserved(["Reference."], {"schema": 1, "blocks": [row, row]})

    def test_report_span_and_source_identity_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, report, _, _ = self.translate_fixture(Path(tmp), "Reference ABC.", [1])
            source, output = src.read_text(encoding="utf-8"), dst.read_text(encoding="utf-8")
            for key, value in (("output_start", 1), ("output_end", 2), ("source_sha256", "bad")):
                bad = copy.deepcopy(report)
                bad["allowed_source"][0][key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    verify_preserved(source, output, bad)
            with self.assertRaises(ValueError):
                verify_preserved(source, "Changed source.", report)

    def test_pipeline_defaults_and_snapshot_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, policy, jobdir = root / "book.md", root / "keep.json", root / "job"
            atomic_text(src, "Reference.")
            write_json(policy, {"schema": 1, "blocks": [{"index": 1,
                "source_sha256": sha_text("Reference."), "reason": "Original spelling"}]})
            job = pipeline.initialize(src, jobdir, {"keep_source": str(policy)})
            self.assertEqual((job["settings"]["group_chars"], job["settings"]["layout"]), (900, "compact"))
            self.assertFalse(job["settings"]["polish"])
            self.assertFalse(job["settings"]["allow_remote"])
            cmd = pipeline.steps_for(jobdir, job)[0][1]
            self.assertEqual(cmd[cmd.index("--group-chars") + 1], "900")
            self.assertEqual(cmd[cmd.index("--layout") + 1], "compact")
            snapshot = jobdir / "input/keep-source.json"
            self.assertEqual(cmd[cmd.index("--keep-source") + 1], str(snapshot.resolve()))
            atomic_text(policy, "changed external policy")
            pipeline.load_job(jobdir)
            atomic_text(snapshot, "changed snapshot")
            with self.assertRaises(ValueError):
                pipeline.load_job(jobdir)
            cfg = pipeline.validate_settings({"group_chars": 0, "layout": "screen"}, "books")
            self.assertEqual((cfg["group_chars"], cfg["layout"]), (0, "screen"))

    def test_recorded_edit_outside_preserved_unit_rebases_spans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, report, _, _ = self.translate_fixture(root, "A sentence.\n\nReference ABC.", [2])
            receipt_hash = sha_file(str(dst) + ".report.json")
            result = dst.read_text(encoding="utf-8").replace("Перевод текста.", "Проверенный более длинный перевод текста.")
            record_edit(src, dst, result, [{"reason": "Review of translated prose"}])
            self.assertEqual(check_result(src, dst), report)
            self.assertEqual(sha_file(str(dst) + ".report.json"), receipt_hash)
            before = dst.read_bytes()
            lineage = Path(str(dst) + ".lineage.json").read_bytes()
            with self.assertRaises(ValueError):
                record_edit(src, dst, result.replace("Reference ABC.", "Modified reference."), [{"reason": "Invalid edit"}])
            self.assertEqual(dst.read_bytes(), before)
            self.assertEqual(Path(str(dst) + ".lineage.json").read_bytes(), lineage)

    def test_polish_combination_refused_before_output_creation(self):
        import translate_book
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(sys, "argv", ["translate_book", str(root / "missing.md"),
                              "--keep-source", str(root / "missing.json"), "--polish",
                              "--workdir", str(root / "output")]):
                with self.assertRaises(SystemExit) as exc:
                    translate_book.main()
            self.assertEqual(exc.exception.code, 2)
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
