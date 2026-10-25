"""Scheduler boundary: current-user ownership, literal argv and no overwrite."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import windows_tasks


@pytest.mark.parametrize("name", ["", "Other-" + "a" * 32,
                                  "FreeTranslate-../foreign", "\\Microsoft\\Task",
                                  "FreeTranslate-" + "a" * 32 + "\n"])
def test_foreign_names_never_reach_scheduler(name, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must not spawn"))
    for operation in (windows_tasks.run_task, windows_tasks.task_status, windows_tasks.delete_task):
        with pytest.raises(ValueError):
            operation(name)


def test_task_action_requires_absolute_executable_and_directory(tmp_path):
    name = "FreeTranslate-" + uuid.uuid4().hex
    with pytest.raises(ValueError):
        windows_tasks.register_task(name, ["python.exe"], tmp_path)
    script = tmp_path / "untrusted.cmd"
    script.write_text("echo no", encoding="utf-8")
    with pytest.raises(ValueError):
        windows_tasks.register_task(name, [str(script)], tmp_path)
    with pytest.raises(ValueError):
        windows_tasks.register_task(name, [sys.executable, "bad\0arg"], tmp_path)
    with pytest.raises(ValueError):
        windows_tasks.register_task(name, [sys.executable], Path("relative"))


@pytest.mark.skipif(os.name != "nt", reason="Windows scheduler boundary")
def test_adapter_passes_utf8_json_without_shell_interpolation(tmp_path, monkeypatch):
    name = "FreeTranslate-" + uuid.uuid4().hex
    unusual = ['Кириллица и пробелы', '$(Get-Secret)', 'a"b', 'end\\', '']
    inspected = {}

    def fake_run(command, **kwargs):
        assert "-File" in command and "-Command" not in command
        assert kwargs.get("shell", False) is False
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert kwargs["stdin"] == subprocess.DEVNULL
        spec_path = Path(command[-1])
        inspected.update(json.loads(spec_path.read_text(encoding="utf-8")))
        assert command[-3].endswith("windows_task.ps1")
        assert command[-2] == "-SpecPath"
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    windows_tasks.register_task(name, [sys.executable, *unusual], tmp_path)
    assert inspected["arguments"] == subprocess.list2cmdline(unusual)
    assert inspected["name"] == name
    assert inspected["operation"] == "register"


@pytest.mark.skipif(os.name != "nt", reason="Windows scheduler boundary")
@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout='{"ok":false,"error":"Access denied"}', stderr=""),
    SimpleNamespace(returncode=0, stdout="", stderr="No receipt"),
    SimpleNamespace(returncode=0, stdout='[]', stderr=""),
])
def test_scheduler_failures_have_no_direct_launch_fallback(monkeypatch, result):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a) or result)
    with pytest.raises(windows_tasks.TaskSchedulerError):
        windows_tasks.task_status("FreeTranslate-" + uuid.uuid4().hex)
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Task Scheduler")
def test_real_current_user_task_literal_arguments_no_overwrite_and_cleanup(tmp_path):
    """Only a new synthetic output is writable; leave no registered task behind."""
    name = "FreeTranslate-" + uuid.uuid4().hex
    output = tmp_path / "запуск с пробелами.json"
    executable = Path(sys.executable).with_name("pythonw.exe")
    assert executable.is_file(), "Windows Python must provide pythonw for hidden tasks"
    literals = ['Кириллица и пробелы', '$(Get-Secret)', 'a"b', 'end\\', '']
    script = ("import json,pathlib,sys; "
              "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:], ensure_ascii=False), encoding='utf-8')")
    created = False
    try:
        receipt = windows_tasks.register_task(
            name, [str(executable), "-X", "utf8", "-c", script, str(output), *literals], tmp_path)
        created = True
        assert receipt["principal"]["user_sid"] == windows_tasks.current_user_sid()
        assert receipt["principal"]["logon_type"] == 3
        assert receipt["principal"]["run_level"] == 0
        settings = receipt["settings"]
        assert settings["execution_time_limit"] == "PT0S"
        assert settings["hidden"] is True
        assert settings["multiple_instances"] == 2
        assert settings["restart_count"] == settings["trigger_count"] == 0
        assert settings["run_only_if_idle"] is False
        assert settings["run_only_if_network_available"] is False
        assert settings["stop_if_going_on_batteries"] is False
        assert settings["disallow_start_if_on_batteries"] is False
        with pytest.raises(windows_tasks.TaskSchedulerError):
            windows_tasks.register_task(name, [str(executable), "-c", "raise SystemExit(99)"], tmp_path)
        assert windows_tasks.task_status(name)["action"] == receipt["action"]
        running = windows_tasks.run_task(name)
        assert running["instance_guid"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = windows_tasks.task_status(name)
            if output.is_file() and state["state_name"] == "ready":
                break
            time.sleep(0.2)
        assert output.is_file(), state
        assert json.loads(output.read_text(encoding="utf-8")) == literals
        assert state["state_name"] == "ready" and state["last_task_result"] == 0
    finally:
        if created:
            assert windows_tasks.delete_task(name)["deleted"] is True
    assert windows_tasks.task_status(name)["exists"] is False
    assert windows_tasks.delete_task(name)["deleted"] is False
