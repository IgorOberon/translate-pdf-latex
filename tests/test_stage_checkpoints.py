"""Synthetic stage resumes, with no finished book or live model as fixture."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stage_checkpoints as stages
from provenance import canonical_hash, sha_file, write_json
from build_release import compile_latex
from test_build_resume import fake_pass, pass_directory
import pipeline_queue as queue


def test_extract_identity_excludes_orchestrator_but_binds_engine_source_options(tmp_path):
    md = tmp_path / "source.md"
    md.write_text("synthetic extraction")
    for name in ("extract_chunked.py", "procutils.py", "translate_book.py"):
        (tmp_path / name).write_text("code = 'original'")
    key = stages.extraction_identity("sourcehash", "math", 10, tmp_path)
    receipt = {"schema": stages.EXTRACTION_VERSION, "source_sha256": "sourcehash",
               "config_sha256": key, "output_sha256": sha_file(md)}
    (tmp_path / "translate_book.py").write_text("code = 'new translation code'")
    assert stages.reuse_extraction(receipt, "sourcehash", "math", 10, tmp_path, md)
    assert not stages.reuse_extraction(receipt, "changed", "math", 10, tmp_path, md)
    assert not stages.reuse_extraction(receipt, "sourcehash", "math", 9, tmp_path, md)
    (tmp_path / "extract_chunked.py").write_text("new engine code")
    assert not stages.reuse_extraction(receipt, "sourcehash", "math", 10, tmp_path, md)
    legacy = {"output_sha256": sha_file(md), "config_sha256": canonical_hash({
        "source": "sourcehash", "mode": "math", "chunk": 10,
        "script": next(iter(stages.LEGACY_EXTRACTORS))})}
    before = dict(legacy)
    assert stages.reuse_extraction(legacy, "sourcehash", "math", 10, tmp_path, md)
    assert legacy == before
    assert not stages.reuse_extraction(legacy, "other", "math", 10, tmp_path, md)
    assert not stages.reuse_extraction({"output_sha256": sha_file(md)}, "sourcehash", "math", 10, tmp_path, md)


def test_build_reuses_prepared_inputs_and_refuses_identical_known_failure(tmp_path):
    inputs = {"md": "exacthash", "renderer": "v1", "figures": "hash"}
    candidate, reused, receipt = stages.select_build(tmp_path, inputs)
    assert not reused
    (candidate / "main.tex").write_text("synthetic main")
    (candidate / "body.tex").write_text("synthetic body")
    write_json(candidate / "release.report.json", {"status": "failed"})
    stages.seal_build(candidate, receipt)
    write_json(candidate / "release.report.json", {"status": "passed"})
    assert stages.select_build(tmp_path, inputs)[:2] == (candidate, True)
    write_json(candidate / "compile.report.json", {"status": "failed", "error": "ValueError: Missing character"})
    before = sorted(tmp_path.glob("candidate-*"))
    with pytest.raises(ValueError, match="unchanged build failure"):
        stages.select_build(tmp_path, inputs)
    assert sorted(tmp_path.glob("candidate-*")) == before
    new, reused, _ = stages.select_build(tmp_path, dict(inputs, renderer="fixed-v2"))
    assert new != candidate and not reused
    assert (candidate / "body.tex").read_text() == "synthetic body"


def test_technical_failure_and_tampered_preparation_are_not_accepted(tmp_path):
    candidate, _, receipt = stages.select_build(tmp_path, {"md": "same"})
    (candidate / "main.tex").write_text("main")
    stages.seal_build(candidate, receipt)
    write_json(candidate / "release.report.json", {"technical": {"status": "failed"}})
    with pytest.raises(ValueError, match="unchanged technical"):
        stages.select_build(tmp_path, {"md": "same"})
    (candidate / "main.tex").write_text("tampered")
    new, reused, _ = stages.select_build(tmp_path, {"md": "same"})
    assert not reused and new != candidate


def test_native_tex_interruption_keeps_same_prepared_candidate(tmp_path):
    candidate, _, receipt = stages.select_build(tmp_path, {"md": "same"})
    (candidate / "main.tex").write_text("main")
    stages.seal_build(candidate, receipt)
    write_json(candidate / "compile.report.json", {"status": "failed",
               "error": "ValueError: xelatex returned 3221225786", "passes": [{"rc": 3221225786}]})
    assert stages.select_build(tmp_path, {"md": "same"})[:2] == (candidate, True)


def test_font_change_creates_new_build_without_touching_old_candidate(tmp_path):
    candidate, _, receipt = stages.select_build(tmp_path, {"md": "same"})
    (candidate / "main.tex").write_text("main")
    stages.seal_build(candidate, receipt)
    font = tmp_path / "font.otf"
    font.write_bytes(b"old font")
    write_json(candidate / "compile.state.json", {"inputs": {str(font): sha_file(font)}})
    assert stages.select_build(tmp_path, {"md": "same"})[:2] == (candidate, True)
    font.write_bytes(b"fixed font")
    new, reused, _ = stages.select_build(tmp_path, {"md": "same"})
    assert new != candidate and not reused
    assert (candidate / "main.tex").read_text() == "main"


def test_missing_character_stops_after_first_pass_without_publishing(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text("synthetic")
    calls = []
    def runner(command, **kwargs):
        directory = pass_directory(command)
        calls.append(directory)
        fake_pass(directory)
        (directory / "main.log").write_text("Missing character: Ж in font!\n")
        return SimpleNamespace(returncode=0)
    result = compile_latex(main, tmp_path / "build", runner=runner)
    assert result["status"] == "failed" and "Missing character" in result["error"]
    assert len(calls) == 1 and not (tmp_path / "build/main.pdf").exists()


@pytest.mark.parametrize("artifact", ["main.pdf", "main.log"])
def test_completed_compile_rechecks_published_artifacts(tmp_path, artifact):
    main = tmp_path / "main.tex"
    main.write_text("synthetic")
    def runner(command, **kwargs):
        fake_pass(pass_directory(command))
        return SimpleNamespace(returncode=0)
    output = tmp_path / "build"
    assert compile_latex(main, output, runner=runner)["status"] == "passed"
    (output / artifact).write_bytes(b"changed")
    def forbidden(*args, **kwargs):
        pytest.fail("must refuse changed output without launching TeX")
    assert compile_latex(main, output, runner=forbidden)["status"] == "failed"


@pytest.mark.parametrize("message, expected", [
    ("request failed: HTTP 400", "configuration"), ("HTTP Error 429", "transient"),
    ("HTTP 503", "transient"), ("request failed: URLError", "offline"),
    ("Missing character after URLError", "build"), ("numbers changed", "validation"),
    ("nothing_to_repair", "validation"), ("KeyboardInterrupt", "interrupted"),
    ("managed model refused: changed", "configuration"),
])
def test_failure_classification(message, expected):
    assert queue.classify_failure(message) == expected


def test_controller_is_bounded_and_continues_other_jobs():
    calls, waits = [], []
    def run(job):
        calls.append(job)
        return {"returncode": 1, "failure": "transient" if job == "network" else "validation"}
    results = queue.drain(["network", "badtext"], run, lambda job: True, waits.append)
    assert calls == ["network", "network", "network", "badtext"]
    assert waits == [5, 15] and [r["attempts"] for r in results] == [3, 1]
    calls.clear()
    results = queue.drain(["offline"], lambda job: {"returncode": 1, "failure": "offline"},
                          lambda job: False, waits.append)
    assert results[0]["attempts"] == 1 and results[0]["retry_stopped"]


def test_controller_recovers_network_once_and_stops_on_interrupt():
    attempts = iter([{"returncode": 1, "failure": "offline"}, {"returncode": 0}])
    result = queue.drain(["job"], lambda job: next(attempts), lambda job: True, lambda _: None)
    assert result[0]["attempts"] == 2 and result[0]["returncode"] == 0
    result = queue.drain(["a", "b"], lambda job: {"returncode": 130, "failure": "interrupted"},
                         lambda job: pytest.fail("no retry"), lambda _: pytest.fail("no sleep"))
    assert len(result) == 1
