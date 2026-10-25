"""Supervisor acceptance, interrupted submission and cleanup cannot fake success."""
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import background_run as background
import lmstudio_runtime
import pipeline
from provenance import write_json


@pytest.fixture
def setup(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows-only supervisor")
    job = {"schema": 1, "settings": {"host": "http://127.0.0.1:1234"}}
    scheduler = {}
    events = []
    root = tmp_path / "new-job"
    root.mkdir()
    write_json(root / pipeline.STATE, {"schema": 1, "status": "ready"})
    python = tmp_path / "python.exe"
    python.write_bytes(b"synthetic executable, never run")
    python.with_name("pythonw.exe").write_bytes(b"synthetic executable, never run")
    monkeypatch.setattr(pipeline, "load_job", lambda path: job)
    monkeypatch.setattr(pipeline, "status", lambda path: {"status": "ready"})
    monkeypatch.setattr(pipeline, "code_identity", lambda: "code-before")
    monkeypatch.setattr(background, "venv_python", lambda: str(python))
    monkeypatch.setattr(background.windows_tasks, "current_user_sid", lambda: "S-1-test-user")
    monkeypatch.setattr(background, "alive", lambda row: bool(row and row.get("live")))
    monkeypatch.setattr(background, "identity", lambda pid=None: {
        "pid": pid or 1001, "create_time": 1.0, "username": "test-user", "live": True})
    # _worker configures only this private environment mapping.
    monkeypatch.setattr(background.os, "environ", dict(os.environ))

    def register(name, command, cwd):
        assert name not in scheduler
        events.append(("register", name))
        scheduler[name] = {"exists": True, "state_name": "ready", "command": command}
        return dict(scheduler[name])

    def launch(name):
        events.append(("run", name))
        scheduler[name]["state_name"] = "running"
        return {"instance_guid": "test-instance"}

    def remove(name):
        events.append(("delete", name))
        scheduler.pop(name, None)
        return {"deleted": True}

    monkeypatch.setattr(background.windows_tasks, "register_task", register)
    monkeypatch.setattr(background.windows_tasks, "run_task", launch)
    monkeypatch.setattr(background.windows_tasks, "delete_task", remove)
    monkeypatch.setattr(background.windows_tasks, "task_status", lambda name: dict(
        scheduler.get(name, {"exists": False})))
    return SimpleNamespace(root=root, job=job, events=events, scheduler=scheduler,
                           python=python, monkeypatch=monkeypatch)


def launch(setup, **kwargs):
    result = background.start(setup.root, **kwargs)
    run = Path(result["run_dir"])
    return run, background.read(run / "request.json"), background.read(run / "state.json")


def invoke(config, run, state):
    return background._worker(config, run, state, io.StringIO())


def no_child(monkeypatch):
    monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: pytest.fail("no child may start"))
    monkeypatch.setattr(lmstudio_runtime, "ensure_server", lambda *a, **kw: pytest.fail("no server may start"))


def test_completed_job_does_not_create_background_state(setup):
    setup.monkeypatch.setattr(pipeline, "status", lambda root: {"status": "done"})
    assert background.start(setup.root)["status"] == "done"
    assert not (setup.root / "background").exists()
    assert not setup.events


@pytest.mark.parametrize("holder", ["worker", "child"])
def test_terminal_receipt_with_live_worker_or_orphan_refuses_duplicate(setup, holder):
    run, config, state = launch(setup)
    state.update(status="failed", **{holder: {"pid": 1001, "live": True}})
    write_json(run / "state.json", state)
    setup.scheduler[config["task_name"]]["state_name"] = "ready"
    with pytest.raises(ValueError, match="still active"):
        background.start(setup.root)
    assert [event[0] for event in setup.events].count("register") == 1


@pytest.mark.parametrize("task_state", ["queued", "running"])
def test_submission_without_worker_receipt_cannot_start_duplicate(setup, task_state):
    run, config, state = launch(setup)
    setup.scheduler[config["task_name"]]["state_name"] = task_state
    assert background.status(setup.root)["status"] == "starting"
    with pytest.raises(ValueError, match="still active"):
        background.start(setup.root)
    assert len(list((setup.root / "background").glob("run-*"))) == 1


def test_scheduler_accepted_run_then_transport_failed_preserves_single_run(setup):
    def uncertain_launch(name):
        setup.scheduler[name]["state_name"] = "running"
        raise background.windows_tasks.TaskSchedulerError("lost receipt after dispatch")

    setup.monkeypatch.setattr(background.windows_tasks, "run_task", uncertain_launch)
    with pytest.raises(background.windows_tasks.TaskSchedulerError, match="lost receipt"):
        background.start(setup.root)
    run = background.latest_run(setup.root)
    assert "lost receipt" in background.read(run / "submission.error.json")["reason"]
    assert background.status(setup.root)["status"] == "starting"
    with pytest.raises(ValueError, match="still active"):
        background.start(setup.root)
    assert [event[0] for event in setup.events] == ["register"]


