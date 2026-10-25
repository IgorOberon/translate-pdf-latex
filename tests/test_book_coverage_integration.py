"""Coverage gates through orchestrators, with synthetic mocked MT/TeX stages.

These are orchestration regressions, not a substitute for real TeX integrations.
"""
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import content_coverage as coverage
import translate_book as book


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


def _profile(text, old, new):
    return {"schema": 1, "post_edits": [{"id": "checked-title",
        "source_sha256": coverage.sha_text(text), "old": old, "new": new,
        "reason": "Title checked against synthetic original."}]}


def _book_harness(tmp_path, monkeypatch, raw, source="# Source\n\nSource paragraph.\n",
                  profile=None, extra=(), mutate_build=None, remove_toc=False):
    src, work = tmp_path / "source.md", tmp_path / "job"
    _write(src, source)
    args = ["translate_book", str(src), "--workdir", str(work), "--no-lock", "--no-plaques"]
    if profile is not None:
        path = tmp_path / "profile.json"
        _write(path, json.dumps(profile))
        args.extend(["--book-profile", str(path)])
    args.extend(extra)
    published, inspected = [], []
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(book, "preflight", lambda *a, **kw: None)

    def extract(_src, out, *_args):
        result = out / "source.md"
        result.write_bytes(src.read_bytes())
        return result

    def stage(cmd, **_kwargs):
        cmd = list(map(str, cmd))
        if any(Path(item).name == "mask_math.py" for item in cmd):
            mode = cmd.index("mask")
            shutil.copyfile(cmd[mode + 1], cmd[mode + 2])
            _write(Path(cmd[mode + 3]), "{}")
        elif cmd[0] == "pandoc":
            _write(Path(cmd[cmd.index("-o") + 1]), "\\chapter{Synthetic fixture}\n")
        return SimpleNamespace(returncode=0)

    report = {"status": "passed", "kept_source": 0, "failed_blocks": [],
              "source_preservation_sha256": None}

    def translate(_cmd, dst, **_kwargs):
        _write(Path(dst), raw)
        _write(Path(str(dst) + ".report.json"), json.dumps(report))

    def unmask(_masked, dst, _mapping, **_kwargs):
        _write(Path(dst), raw)

    def compile_candidate(candidate):
        (candidate / "main.pdf").write_bytes(b"synthetic mocked PDF")
        _write(candidate / "main.log", "mocked successful compilation")
        _write(candidate / "compile.report.json", '{"status":"passed"}')
        if mutate_build:
            mutate_build(work)
        return True, 0

    def inspect(*_args, **kwargs):
        inspected.append(kwargs)
        return {"status": "passed"}

    monkeypatch.setattr(book, "to_markdown", extract)
    monkeypatch.setattr(book, "run", stage)
    monkeypatch.setattr(book, "run_translate", translate)
    monkeypatch.setattr(book, "run_unmask", unmask)
    monkeypatch.setattr(book, "check_result", lambda *_a, **_k: dict(report))
    monkeypatch.setattr(book, "latex_build", compile_candidate)
    monkeypatch.setattr(book, "inspect_pdf", inspect)
    monkeypatch.setattr(book, "copy_figures", lambda *_a: None)
    monkeypatch.setattr(book, "publish_candidate", lambda *a: published.append(a))
    if remove_toc:
        original = book.assemble_main_tex
        monkeypatch.setattr(book, "assemble_main_tex", lambda *a, **kw:
                            original(*a, **kw).replace("\\tableofcontents", ""))
    return work, published, inspected


def test_book_default_keeps_atlas_title_and_authors(tmp_path, monkeypatch):
    raw = "# Акупунктура 0\n\n# Каналы Клаудия Фокс, Ульрих Мерц\n"
    work, published, _ = _book_harness(tmp_path, monkeypatch, raw)
    book.main()
    assert len(published) == 1
    assert (work / "work/book.ru.md").read_bytes() == raw.encode()
    assert not (work / "work/book.ru.reviewed.md").exists()
    report = json.loads((work / "work/postprocess.coverage.json").read_text())
    assert report["edits"] == []


def test_book_unknown_control_rejected_and_raw_evidence_retained(tmp_path, monkeypatch):
    raw = "Отношение A\x01B необходимо сохранить.\n"
    work, published, _ = _book_harness(tmp_path, monkeypatch, raw)
    with pytest.raises(ValueError, match="unresolved control"):
        book.main()
    assert published == []
    assert (work / "work/book.ru.md").read_bytes() == raw.encode()


def test_reviewed_book_edit_has_distinct_output_and_release_evidence(tmp_path, monkeypatch):
    raw = "# Акупунктура 0\n\n# Каналы Клаудия Фокс\n"
    profile = _profile(raw, "# Акупунктура 0", "# Атлас акупунктуры")
    work, published, _ = _book_harness(tmp_path, monkeypatch, raw, profile=profile)
    book.main()
    assert (work / "work/book.ru.md").read_bytes() == raw.encode()
    checked = (work / "work/book.ru.reviewed.md").read_bytes().decode()
    assert checked == raw.replace("# Акупунктура 0", "# Атлас акупунктуры")
    assert "Клаудия Фокс" in checked
    report_path = work / "work/postprocess.coverage.json"
    report = json.loads(report_path.read_text())
    assert report["source_sha256"] == coverage.sha_text(raw)
    assert report["output_sha256"] == coverage.sha_text(checked)
    assert published[0][2]["postprocess_coverage_sha256"] == book.sha_file(report_path)


