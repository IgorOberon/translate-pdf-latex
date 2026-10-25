#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a curated portable pipeline, with hashes and recoverable replacement.

No runtime book directories, caches, environments, private logs or debug
untracked scripts. Git is inventory only: modified tracked code IS included.
In an extracted distribution the previous package manifest is the inventory.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT
from provenance import inside, sha_file, write_json, atomic_text

NAME = "Конвейер-перевода-книг"
SKILL_NAME = "translate-pdf-latex"
DIRS = ("scripts", "assets", "skeleton", "tests")
DOCS = [
    "docs/COMPANION_SKILLS.md",
    "AGENTS.md", "PIPELINE_REFERENCE.md", "ПРОМПТ_РУЧНЫЕ_СТАДИИ.md",
    "docs/LOCAL_AGENT.md", "docs/ARTICLE_IDEAS.md",
    "docs/BOOK_PROFILES.md", "docs/BACKGROUND_RUN.md", "docs/OPENCODE_LESSONS.md",
    "ЧИТАТЬ_ПЕРВЫМ.md", "УСТАНОВКА.md", "ДЛЯ_ИИ_АГЕНТА.md",
    "install.ps1", "requirements.txt", "requirements-extra.txt",
    "README.md", "АГЕНТ.md", "ПРОМПТ.md", "SKILL.md",
    "КОНФИГУРАЦИИ.md", "РУКОВОДСТВО.md", "ПОСОБИЕ.md",
    "ОТЧЁТ_КОНВЕЙЕР.md", "ОТЧЁТ_NFCM_ДЕФЕКТЫ_КОНВЕЙЕРА.md",
    "glossary.example.md", "glossary_de.md",
    "docs/INTEGRITY.md",
    "docs/опыт-тома-6/README.md", "docs/опыт-тома-6/01_ОПЫТ_ТОМА_6.md",
    "docs/опыт-тома-6/02_ЗАДАНИЕ_НА_ОБНОВЛЕНИЕ.md",
    "docs/опыт-тома-6/03_ПРОМПТ_ДЛЯ_ДРУГОЙ_СЕССИИ.md",
    "docs/опыт-тома-6/04_РЕЗУЛЬТАТ.md",
    "docs/опыт-тома-6/05_ПРОВЕРКА_LUNA.md",
    "docs/опыт-тома-6/06_СЛОЖНЫЙ_ПИЛОТ.md",
]
# Explicitly reviewed new source files, before their first git commit.
ADDITIONS = {
    "scripts/background_run.py", "scripts/windows_tasks.py", "scripts/windows_task.ps1",
    "scripts/lmstudio_runtime.py", "tests/test_background_run.py",
    "scripts/managed_model_guard.py", "tests/test_managed_model_guard.py",
    "tests/test_pipeline_state_diagnostics.py", "tests/test_pdf_geometry.py",
    "tests/test_windows_tasks.py", "tests/test_lmstudio_runtime.py",
    "tests/test_managed_gpu_handoff.py",
    "tests/run_detached_integration.py", "tests/run_lmstudio_detached_integration.py",
    "scripts/prepare_mineru.py", "tests/test_mineru_structure.py",
    "tests/test_installation.py",
    "scripts/verify_package.py", "assets/quickstart.md",
    "scripts/pipeline.py", "scripts/endpoint_policy.py",
    "tests/test_dispatcher.py", "tests/run_dispatcher_integrations.py",
    "scripts/provenance.py", "scripts/request_plan.py", "scripts/build_release.py",
    "scripts/editorial.py", "scripts/glossary_selection.py",
    "scripts/agent_exchange.py",
    "scripts/tex_logs.py", "scripts/source_preservation.py",
    "scripts/attempt_journal.py", "scripts/paragraph_units.py",
    "scripts/content_coverage.py", "scripts/request_budget.py",
    "tests/test_attempt_journal.py", "tests/test_paragraph_identity.py",
    "tests/test_content_coverage.py", "tests/test_route_request_settings.py",
    "tests/test_translation_attempt_journal.py", "tests/test_book_coverage_integration.py",
    "tests/test_build_resume.py", "tests/test_longtable_layout.py",
    "tests/test_atlas_translation.py", "tests/test_source_preservation.py",
    "tests/test_integrity_regressions.py", "tests/run_integrations.py",
    "tests/test_packaging.py",
    "tests/test_luna_pilot_regressions.py",
    "tests/test_complex_ocr_regressions.py",
}
SKIP_SCRIPTS = {"escape_dollars.py", "fix_map_english.py"}
SKIP_DIRS = {"__pycache__", ".git", ".tcache", ".pytest_cache", "fixtures"}
SOURCE_SUFFIXES = {".py", ".sh", ".tex", ".lua", ".md", ".json", ".ps1"}
SECRET_NAME = re.compile(r"(^|[._-])(token|secret|credential|password|apikey|api_key)s?([._-]|$)|\.(pem|key|pfx|env)$", re.I)
# High-confidence formats only; never echo detected values.
SECRET_CONTENT = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
                            r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|"
                            r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b|"
                            r"(?i:(?:api[_-]?key|token|password)\s*=\s*[\"'])[A-Za-z0-9_-]{24,}")


