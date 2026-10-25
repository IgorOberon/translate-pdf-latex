"""Request settings reach every route using isolated synthetic inputs only."""
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skeleton"))
import pipeline
import provenance
import run_doc_translate as doc
import translate as mt
import translate_book as book
import translate_folder as folder
import translate_into_skeleton as skeleton
from glossary_selection import load_profile, select_glossary
from request_budget import check_messages, estimate_messages
from content_coverage import clean_with_coverage


class CapturedCall(Exception):
    """Stop at the child-process boundary without running a model or TeX."""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def profile(path, word="term"):
    write(path, json.dumps({"schema": 1, "critical": [word + " => термин"]}))
    return path


def value(argv, flag):
    args = list(map(str, argv))
    return args[args.index(flag) + 1]


def route_source(root, route):
    if route == "books":
        return write(root / "source/book.md", "A source paragraph.")
    if route == "ocr":
        write(root / "source/pages/p0001.txt", "Der Begriff.")
        write(root / "source/structure.json", "{}")
    else:
        write(root / "source/book/main.tex", r"\documentclass{book}")
        write(root / "source/source.pdf", "synthetic placeholder; never parsed")
    return root / "source"


class RouteRequestSettingsTests(unittest.TestCase):
    def test_implicit_profile_is_snapshotted_and_forwarded_in_all_routes(self):
        for route in ("books", "ocr", "skeleton"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = route_source(root, route)
                supplied = profile(root / "source/glossary.profile.json")
                job = pipeline.initialize(source, root / "job", {
                    "split_chars": 725, "group_chars": 0 if route == "ocr" else 1400,
                    "context_per_request": 8192})
                steps = pipeline.steps_for(root / "job", job)
                cmd = steps[1][1] if route == "ocr" else steps[0][1]
                self.assertEqual(value(cmd, "--split-chars"), "725")
                self.assertEqual(value(cmd, "--context-per-request"), "8192")
                if route != "ocr":
                    self.assertEqual(value(cmd, "--group-chars"), "1400")
                saved = Path(value(cmd, "--glossary-profile"))
                self.assertTrue(saved.is_relative_to(root / "job/input"))
                self.assertEqual(saved.read_bytes(), supplied.read_bytes())
                self.assertIn(saved.relative_to(root / "job").as_posix(), job["inputs"])
                profile(supplied, "changed original")
                pipeline.load_job(root / "job")
                profile(saved, "changed snapshot")
                with self.assertRaisesRegex(ValueError, "snapshot changed"):
                    pipeline.load_job(root / "job")

    def test_explicit_profile_override_uses_immutable_job_snapshot(self):
        for route in ("books", "ocr", "skeleton"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = route_source(root, route)
                profile(root / "source/glossary.profile.json", "implicit")
                explicit = profile(root / "external/profile.json", "explicit")
                job = pipeline.initialize(source, root / "job", {"glossary_profile": str(explicit)})
                cmd = pipeline.steps_for(root / "job", job)[1 if route == "ocr" else 0][1]
                saved = Path(value(cmd, "--glossary-profile"))
                self.assertEqual(saved, root / "job/input/request-glossary.profile.json")
                self.assertEqual(load_profile(saved)["critical"], ["explicit => термин"])
                self.assertNotIn(str(explicit), list(map(str, cmd)))

    def test_book_forwards_exact_request_settings_to_translator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write(root / "source.md", "A source paragraph.")
            gp = profile(root / "profile.json")
            argv = ["translate_book", str(source), "--workdir", str(root / "job"),
                    "--no-lock", "--no-clean", "--split-chars", "725", "--group-chars", "1400",
                    "--context-per-request", "8192", "--glossary-profile", str(gp)]
            with patch.object(sys, "argv", argv), patch.object(book, "preflight"), \
                    patch.object(book, "run", return_value=SimpleNamespace(returncode=0)), \
                    patch.object(book, "run_translate", side_effect=CapturedCall) as called:
                with self.assertRaises(CapturedCall):
                    book.main()
            cmd = called.call_args.args[0]
            for flag, expected in (("--split-chars", "725"), ("--group-chars", "1400"),
                                   ("--context-per-request", "8192"), ("--glossary-profile", str(gp))):
                self.assertEqual(value(cmd, flag), expected)
            self.assertEqual(source.read_text(encoding="utf-8"), "A source paragraph.")

    def test_folder_forwards_settings_to_each_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "books/source.pdf", "synthetic placeholder")
            gp = profile(root / "profile.json")
            argv = ["translate_folder", str(root / "books"), "--outdir", str(root / "output"),
                    "--split-chars", "725", "--group-chars", "1400", "--context-per-request", "8192",
                    "--glossary-profile", str(gp)]
            with patch.object(sys, "argv", argv), \
                    patch.object(folder.subprocess, "run", side_effect=CapturedCall) as called:
                with self.assertRaises(CapturedCall):
                    folder.main()
            cmd = called.call_args.args[0]
            for flag, expected in (("--split-chars", "725"), ("--group-chars", "1400"),
                                   ("--context-per-request", "8192"), ("--glossary-profile", str(gp))):
                self.assertEqual(value(cmd, flag), expected)

    def test_skeleton_forwards_settings_to_chapter_translator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            md = write(root / "source.md", "A source paragraph.")
            gp = profile(root / "profile.json")
            work = root / "work"
            work.mkdir()
            args = SimpleNamespace(mode="hymt", model="fixture", host="http://localhost:1234",
                                   glossary=None, glossary_profile=str(gp), force=False, parallel=1,
                                   split_chars=725, group_chars=1400, context_per_request=8192)

            def stage(name, *args, **kwargs):
                if name == "clean_markdown.py":
                    clean_with_coverage(args[0], args[1])
                return ""

            with patch.object(skeleton, "stage_script", side_effect=stage), \
                    patch.object(skeleton, "run_translate", side_effect=CapturedCall) as called:
                with self.assertRaises(CapturedCall):
                    skeleton.translate_chapter(work, md, args)
            cmd = called.call_args.args[0]
            for flag, expected in (("--split-chars", "725"), ("--group-chars", "1400"),
                                   ("--context-per-request", "8192"), ("--glossary-profile", str(gp))):
                self.assertEqual(value(cmd, flag), expected)

    def test_skeleton_rejects_glossary_mutation_before_accepting_chapter(self):
        for changed_input in ("glossary", "glossary_profile"):
            with self.subTest(changed_input=changed_input), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                md = write(root / "source.md", "The source paragraph.")
                gp = profile(root / "profile.json", "first")
                glossary = write(root / "glossary.md", "First => первое")
                work = root / "work"
                work.mkdir()
                args = SimpleNamespace(mode="hymt", model="fixture", host="http://localhost:1234",
                                       glossary=str(glossary), glossary_profile=str(gp), force=False,
                                       parallel=1, split_chars=900, group_chars=900, context_per_request=0)

                def stage(name, *args, **kwargs):
                    if name == "clean_markdown.py":
                        clean_with_coverage(args[0], args[1])
                    elif args[0] == "mask":
                        write(Path(args[2]), Path(args[1]).read_text(encoding="utf-8"))
                        write(Path(args[3]), "{}")
                    elif args[0] == "unmask":
                        write(Path(args[2]), Path(args[1]).read_text(encoding="utf-8"))
                        return "unresolved: 0"
                    return ""

                def translate(cmd, dst):
                    if changed_input == "glossary_profile":
                        profile(gp, "changed during MT")
                    else:
                        write(glossary, "Changed => изменённое")
                    write(Path(dst), "Переведённый абзац.")

                with patch.object(skeleton, "stage_script", side_effect=stage), \
                        patch.object(skeleton, "run_translate", side_effect=translate), \
                        patch.object(skeleton, "check_result", return_value={"status": "passed"}):
                    with self.assertRaises(SystemExit):
                        skeleton.translate_chapter(work, md, args)
                self.assertFalse((work / "chapter.state.json").exists())
                self.assertTrue((work / "chapter.ru.md").is_file())  # retained diagnostic candidate
                self.assertEqual(md.read_text(encoding="utf-8"), "The source paragraph.")

    def test_ocr_forwards_settings_without_changing_aligned_block_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "book.md", "Der Begriff.")
            write(root / "book.part1.md", "Der Begriff.")
            gp = profile(root / "profile.json")
            argv = ["run_doc_translate", str(root), "--split-chars", "725",
                    "--context-per-request", "8192", "--glossary-profile", str(gp)]
            with patch.object(sys, "argv", argv), \
                    patch.object(doc.subprocess, "run", side_effect=CapturedCall) as called:
                with self.assertRaises(CapturedCall):
                    doc.main()
            cmd = called.call_args.args[0]
            self.assertEqual(value(cmd, "--split-chars"), "725")
            self.assertEqual(value(cmd, "--context-per-request"), "8192")
            self.assertEqual(value(cmd, "--glossary-profile"), str(gp))
            if "--group-chars" in cmd:
                self.assertEqual(value(cmd, "--group-chars"), "0")

    def test_new_ocr_jobs_reject_grouping_without_breaking_historical_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = route_source(root, "ocr")
            job = pipeline.initialize(source, root / "job")
            self.assertEqual(job["settings"]["group_chars"], 0)
            with self.assertRaisesRegex(ValueError, "requires group_chars=0"):
                pipeline.initialize(source, root / "invalid-job", {"group_chars": 900})
            self.assertFalse((root / "invalid-job").exists())
            # Previous dispatcher versions wrote the global default although
            # OCR ignored it. Loading that saved configuration does not migrate
            # source snapshots, cache identities, or input hashes.
            job["settings"]["group_chars"] = 900
            provenance.write_json(root / "job" / pipeline.JOB, job)
            before = (root / "job" / pipeline.JOB).read_bytes()
            loaded = pipeline.load_job(root / "job")
            self.assertEqual(loaded["settings"]["group_chars"], 900)
            self.assertEqual((root / "job" / pipeline.JOB).read_bytes(), before)
            cmd = pipeline.steps_for(root / "job", loaded)[1][1]
            self.assertNotIn("--group-chars", cmd)

    def test_release_identity_changes_with_request_settings_and_profile_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            gp = profile(Path(tmp) / "profile.json", "first")
            args = SimpleNamespace(split_chars=725, group_chars=1400, context_per_request=8192,
                                   glossary_profile=str(gp))
            baseline = provenance.book_settings(args)
            for setting in ("split_chars", "group_chars", "context_per_request"):
                for new in (0, 15000):
                    with self.subTest(setting=setting, value=new):
                        changed = SimpleNamespace(**vars(args))
                        setattr(changed, setting, new)
                        self.assertNotEqual(provenance.book_settings(changed), baseline)
            profile(gp, "second")
            self.assertNotEqual(provenance.book_settings(args), baseline)

    def test_context_unknown_is_explicit_and_parallel_does_not_divide_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "book.md", "Der Begriff.")
            write(root / "book.part1.md", "Der Begriff.")
            provenance.create_manifest(root)
            ok, lines = doc.check_context(root, None, "fixture", 32, 900, 0)
            self.assertTrue(ok)
            self.assertIn("unknown", " ".join(lines))
            serial = doc.check_context(root, None, "fixture", 1, 900, 8192)
            parallel = doc.check_context(root, None, "fixture", 32, 900, 8192)
            self.assertEqual(serial, parallel)

    def test_budget_includes_exact_prompt_glossary_and_reply_reserve(self):
        source = "A short source."
        gp = {"schema": 1, "critical": ["longterm => " + "термин " * 3000]}
        messages = mt.build_messages(source, "hymt", "Russian", select_glossary(source, "", gp))
        small = estimate_messages(mt.build_messages(source, "hymt", "Russian", ""))
        self.assertGreater(estimate_messages(messages), small)
        self.assertGreater(small, 4096)
        self.assertEqual(check_messages(messages, 0), estimate_messages(messages))
        with self.assertRaisesRegex(ValueError, "budget exceeded"):
            check_messages(messages, small)
        for invalid in (-1, True, 3.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                check_messages(messages, invalid)

    def test_oversized_prompt_never_reaches_model_and_retains_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write(root / "source.md", "A short source.")
            gp = profile(root / "profile.json", "verylongterm" * 1000)
            output = root / "target.md"
            argv = ["translate", str(source), str(output), "--cache-dir", str(root / "cache"),
                    "--context-per-request", "5000", "--glossary-profile", str(gp)]
            with patch.object(sys, "argv", argv), patch.object(mt, "call_api") as api, \
                    patch.object(mt.time, "sleep"), patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as failed:
                    mt.main()
            self.assertEqual(failed.exception.code, 4)
            api.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertFalse(list((root / "cache").glob("*.json")))
            report = json.loads(Path(str(output) + ".report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            trace = "\n".join(p.read_text(encoding="utf-8") for p in Path(report["attempt_journal"]).rglob("*.json"))
            self.assertIn("budget exceeded", trace)

    def test_profile_only_glossary_leak_is_rejected_on_first_run(self):
        entries = ["Being => бытие", "Nothing => ничто", "Becoming => становление"]
        leaked = "Здесь раскрывается понятие.\n" + "\n".join(entries)
        for group_chars in (0, 900):
            with self.subTest(group_chars=group_chars), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = write(root / "source.md", "The concept is developed here.\n\nThe relation is retained.")
                gp = write(root / "profile.json", json.dumps({"schema": 1, "critical": entries}))
                output = root / "target.md"

                def response(host, model, messages, temperature, top_p):
                    tokens = list(mt.paragraphs.MARKER.finditer(messages[-1]["content"]))
                    if not tokens:
                        return leaked
                    return "\n\n".join(f"⟦{opening[2]}⟧\n{leaked}\n⟦/{opening[2]}⟧"
                                       for opening in tokens[::2])

                argv = ["translate", str(source), str(output), "--cache-dir", str(root / "cache"),
                        "--group-chars", str(group_chars), "--glossary-profile", str(gp)]
                with patch.object(sys, "argv", argv), patch.object(mt, "call_api", side_effect=response), \
                        patch.object(mt.time, "sleep"), patch.object(sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as failed:
                        mt.main()
                self.assertEqual(failed.exception.code, 4)
                self.assertEqual(output.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
                self.assertFalse(list((root / "cache").glob("*.json")))
                report = json.loads(Path(str(output) + ".report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "failed")
                self.assertTrue(all("glossary leak" in row["reason"] for row in report["failed_blocks"]))


if __name__ == "__main__":
    unittest.main()
