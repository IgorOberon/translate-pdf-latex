"""Small reproductions of volume-6 losses; never calls a live model."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
import copy
import io
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import translate as mt
import assemble_ru_book as assembly
import provenance as pv
import build_release as release
import build_ocr_md as ocr
import accept_fraktur as acceptance
import postfix_ru as postfix
import run_doc_translate as run_doc
from request_plan import plan_fragments
from editorial import reference_findings, apply_corrections, terminology_findings
from glossary_selection import select_glossary


def book_fixture(root, sources=("A source.",), targets=("Перевод.",)):
    pv.atomic_text(root / "book.md", "\n\n".join(sources))
    for i, source in enumerate(sources, 1):
        pv.atomic_text(root / f"book.part{i}.md", source)
    manifest = pv.create_manifest(root)
    for row, target in zip(manifest["parts"], targets):
        dst = root / row["output"]
        pv.atomic_text(dst, target)
        config = {"model": "mock", "source_lang": "English"}
        pv.write_json(str(dst) + ".report.json", {
            "schema": 1, "status": "passed", "source_sha256": row["sha256"],
            "output_sha256": pv.sha_file(dst), "request_config": config,
            "request_config_sha256": pv.canonical_hash(config), "blocks_total": 1,
            "manifest_sha256": manifest["manifest_sha256"], "kept_source": 0,
            "failed_blocks": [], "aborted": False})
    return manifest


class TranslationIntegrity(unittest.TestCase):
    def test_term_guard_rejects_before_compile_and_keeps_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pv.write_json(root / "structure.json", {"term_guards": [{
                "id": "contrast", "source_pattern": "source term",
                "required_target_pattern": "верный термин"}]})
            book_fixture(root, ("The source term is here.",), ("Здесь неверный перевод.",))
            pdf = root / "published.pdf"
            pdf.write_bytes(b"previous published bytes")
            with patch.object(sys, "argv", ["assemble", str(root), "--pdf", str(pdf)]), patch.object(assembly, "compile_latex") as compile_mock:
                with self.assertRaises(SystemExit) as caught:
                    assembly.main()
                self.assertIn("source/term review failed", str(caught.exception))
                compile_mock.assert_not_called()
            self.assertEqual(pdf.read_bytes(), b"previous published bytes")

    def run_translation(self, source, answer, cached=None, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, cache = root / "src.md", root / "dst.md", root / "cache"
            src.write_text(source, encoding="utf-8")
            cache.mkdir()
            if cached is not None:
                key = hashlib.sha256(("v3hymthy-mt2-7bRussian" + source).encode()).hexdigest()
                (cache / (key + ".txt")).write_text(cached, encoding="utf-8")
            args = ["translate.py", str(src), str(dst), "--cache-dir", str(cache),
                    "--min-ratio", "0.8", *extra]
            with patch.object(sys, "argv", args), patch.object(mt, "call_api", return_value=answer) as api:
                try:
                    mt.main()
                    rc = 0
                except SystemExit as exc:
                    rc = exc.code
            report = json.loads(Path(str(dst) + ".report.json").read_text(encoding="utf-8"))
            return rc, report, api.call_count

    def test_T01_short_replies_are_errors(self):
        rc, report, _ = self.run_translation("A long meaningful sentence. " * 20, "Кратко.")
        self.assertEqual(rc, 4)
        self.assertEqual(report["kept_source"], 1)

    def test_T02_short_legacy_cache_not_trusted(self):
        rc, report, calls = self.run_translation("A long meaningful sentence. " * 20, "Кратко.", cached="Кратко.")
        self.assertEqual(rc, 4)
        self.assertEqual(report["from_cache"], 0)
        self.assertGreater(calls, 0)

    def test_T03_repair_is_not_a_quality_bypass(self):
        rc, _, _ = self.run_translation("A long meaningful sentence. " * 20 + "⟦M000001⟧", "Кратко. ⟨M000001⟩")
        self.assertEqual(rc, 4)
        self.assertFalse(mt.validate_candidate("Formula ⟦M000001⟧", "⟨M000001⟩ /no_think").ok)

    def test_new_cache_is_revalidated_under_stricter_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst = root / "src.md", root / "dst.md"
            pv.atomic_text(src, "A sufficiently long sentence about mechanics. " * 20)
            args = ["translate", str(src), str(dst), "--cache-dir", str(root / "cache")]
            with patch.object(sys, "argv", args), patch.object(mt, "call_api", return_value="Кратко."):
                mt.main()
            cache = next((root / "cache").glob("*.json"))
            before = cache.read_bytes()
            with patch.object(sys, "argv", args + ["--min-ratio", ".8"]), patch.object(mt, "call_api", return_value="Кратко.") as api:
                with self.assertRaises(SystemExit) as exc:
                    mt.main()
                self.assertEqual(exc.exception.code, 4)
                self.assertEqual(api.call_count, 3)
            self.assertEqual(cache.read_bytes(), before)
            self.assertEqual(json.loads(Path(str(dst) + ".report.json").read_text(encoding="utf-8"))["from_cache"], 0)

    def test_explicit_numbering_restart(self):
        items = [{"kind": "para", "text": f"§ {n}.", "paranum": n} for n in [1, 2, 1, 2]]
        result, bad = ocr.filter_paragraph_numbers(items, expected=[1, 2, 1, 2])
        self.assertEqual(bad, 0)
        self.assertEqual([r["paranum"] for r in result], [1, 2, 1, 2])

    def test_page_mark_does_not_precede_chapter_break(self):
        md = assembly.to_markdown(["<!--1-->", "# Heading", "## Subheading", "Body.", "<!--2-->"])
        self.assertLess(md.index("# Heading"), md.index("marginpar"))
        self.assertLess(md.index("## Subheading"), md.index("marginpar"))
        self.assertEqual(md.count("marginpar"), 2)

    def test_T04_missing_clean_mask_rejected(self):
        rc, _, _ = self.run_translation("Formula ⟦M000001⟧.", "Формула.")
        self.assertEqual(rc, 4)
        for answer in ("⟦M000002⟧", "⟦M000001⟧ ⟦M000001⟧"):
            self.assertFalse(mt.validate_candidate("⟦M000001⟧", answer).ok)

    def test_T05_request_identity_and_policy(self):
        a = SimpleNamespace(host="http://localhost:1234", source_lang="English",
            target_lang="Russian", model="mock", mode="hymt", think="off",
            split_chars=900, temperature=.7, top_p=.6)
        config = mt.request_config(a, "")
        original = pv.canonical_hash(config)
        a.source_lang = "German"
        self.assertNotEqual(original, pv.canonical_hash(mt.request_config(a, "")))
        a.source_lang = "English"
        with patch.object(mt, "TOKEN_RETRY_HINT", "changed"):
            self.assertNotEqual(original, pv.canonical_hash(mt.request_config(a, "")))
        self.assertNotEqual(mt.request_identity("text", config, [], []),
                            mt.request_identity("text", config, [{"start": 0}], []))
        source = "A sentence about a physical law. " * 20
        self.assertTrue(mt.validate_candidate(source, "Кратко.", mt.ValidationPolicy()).ok)
        self.assertFalse(mt.validate_candidate(source, "Кратко.", mt.ValidationPolicy(.8)).ok)

    def test_T06_controls_not_ordinary_words(self):
        self.assertFalse(mt.validate_candidate("# Heading", "# /без_рассуждений").ok)
        self.assertTrue(mt.validate_candidate("What can we think?", "Что можно думать?").ok)
        self.assertTrue(mt.validate_candidate("Quote /no_think", "Цитата /no_think").ok)
        self.assertFalse(mt.validate_candidate("Quote /no_think", "Цитата /no_think /no_think").ok)

    def test_T07_source_aware_CJK_across_stages(self):
        src = "The name 王 is quoted."
        for target, valid in (("Цитируется имя 王.", True), ("Имя 王 и 新增.", False)):
            self.assertEqual(mt.validate_candidate(src, target).ok, valid)
            rep = acceptance.Report()
            acceptance.check_translation_hygiene([(src, target)], rep)
            self.assertEqual(not rep.gates, valid)
        rep = acceptance.Report()
        acceptance.check_translation_hygiene([("§ 1.", "§\u00a01.")], rep)
        self.assertFalse(rep.gates)

    def test_T08_planner_protected_boundaries(self):
        for protected in ("z. B.", "d. h.", "u. s. w.", "S. 33.", "J. W. Goethe", "3.14", "т. е.", "⟦M000001⟧", "«Two. Sentences.»"):
            source = "An opening sentence. " + protected + " ist hier wichtig. The end."
            parts = plan_fragments(source, 7)
            self.assertEqual("".join(p["text"] for p in parts), source)
            start = source.index(protected)
            self.assertTrue(all(not start < p["end"] < start + len(protected) for p in parts))
            self.assertTrue(all(source[p["start"]:p["end"]] == p["text"] for p in parts))

    def test_T09_oversized_is_not_hidden_from_budget(self):
        source = "A very long indivisible sentence " * 60
        plan = plan_fragments(source, 100)
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan[0]["oversized"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_fixture(root, (source,))
            self.assertEqual(run_doc.longest_block(root, 100), len(source.strip()))
            ok, lines = run_doc.check_context(root, None, "mock", 99, 100, context_per_request=100)
            self.assertFalse(ok)
            self.assertIn(str(len(source.strip())), lines[0])

    def test_T10_paragraph_not_page_or_year(self):
        with patch.object(ocr, "PARAGRAPH_NUMBERS", True):
            self.assertEqual(ocr.paragraph_number("s. 86."), 86)
            self.assertEqual(ocr.paragraph_number("8. 243."), 243)
            self.assertIsNone(ocr.paragraph_number("33"))
            self.assertIsNone(ocr.paragraph_number("1843"))
            self.assertIsNone(ocr.paragraph_number("§ 1843."))

    def test_T11_structural_barriers(self):
        for kind, text in (("head", "Heading"), ("para", "*) eine Fußnote"), ("para", "Zusatz. Mehr"), ("para", "§ 2.")):
            items = [{"kind": "para", "text": "unfinished thought", "page": 1},
                     {"kind": kind, "text": text, "page": 1},
                     {"kind": "para", "text": "continuation", "page": 2}]
            self.assertEqual(len(ocr.merge_split_paragraphs(items)), 3)

    def test_T12_references_diagnose_without_inventing(self):
        text = "vgl. folg. § 3; S. 54; § 999"
        findings = reference_findings(text, {1, 2})
        self.assertEqual([r["kind"] for r in findings], ["next_paragraph_uncertain", "page", "internal"])
        self.assertEqual(findings[-1]["status"], "warning")
        self.assertEqual(findings[1]["literal"], "S. 54")

    def test_T13_rubric_dedup_preserves_date_and_prose(self):
        out = [{"kind": "para", "text": text} for text in ("Berlin 1843", "Die Objektivität ist nicht nur ein Wort.", "А. Objektivität.")]
        ocr._drop_title_fragments(out, len(out), "A. Objektivität.")
        self.assertEqual([i["text"] for i in out], ["Berlin 1843", "Die Objektivität ist nicht nur ein Wort."])
        ocr._drop_title_fragments(out, len(out), "A. Objektivität.")
        self.assertEqual(len(out), 2)

    def test_T14_equal_count_foreign_report_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "book.part1.md").write_text("Different source", encoding="utf-8")
            (root / "book.ru.part1.md").write_text("Другой перевод", encoding="utf-8")
            (root / "book.ru.part1.md.report.json").write_text(json.dumps({
                "blocks_total": 1, "kept_source": 0, "failed_blocks": [], "aborted": False,
                "source_sha256": "not-this-source", "output_sha256": "not-this-output"}), encoding="utf-8")
            self.assertFalse(assembly.check_reports(tmp, 1)[0])

    def test_T14_current_report_detects_same_count_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_fixture(root)
            self.assertTrue(assembly.check_reports(tmp, 1)[0])
            pv.atomic_text(root / "book.ru.part1.md", "Другой перевод.")
            self.assertFalse(assembly.check_reports(tmp, 1)[0])

    def test_T15_parts_identity(self):
        for change in ("extra", "gap", "reordered", "book_changed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                book_fixture(root, ("First.", "Second."), ("Первый.", "Второй."))
                if change == "extra":
                    pv.atomic_text(root / "book.part3.md", "Tail")
                elif change == "gap":
                    (root / "book.part1.md").unlink()
                elif change == "reordered":
                    pv.atomic_text(root / "book.part1.md", "Second.")
                    pv.atomic_text(root / "book.part2.md", "First.")
                else:
                    pv.atomic_text(root / "book.md", "Different.\n\nSecond.")
                with self.assertRaises((ValueError, OSError)):
                    pv.load_manifest(root)

    def test_T16_postedit_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_fixture(root)
            src, dst = root / "book.part1.md", root / "book.ru.part1.md"
            report = pv.sha_file(str(dst) + ".report.json")
            pv.record_edit(src, dst, "Редактура.", [{"reason": "review", "before": "Перевод.", "after": "Редактура."}])
            pv.check_result(src, dst)
            self.assertEqual(pv.sha_file(str(dst) + ".report.json"), report)
            pv.atomic_text(dst, "Untracked")
            with self.assertRaises(ValueError):
                pv.check_result(src, dst)

    def test_T17_zero_exit_without_report_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_fixture(root)
            (root / "book.ru.part1.md.report.json").unlink()
            with patch.object(sys, "argv", ["run_doc_translate", tmp]), patch.object(run_doc.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                self.assertEqual(run_doc.main(), 4)
            self.assertEqual(json.loads((root / "translation.run.json").read_text(encoding="utf-8"))["status"], "failed")

    def test_T18_failed_compile_cannot_publish_old_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main, final = root / "main.tex", root / "published.pdf"
            pv.atomic_text(main, "bad TeX")
            final.write_bytes(b"previous book")
            compiled = release.compile_latex(main, root / "candidate", runner=lambda *a, **k: SimpleNamespace(returncode=1))
            self.assertEqual(compiled["status"], "failed")
            self.assertEqual(final.read_bytes(), b"previous book")
            (root / "candidate" / "main.pdf").write_bytes(b"old candidate")
            self.assertEqual(release.compile_latex(main, root / "candidate")["status"], "failed")

    def test_T19_unaccepted_candidate_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate, final = root / "candidate.pdf", root / "published.pdf"
            candidate.write_bytes(b"candidate")
            final.write_bytes(b"previous")
            for status in ("failed", "not_run", "warning"):
                with self.assertRaises(ValueError):
                    release.publish_candidate(candidate, final, {"status": status, "profile": "release", "pdf_sha256": pv.sha_file(candidate)})
            self.assertEqual(final.read_bytes(), b"previous")
            self.assertTrue(candidate.exists())
            rep = acceptance.Report()
            acceptance.check_build(root, rep, required=True)
            self.assertTrue(rep.gates)

    def test_T20_unstable_aux_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pv.atomic_text(root / "main.tex", "test")
            calls = []
            def runner(*args, **kwargs):
                calls.append(1)
                build = Path(next(arg.split("=", 1)[1] for arg in args[0]
                                  if arg.startswith("-output-directory=")))
                (build / "main.pdf").write_bytes(b"candidate")
                pv.atomic_text(build / "main.log", "compiled")
                pv.atomic_text(build / "main.aux", str(len(calls)))
                return SimpleNamespace(returncode=0)
            report = release.compile_latex(root / "main.tex", runner=runner, max_passes=3)
            self.assertEqual(len(calls), 3)
            self.assertEqual(report["status"], "failed")

    def test_T21_T22_no_mass_terminology_or_gender_substitutions(self):
        source = "Begriff Vorstellen darstellen Vernunst beseitigen"
        target = "Логическое умозаключение, математическое умозаключение, третье умозаключение; через рефлексию; это тождество."
        rules = [{"name": "contrast", "anchor": "Begriff", "competitor": "Vorstellen|darstellen", "pairs": {"умозаключение": "понятие"}}]
        self.assertTrue(terminology_findings(source, target, rules)[0]["ambiguous"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_fixture(root, (source,), (target,))
            with patch.object(sys, "argv", ["postfix_ru", tmp]), patch.object(postfix, "fix_terms", side_effect=AssertionError("unsafe substitution")), patch.object(postfix, "apply_gender_rules", side_effect=AssertionError("unsafe gender")):
                self.assertEqual(postfix.main(), 0)
            self.assertEqual((root / "book.ru.part1.md").read_text(encoding="utf-8"), target)

    def test_T23_checked_corrections_idempotent(self):
        source = "An exact anchor."
        rule = {"id": "r1", "stage": "postprocess", "source_sha256": pv.sha_text(source),
                "source_anchor": "exact anchor", "old": "Было.", "new": "Стало.", "reason": "review", "expected_matches": 1}
        first, edits = apply_corrections([source], ["Было."], [rule])
        self.assertEqual(first, ["Стало."])
        second, edits = apply_corrections([source], first, [rule])
        self.assertEqual(first, second)
        self.assertFalse(edits)
        with self.assertRaises(ValueError):
            apply_corrections(["Changed anchor."], first, [rule])

    def test_glossary_selection_keeps_critical_pairs(self):
        profile = {"critical": ["A => А"], "groups": [{"source_pattern": "Being|Nothing", "entries": ["Being => Бытие", "Nothing => Ничто"]}]}
        self.assertEqual(select_glossary("unrelated", profile=profile), "A => А")
        self.assertIn("Nothing => Ничто", select_glossary("Being", profile=profile))


if __name__ == "__main__":
    unittest.main()