def test_missing_worker_after_scheduler_finishes_is_never_success(setup):
    run, config, _ = launch(setup)
    setup.scheduler[config["task_name"]].update(state_name="ready", last_task_result=1)
    result = background.status(setup.root)
    assert result["status"] == "needs_attention"
    assert "not running" in result["reason"]


@pytest.mark.parametrize("phase", ["before_registration", "before_dispatch"])
def test_status_during_submission_is_starting_and_cannot_launch_duplicate(setup, phase):
    original = getattr(background.windows_tasks, "register_task" if phase == "before_registration" else "run_task")

    def observe(*args):
        result = background.status(setup.root)
        assert result["status"] == "starting"
        assert "submission" in result["reason"]
        return original(*args)

    setup.monkeypatch.setattr(background.windows_tasks,
                             "register_task" if phase == "before_registration" else "run_task", observe)
    launch(setup)
    assert [event[0] for event in setup.events] == ["register", "run"]


@pytest.mark.parametrize("failure", ["expired", "submission.error.json", "bootstrap.error.json", "stop.json"])
def test_submission_grace_does_not_hide_errors_or_abandoned_submission(setup, failure):
    run, config, state = launch(setup)
    (run / "task.started.json").unlink()
    setup.scheduler[config["task_name"]]["state_name"] = "ready"
    if failure == "expired":
        state["submitted_at"] -= background.SUBMISSION_GRACE_SECONDS + 1
        write_json(run / "state.json", state)
    else:
        write_json(run / failure, {"reason": "synthetic failure"})
    assert background.status(setup.root)["status"] == "needs_attention"


@pytest.mark.parametrize("changed", ["job", "code", "snapshot", "user"])
def test_worker_rechecks_submission_before_starting_any_process(setup, changed):
    run, config, state = launch(setup)
    no_child(setup.monkeypatch)
    if changed == "job":
        setup.job["extra"] = "changed after submit"
    elif changed == "code":
        setup.monkeypatch.setattr(pipeline, "code_identity", lambda: "code-after")
    elif changed == "snapshot":
        def corrupt_snapshot(path):
            raise ValueError("input snapshot changed")
        setup.monkeypatch.setattr(pipeline, "load_job", corrupt_snapshot)
    else:
        setup.monkeypatch.setattr(background.windows_tasks, "current_user_sid", lambda: "S-1-other-user")
    assert invoke(config, run, state) != 0
    result = background.read(run / "state.json")
    assert result["status"] == "failed"
    if changed == "user":
        assert not any(event[0] == "delete" for event in setup.events)
    else:
        assert any(event[0] == "delete" for event in setup.events)


def test_explicit_stop_before_startup_writes_marker_and_starts_nothing(setup):
    run, config, state = launch(setup)
    no_child(setup.monkeypatch)
    result = background.stop(setup.root)
    assert result["stop_requested"] is True
    assert (run / "stop.json").is_file()
    assert invoke(config, run, state) != 0
    assert background.read(run / "state.json")["status"] == "stopped"


def test_server_start_failure_is_recorded_and_task_removed(setup):
    run, config, state = launch(setup, start_lmstudio=True, lms_exe=str(setup.python))
    def failed_start(*a, **kw):
        raise RuntimeError("dedicated server failed to become ready")
    setup.monkeypatch.setattr(lmstudio_runtime, "ensure_server", failed_start)
    setup.monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: pytest.fail("no pipeline may start"))
    assert invoke(config, run, state) != 0
    final = background.read(run / "state.json")
    assert final["status"] == "failed" and "failed to become ready" in final["reason"]
    assert final["task_removed"] is True


class CompleteChild:
    pid = 9002
    returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, **kwargs):
        return self.returncode


def completed_pipeline(setup, returncode=0, final="done"):
    child = CompleteChild()
    child.returncode = returncode
    setup.monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: child)
    setup.monkeypatch.setattr(pipeline, "status", lambda path: {"status": final})
    return child


@pytest.mark.parametrize("returncode,final", [(1, "done"), (0, "needs_attention")])
def test_child_exit_and_pipeline_receipt_must_both_pass(setup, returncode, final):
    run, config, state = launch(setup)
    completed_pipeline(setup, returncode, final)
    assert invoke(config, run, state) != 0
    assert background.read(run / "state.json")["status"] == "failed"


