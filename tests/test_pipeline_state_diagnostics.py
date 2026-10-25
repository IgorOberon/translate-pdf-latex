"""Broken state bytes are reported, preserved, and never treated as a new run."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import background_run
import pipeline


@pytest.fixture
def job(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("A synthetic source paragraph.", encoding="utf-8")
    root = tmp_path / "new-job"
    pipeline.initialize(source, root)
    return root


@pytest.mark.parametrize("corrupt", [b'{"schema": 1,', b'\xff\xfeinvalid', b'[]',
                                     b'{"schema":2,"status":"ready"}',
                                     b'{"schema":true,"status":"ready"}',
                                     b'{"schema":1,"status":"invented"}',
                                     b'{"schema":1,"status":[]}'] + [
    json.dumps(dict(schema=1, status="ready", **fields)).encode("utf-8")
    for fields in [
        {"steps": []}, {"steps": {"one": []}}, {"steps": {"one": {"status": []}}},
        {"steps": {"one": {"outputs": []}}},
        {"steps": {"one": {"outputs": {"part.md": 7}}}},
        {"steps": {"one": {"outputs": {"part.md": "x" * 64}}}},
        {"artifacts": ["result.pdf"]}, {"artifacts": {"result.pdf": 7}},
        {"artifacts": {"": "a" * 64}}, {"artifacts": {"result.pdf": "short"}},
        {"job_sha256": []}, {"code_sha256": "invalid"},
        {"pid": "123"}, {"pid": True}, {"pid": -1}, {"pid": 2 ** 64},
        {"child_pid": 1.5}, {"child_pid": 0},
        {"started": "123"}, {"started": True}, {"started": float("nan")},
        {"started": float("inf")}, {"started": 10 ** 400},
        {"child_started": float("-inf")}, {"child_started": 0},
    ]
])
def test_status_reports_corrupt_state_without_repair_or_process_launch(job, monkeypatch, corrupt):
    state = job / pipeline.STATE
    state.write_bytes(corrupt)
    before_paths = sorted(str(path.relative_to(job)) for path in job.rglob("*"))
    monkeypatch.setattr(pipeline.subprocess, "Popen", lambda *a, **kw: pytest.fail("corrupt state cannot launch"))
    result = pipeline.status(job)
    assert result["status"] == "needs_attention" and result["next_action"] == "inspect_state"
    assert result["state_error"]["sha256"] == hashlib.sha256(corrupt).hexdigest()
    assert result["state_error"]["path"] == str(state)
    with pytest.raises(pipeline.StateFileError, match="bytes preserved"):
        pipeline.run_job(job)
    if os.name == "nt":
        with pytest.raises(pipeline.StateFileError, match="bytes preserved"):
            background_run.start(job)
    assert state.read_bytes() == corrupt
    assert sorted(str(path.relative_to(job)) for path in job.rglob("*")) == before_paths


def test_json_error_reports_location_and_cli_stays_read_only(job, monkeypatch, capsys):
    state = job / pipeline.STATE
    corrupt = b'{\n"schema": 1,\n"status":'
    state.write_bytes(corrupt)
    result = pipeline.status(job)
    assert result["state_error"]["line"] == 3
    assert result["state_error"]["column"] > 0
    monkeypatch.setattr(sys, "argv", ["pipeline", "status", str(job)])
    assert pipeline.main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["next_action"] == "inspect_state"
    monkeypatch.setattr(sys, "argv", ["pipeline", "run", str(job)])
    assert pipeline.main() == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "needs_attention" and "bytes preserved" in printed["reason"]
    assert state.read_bytes() == corrupt


def test_valid_ready_state_and_missing_state_keep_existing_semantics(job, tmp_path):
    assert pipeline.status(job)["status"] == "ready"
    assert pipeline.read_state(job)["schema"] == 1
    assert pipeline.status(tmp_path / "not-initialized")["status"] == "not_initialized"


def test_schema_one_optional_fields_and_unknown_metadata_are_preserved(job):
    state = job / pipeline.STATE
    payload = {"schema": 1, "status": "ready", "extra": {"future": [1, 2]},
               "steps": {"one": {"status": "passed", "outputs": {"part.md": "a" * 64},
                                    "future": ["metadata"]}},
               "artifacts": {}, "pid": None, "started": None,
               "child_pid": 123, "child_started": 123.5}
    original = json.dumps(payload, indent=3).encode("utf-8")
    state.write_bytes(original)
    assert pipeline.read_state(job) == payload
    assert pipeline.status(job)["extra"] == payload["extra"]
    assert state.read_bytes() == original


def test_done_with_malformed_artifacts_is_diagnostic_not_stale(job):
    state = job / pipeline.STATE
    original = b'{"schema":1,"status":"done","artifacts":["result.pdf"]}'
    state.write_bytes(original)
    result = pipeline.status(job)
    assert result["status"] == "needs_attention"
    assert result["next_action"] == "inspect_state"
    assert state.read_bytes() == original