def test_no_clean_cannot_bypass_explicit_decisions(tmp_path, monkeypatch):
    source = "# Source\n\nAn exact reviewed paragraph.\n"
    span = "An exact reviewed paragraph.\n"
    profile = {"schema": 1, "cleaning": {"source_sha256": coverage.sha_text(source),
        "decisions": [{"id": "review", "start": source.index(span), "end": len(source),
                       "source_sha256": coverage.sha_text(span), "action": "keep",
                       "reason": "Reviewed in synthetic original."}]}}
    _, published, _ = _book_harness(tmp_path, monkeypatch, "Перевод.\n", source=source,
                                   profile=profile, extra=("--no-clean",))
    with pytest.raises(SystemExit) as stopped:
        book.main()
    assert stopped.value.code == 2
    assert not published


@pytest.mark.parametrize("target", ["receipt", "clean", "post_receipt", "unmasked"])
def test_evidence_modified_during_build_blocks_publication(tmp_path, monkeypatch, target):
    def corrupt(work):
        if target == "receipt":
            path = work / "work/cleaning.coverage.json"
            report = json.loads(path.read_text())
            report["default_decision"] = "tampered"
            _write(path, json.dumps(report))
        elif target == "clean":
            path = next((work / "extract").rglob("*.clean.md"))
            _write(path, "# Source\n\nReplaced text with the same block count.\n")
        elif target == "post_receipt":
            path = work / "work/postprocess.coverage.json"
            _write(path, "{}")
        else:
            path = work / "work/book.ru.md"
            _write(path, "Changed output after assembly.\n")

    _, published, _ = _book_harness(tmp_path, monkeypatch, "Проверенный перевод.\n",
                                   mutate_build=corrupt)
    with pytest.raises((ValueError, SystemExit)):
        book.main()
    assert not published


def _toc_profile(source):
    span = "# Contents\n\nFirst chapter .... 1\n"
    return {"schema": 1, "cleaning": {"source_sha256": coverage.sha_text(source),
        "decisions": [{"id": "toc", "start": 0, "end": len(span),
                       "source_sha256": coverage.sha_text(span), "action": "rebuild",
                       "reason": "Regenerate contents using new pagination."}]}}


def test_explicit_toc_rebuild_requires_pdf_toc_even_without_remaining_heading(tmp_path, monkeypatch):
    source = "# Contents\n\nFirst chapter .... 1\n\nUnnumbered introduction.\n"
    _, published, inspected = _book_harness(tmp_path, monkeypatch, "Введение без заголовка.\n",
                                           source=source, profile=_toc_profile(source))
    book.main()
    assert published
    assert inspected[0]["require_toc"] is True


def test_toc_rebuild_without_generated_toc_cannot_publish(tmp_path, monkeypatch):
    source = "# Contents\n\nFirst chapter .... 1\n\nUnnumbered introduction.\n"
    _, published, _ = _book_harness(tmp_path, monkeypatch, "Введение без заголовка.\n",
                                   source=source, profile=_toc_profile(source), remove_toc=True)
    with pytest.raises((ValueError, SystemExit)):
        book.main()
    assert not published


def _skeleton_module():
    spec = importlib.util.spec_from_file_location("coverage_skeleton_fixture",
            ROOT / "skeleton/translate_into_skeleton.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("target", ["receipt", "clean"])
def test_skeleton_resume_revalidates_cleaning_evidence(tmp_path, monkeypatch, target):
    skeleton = _skeleton_module()
    src, work = tmp_path / "source.md", tmp_path / "chapter"
    work.mkdir()
    _write(src, "# Chapter\n\nAn original paragraph.\n")
    report = {"status": "passed"}
    monkeypatch.setattr(skeleton, "check_result", lambda *_a: dict(report))

    def stage(script, *args, **kwargs):
        if script == "clean_markdown.py":
            coverage.clean_with_coverage(args[0], args[1])
        elif script == "mask_math.py" and args[0] == "mask":
            shutil.copyfile(args[1], args[2])
            _write(Path(args[3]), "{}")
        elif script == "mask_math.py" and args[0] == "unmask":
            shutil.copyfile(args[1], args[2])
            return "restored exact: 0, repaired damaged: 0, unresolved: 0"
        return ""

    def translate(_cmd, dst):
        _write(Path(dst), "# Глава\n\nПереведённый абзац.\n")

    monkeypatch.setattr(skeleton, "stage_script", stage)
    monkeypatch.setattr(skeleton, "run_translate", translate)
    args = SimpleNamespace(mode="hymt", model="mock", host="http://localhost:1234",
                           glossary=None, glossary_profile=None, split_chars=0,
                           group_chars=0, context_per_request=0, force=False, parallel=1)
    output = skeleton.translate_chapter(work, src, args)
    original = output.read_bytes()
    if target == "receipt":
        _write(work / "chapter.clean.coverage.json", "{}")
    else:
        _write(work / "chapter.clean.md", "Replacement paragraph.\n")
    with pytest.raises((ValueError, SystemExit)):
        skeleton.translate_chapter(work, src, args)
    assert output.read_bytes() == original