def test_server_cleanup_failure_never_exposes_done_or_success_exit(setup):
    run, config, state = launch(setup, start_lmstudio=True, lms_exe=str(setup.python))
    completed_pipeline(setup)
    setup.monkeypatch.setattr(lmstudio_runtime, "ensure_server", lambda *a, **kw: {
        "owned": True, "profile": str(run / "isolated-profile")})
    observed = []
    def failed_stop(server):
        observed.append(background.read(run / "state.json")["status"])
        raise RuntimeError("owned server cleanup failed")
    setup.monkeypatch.setattr(lmstudio_runtime, "stop_server", failed_stop)
    rc = invoke(config, run, state)
    final = background.read(run / "state.json")
    assert observed != ["done"], "completion must wait for server cleanup"
    assert final["status"] == "needs_attention"
    assert rc != 0, "failed cleanup must not return a successful worker exit"


def test_mismatched_task_binding_cannot_unregister_another_task(setup):
    run, config, state = launch(setup)
    no_child(setup.monkeypatch)
    unrelated = "FreeTranslate-" + "f" * 32
    config["task_name"] = unrelated
    setup.scheduler[unrelated] = {"exists": True, "state_name": "running"}
    assert invoke(config, run, state) != 0
    assert unrelated in setup.scheduler
    assert ("delete", unrelated) not in setup.events


def test_untracked_orphan_flag_requires_attention_before_another_start(setup):
    run, config, state = launch(setup)
    state.update(status="failed", orphan_untracked=True, child_stop_error="identity unavailable")
    write_json(run / "state.json", state)
    setup.scheduler[config["task_name"]]["state_name"] = "ready"
    with pytest.raises(ValueError, match="active|orphan|attention"):
        background.start(setup.root)
    assert [event[0] for event in setup.events].count("register") == 1


def test_post_submission_command_change_refuses_process_start(setup):
    run, config, state = launch(setup)
    no_child(setup.monkeypatch)
    config["environment"]["PATH"] = "unreviewed path change"
    assert invoke(config, run, state) != 0
    assert background.read(run / "state.json")["status"] == "failed"


def test_stop_requested_during_server_start_never_starts_pipeline(setup):
    run, config, state = launch(setup, start_lmstudio=True, lms_exe=str(setup.python))
    stopped = []
    server = {"owned": True, "profile": str(run / "isolated-profile")}
    def server_start(*a, **kw):
        write_json(run / "stop.json", {"requested_at": 1})
        return server
    setup.monkeypatch.setattr(lmstudio_runtime, "ensure_server", server_start)
    setup.monkeypatch.setattr(lmstudio_runtime, "stop_server", lambda server: stopped.append(server))
    setup.monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: pytest.fail("stop must prevent spawn"))
    assert invoke(config, run, state) != 0
    assert background.read(run / "state.json")["status"] == "stopped"
    assert stopped == [server]


def test_spawned_child_is_cleaned_when_identity_receipt_fails(setup):
    run, config, state = launch(setup)
    child = CompleteChild()
    child.returncode = None
    setup.monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: child)
    original_identity = background.identity
    def identity_fails_for_child(pid=None):
        if pid == child.pid:
            raise RuntimeError("receipt identity failed")
        return original_identity(pid)
    setup.monkeypatch.setattr(background, "identity", identity_fails_for_child)
    setup.monkeypatch.setattr(background.psutil, "Process", lambda pid: SimpleNamespace(
        pid=pid, create_time=lambda: 5.0))
    stopped = []
    def stop_recorded(row):
        stopped.append(row)
        child.returncode = 1
    setup.monkeypatch.setattr(background, "stop_child", stop_recorded)
    assert invoke(config, run, state) != 0
    assert stopped == [{"pid": child.pid, "create_time": 5.0}]
    assert background.read(run / "state.json")["status"] == "failed"


def test_task_cleanup_failure_cannot_claim_success(setup):
    run, config, state = launch(setup)
    completed_pipeline(setup)
    def fail_delete(name):
        raise background.windows_tasks.TaskSchedulerError("scheduler unavailable during cleanup")
    setup.monkeypatch.setattr(background.windows_tasks, "delete_task", fail_delete)
    assert invoke(config, run, state) != 0
    final = background.read(run / "state.json")
    assert final["status"] == "needs_attention"
    assert "scheduler unavailable" in final["task_cleanup_error"]


