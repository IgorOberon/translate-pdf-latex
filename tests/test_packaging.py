"""Curated export must not acquire book files, secrets or unreviewed scripts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_package as package
import sync_skill
from verify_package import verify
from provenance import sha_file
from provenance import atomic_text, write_json


class PackagingTests(unittest.TestCase):
    def test_sync_preserves_user_profile_and_repack_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dest = Path(tmp) / "source", Path(tmp) / "skill"
            result = self.fixture(root)
            write_json(root / "package.manifest.json", {"schema": 1, "profile": "user"})
            write_json(dest / "package.manifest.json", {"schema": 1, "files": {"old": "old"}})
            with patch.object(sync_skill, "ROOT", root), patch.object(package, "DOCS", ["SKILL.md", "docs/ARTICLE_IDEAS.md"]), patch.object(package.subprocess, "run", return_value=result):
                sync_skill.sync(dest)
                self.assertEqual(package.package_profile(dest), "user")
                self.assertEqual(package.package_files(dest), ["SKILL.md", "scripts/translate.py"])
                self.assertEqual(verify(dest), (2, []))
            saved = list((dest / ".sync-backups").glob("*/package.manifest.json"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(saved[0].read_text())["files"], {"old": "old"})

    def test_user_zip_has_single_skill_root_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            out = Path(tmp) / "out"
            result = self.fixture(root)
            with patch.object(package, "ROOT", root), patch.object(package, "DOCS", ["SKILL.md"]), patch.object(package.subprocess, "run", return_value=result), patch.object(sys, "argv", ["make_package.py", "--profile", "user", "--out", str(out)]):
                self.assertEqual(package.main(), 0)
            name = package.SKILL_NAME
            archive = out / (name + ".zip")
            with zipfile.ZipFile(archive) as z:
                self.assertIsNone(z.testzip())
                self.assertEqual(set(z.namelist()), {name + "/" + f for f in ("SKILL.md", "scripts/translate.py", "package.manifest.json")})
                self.assertTrue(all("\\" not in n for n in z.namelist()))
            self.assertEqual(verify(out / name), (2, []))
            self.assertEqual(json.loads((out / name / "package.manifest.json").read_text())["name"], name)
            self.assertTrue(Path(str(archive) + ".sha256").read_text().startswith(sha_file(archive)))

    def test_user_profile_and_manifest_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.fixture(root)
            for name in ("ОТЧЁТ_old.md", "docs/опыт-тома-6/history.md", "docs/ARTICLE_IDEAS.md"):
                atomic_text(root / name, "not for end users")
            docs = ["SKILL.md", "ОТЧЁТ_old.md", "docs/опыт-тома-6/history.md", "docs/ARTICLE_IDEAS.md"]
            with patch.object(package, "DOCS", docs), patch.object(package.subprocess, "run", return_value=result):
                files = package.package_files(root, "user")
                self.assertEqual(files, ["SKILL.md", "scripts/translate.py"])
                self.assertEqual(len(package.package_files(root, "full")), 5)
                write_json(root / "package.manifest.json", {"schema": 1, "profile": "user",
                           "files": {name: sha_file(root / name) for name in files}})
            with patch.object(package, "DOCS", docs), patch.object(package.subprocess, "run", side_effect=OSError):
                self.assertEqual(package.package_files(root), files)
            self.assertEqual(verify(root), (2, []))
            atomic_text(root / "SKILL.md", "changed")
            self.assertIn("CHANGED: SKILL.md", verify(root)[1])

    def test_verify_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("../secret.txt", "C:/secret.txt", "scripts\\evil.py"):
                write_json(root / "package.manifest.json", {"schema": 1, "files": {name: "a" * 64}})
                with self.assertRaises(ValueError):
                    verify(root)

    def fixture(self, root):
        for name in ("SKILL.md", "scripts/translate.py", "scripts/debug_private.py", "source.pdf"):
            atomic_text(root / name, "fixture")
        return SimpleNamespace(returncode=0, stdout=b"scripts/translate.py\0")

    def test_untracked_and_books_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.fixture(root)
            with patch.object(package, "DOCS", ["SKILL.md"]), patch.object(package.subprocess, "run", return_value=result):
                self.assertEqual(package.package_files(root), ["SKILL.md", "scripts/translate.py"])

    def test_secret_content_blocks_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.fixture(root)
            atomic_text(root / "SKILL.md", "sk-" + "a" * 40)
            with patch.object(package, "DOCS", ["SKILL.md"]), patch.object(package.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError):
                    package.package_files(root)

    def test_public_collection_does_not_bundle_historical_engines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            accepted = [
                "companion-skills/local-mt-batch/SKILL.md",
                "companion-skills/translation-pipeline-diagnostics/agents/openai.yaml",
            ]
            historical = [
                "companion-skills/local-mt-batch/scripts/translate.py",
                "companion-skills/translate-book-to-latex/SKILL.md",
                "companion-skills/collection.manifest.json",
            ]
            for name in accepted + historical:
                atomic_text(root / name, "fixture")
            tracked = ["scripts/translate.py", *accepted, *historical]
            result = SimpleNamespace(returncode=0, stdout=("\0".join(tracked) + "\0").encode())
            with patch.object(package, "DOCS", ["SKILL.md"]), patch.object(package.subprocess, "run", return_value=result):
                for profile in ("user", "full"):
                    files = package.package_files(root, profile)
                    self.assertEqual(set(files), {"SKILL.md", "scripts/translate.py", *accepted})
                    write_json(root / "package.manifest.json", {"profile": profile,
                               "files": {name: sha_file(root / name) for name in files}})
                    with patch.object(package.subprocess, "run", side_effect=OSError):
                        self.assertEqual(package.package_files(root), files)

    def test_non_git_uses_inventory_not_recursion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            with patch.object(package, "DOCS", ["SKILL.md"]), patch.object(package.subprocess, "run", side_effect=OSError):
                with self.assertRaises(ValueError):
                    package.package_files(root)
                write_json(root / "package.manifest.json", {"files": {"scripts/translate.py": "oldhash"}})
                self.assertEqual(package.package_files(root), ["SKILL.md", "scripts/translate.py"])
            # A package located inside an unrelated/parent Git worktree may get
            # an empty successful ls-files result; its own manifest still wins.
            with patch.object(package, "DOCS", ["SKILL.md"]), patch.object(package.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"")):
                self.assertEqual(package.package_files(root), ["SKILL.md", "scripts/translate.py"])


if __name__ == "__main__":
    unittest.main()