def want(path):
    return (not set(path.parts) & SKIP_DIRS and path.suffix.lower() in SOURCE_SUFFIXES
            and path.name not in SKIP_SCRIPTS)


def package_profile(root, requested=None):
    if requested:
        if requested not in ("full", "user"):
            raise ValueError("unknown package profile")
        return requested
    manifest = Path(root) / "package.manifest.json"
    if manifest.is_file():
        return package_profile(root, json.loads(manifest.read_text(encoding="utf-8")).get("profile", "full"))
    return "full"


def selected_docs(profile):
    if profile == "full":
        return DOCS
    # User instructions and diagnostic tests stay; project history does not.
    return [name for name in DOCS if not name.startswith(("ОТЧЁТ", "docs/опыт-тома-6/"))
            and name != "docs/ARTICLE_IDEAS.md"]


def package_files(root=ROOT, profile=None):
    root = Path(root).resolve()
    profile = package_profile(root, profile)
    try:
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *DIRS],
                                capture_output=True, timeout=20)
        tracked = set(result.stdout.decode("utf-8").split("\0")) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        tracked = None
    if tracked is None or "scripts/translate.py" not in tracked:
        inventory = root / "package.manifest.json"
        if not inventory.exists():
            raise ValueError("neither git inventory nor package.manifest.json; refusing broad recursive export")
        tracked = set(json.loads(inventory.read_text(encoding="utf-8"))["files"])
    paths = (tracked | ADDITIONS) - {""}
    selected = {name for name in paths if Path(name).parts[0] in DIRS
                and want(Path(name)) and inside(root, name).is_file()}
    # Current canonical docs only; not historical book commands or old manual PDF.
    for name in selected_docs(profile):
        if not inside(root, name).is_file():
            raise ValueError("required package document missing: " + name)
        selected.add(name)
    if not {"scripts/translate.py", "SKILL.md"} <= selected:
        raise ValueError("incomplete package inventory")
    for name in selected:
        path = inside(root, name)
        if SECRET_NAME.search(path.name) or SECRET_CONTENT.search(path.read_text(encoding="utf-8")):
            raise ValueError("possible secret; export refused (value redacted): " + name)
    return sorted(selected)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "_передача"))
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--profile", choices=("full", "user"),
                    help="user: one installable skill without project history; default follows manifest or full")
    a = ap.parse_args()
    profile = package_profile(ROOT, a.profile)
    package_name = SKILL_NAME if profile == "user" else NAME
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    dst = (out / package_name).resolve()
    if dst.parent != out or dst == ROOT.resolve() or ROOT.resolve().is_relative_to(dst):
        raise ValueError("unsafe package destination")
    files = package_files(ROOT, profile=profile)
    stage = Path(tempfile.mkdtemp(prefix=".package-", dir=out))
    for name in files:
        target = inside(stage, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(inside(ROOT, name), target)
    write_json(stage / "package.manifest.json", {"schema": 1, "name": package_name, "profile": profile,
               "files": {name: sha_file(inside(stage, name)) for name in files},
               "excludes": ["books", "caches", "environments", "private logs", "unreviewed files"],
               "examples": "assets/quickstart.md is synthetic; no book data or model weights included"})
    # Validate copied bytes before replacing the old distribution.
    for name in files:
        if sha_file(inside(stage, name)) != sha_file(inside(ROOT, name)):
            raise ValueError("source changed during packaging: " + name)
    backup = None
    if dst.exists():
        backup = out / (package_name + ".previous-" + uuid.uuid4().hex[:10])
        dst.rename(backup)
    try:
        stage.rename(dst)
    except OSError:
        if backup:
            backup.rename(dst)
        raise
    print(f"files: {len(files) + 1}; package: {dst}")
    if backup:
        print(f"previous package retained: {backup}")
    if not a.no_zip:
        fd, tmp = tempfile.mkstemp(prefix=".package-", suffix=".zip", dir=out)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for member in [*files, "package.manifest.json"]:
                    archive.write(inside(dst, member), f"{package_name}/{member}")
            zip_path = out / (package_name + ".zip")
            if zip_path.exists():
                zip_path.rename(out / (package_name + ".previous-" + uuid.uuid4().hex[:10] + ".zip"))
            os.replace(tmp, zip_path)
            atomic_text(Path(str(zip_path) + ".sha256"), sha_file(zip_path) + "  " + zip_path.name + "\n")
            print(f"archive: {zip_path}; sha256: {sha_file(zip_path)}")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