def test_explicit_stop_recovers_only_recorded_orphan_and_owned_server(setup):
    run, config, state = launch(setup)
    state.update(status="running", worker={"pid": 1001, "live": False},
                 child={"pid": 9002, "create_time": 5.0, "live": True},
                 server={"owned": True})
    write_json(run / "state.json", state)
    setup.scheduler[config["task_name"]]["state_name"] = "ready"
    stopped = []
    setup.monkeypatch.setattr(background, "stop_child", lambda row: stopped.append(("child", row)))
    setup.monkeypatch.setattr(lmstudio_runtime, "stop_server", lambda row: stopped.append(("server", row)))
    result = background.stop(setup.root)
    assert result["status"] == "stopped" and result["stop_requested"] is True
    assert stopped == [("child", state["child"]), ("server", state["server"])]
    assert result["task_removed"] and result["server_stopped"]


def test_process_identity_does_not_treat_reused_pid_as_owned(monkeypatch):
    monkeypatch.setattr(background.psutil, "Process", lambda pid: SimpleNamespace(
        create_time=lambda: 99.0))
    assert background.alive({"pid": 9002, "create_time": 5.0}) is False
    # stop_child must return before looking up/killing any descendants.
    background.stop_child({"pid": 9002, "create_time": 5.0})


def test_stop_does_not_ignore_untracked_orphan_after_task_removal(setup):
    run, config, state = launch(setup)
    state.update(status="failed", task_removed=True, orphan_untracked=True,
                 worker={"pid": 1001, "live": False})
    write_json(run / "state.json", state)
    setup.scheduler.pop(config["task_name"])
    setup.monkeypatch.setattr(background, "stop_child", lambda row: pytest.fail("unknown process must not be killed"))
    result = background.stop(setup.root)
    assert result["status"] == "needs_attention" and result["stop_requested"] is True
    assert "manual inspection" in result["reason"]


def test_stop_recovers_live_child_even_with_terminal_removed_task_receipt(setup):
    run, config, state = launch(setup)
    state.update(status="failed", task_removed=True, worker={"pid": 1001, "live": False},
                 child={"pid": 9002, "create_time": 5.0, "live": True})
    write_json(run / "state.json", state)
    setup.scheduler.pop(config["task_name"])
    stopped = []
    setup.monkeypatch.setattr(background, "stop_child", lambda row: stopped.append(row))
    result = background.stop(setup.root)
    assert result["status"] == "stopped" and result["stop_requested"] is True
    assert stopped == [state["child"]]


def test_stop_unregisters_ready_task_when_no_worker_ever_started(setup):
    run, config, state = launch(setup)
    setup.scheduler[config["task_name"]]["state_name"] = "ready"
    result = background.stop(setup.root)
    assert result["status"] == "stopped" and result["stop_requested"] is True
    assert config["task_name"] not in setup.scheduler
    assert background.read(run / "state.json")["task_removed"] is True


@pytest.mark.parametrize("scheduler_state", ["queued", "running"])
def test_stop_before_worker_receipt_leaves_active_task_to_observe_marker(setup, scheduler_state):
    run, config, state = launch(setup)
    setup.scheduler[config["task_name"]]["state_name"] = scheduler_state
    result = background.stop(setup.root)
    assert result["stop_requested"] is True
    assert result["status"] == "starting"
    assert config["task_name"] in setup.scheduler
    assert (run / "stop.json").is_file()


@pytest.mark.parametrize("filename,damaged", [
    ("request.json", b'{"unfinished":'),
    ("state.json", b"not JSON"),
    ("request.json", b"[]"),
])
def test_bootstrap_parse_failure_logs_and_preserves_corrupt_evidence(setup, filename, damaged):
    run, config, state = launch(setup)
    path = run / filename
    path.write_bytes(damaged)
    setup.monkeypatch.setattr(background, "_worker", lambda *a: pytest.fail("invalid config must not run"))
    streams = (sys.stdout, sys.stderr)
    assert background.worker(run / "request.json") == 1
    assert (sys.stdout, sys.stderr) == streams
    assert path.read_bytes() == damaged
    assert "Traceback" in (run / "worker.log").read_text(encoding="utf-8")
    receipt = background.read(run / "bootstrap.error.json")
    assert receipt["status"] == "failed" and receipt["stage"] == "bootstrap"
    assert config["task_name"] in setup.scheduler, "unvalidated bootstrap must not mutate task ownership"


@pytest.mark.skipif(os.name != "nt", reason="Windows OS file lock")
def test_launch_lock_blocks_simultaneous_submit_and_releases(tmp_path):
    folder = tmp_path / "only-new-fixture"
    with background.launch_lock(folder):
        with pytest.raises(OSError):
            with background.launch_lock(folder):
                pytest.fail("second lock must not be acquired")
    with background.launch_lock(folder):
        pass
