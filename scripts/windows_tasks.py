"""Current-user, on-demand Windows Task Scheduler boundary for durable jobs.

Task Scheduler starts the action outside the caller's console/process job.  This
module never requests elevation, stores a password, or starts a recurring task.
Only tasks created by this adapter for the current user may be run or removed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


_NAME = re.compile(r"FreeTranslate-(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z")
_HELPER = Path(__file__).with_name("windows_task.ps1")


class TaskSchedulerError(RuntimeError):
    """A scheduler request failed; no fallback direct launch was attempted."""


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("task name must be FreeTranslate- followed by a lowercase UUID")
    return name


def _powershell() -> Path:
    # Resolve the system executable, not an arbitrary powershell on PATH.
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not executable.is_file():
        raise TaskSchedulerError("Windows PowerShell is unavailable: " + str(executable))
    return executable


def _request(operation: str, name: str | None = None, **fields) -> dict:
    if name is not None:
        _validate_name(name)
    if os.name != "nt":
        raise TaskSchedulerError("durable tasks require Windows Task Scheduler")
    specification = {"operation": operation, **fields}
    if name is not None:
        specification["name"] = name
    # Keep user paths/arguments out of PowerShell code.  The helper only reads a
    # UTF-8 JSON file; CreateProcess receives a normal argument array (no shell).
    with tempfile.TemporaryDirectory(prefix="freetranslate-scheduler-") as temporary:
        spec_path = Path(temporary) / "request.json"
        spec_path.write_text(json.dumps(specification, ensure_ascii=False), encoding="utf-8")
        command = [str(_powershell()), "-NoLogo", "-NoProfile", "-NonInteractive",
                   "-ExecutionPolicy", "Bypass", "-File", str(_HELPER),
                   "-SpecPath", str(spec_path)]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    encoding="utf-8", errors="replace", timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TaskSchedulerError("Task Scheduler request failed: " + str(error)) from error
    try:
        value = json.loads(result.stdout.lstrip("\ufeff").strip())
    except (ValueError, TypeError) as error:
        detail = (result.stderr or result.stdout).strip()
        raise TaskSchedulerError("Task Scheduler returned no valid receipt: " + detail) from error
    if result.returncode != 0 or not isinstance(value, dict) or not value.get("ok"):
        message = value.get("error", "Task Scheduler request failed") if isinstance(value, dict) else str(value)
        raise TaskSchedulerError(message)
    return value


def register_task(name: str, command: list[str], cwd: Path) -> dict:
    """Create a new task; never update an existing task with the same name.

    command[0] must be an absolute executable.  pythonw.exe is appropriate for
    a silent worker that opens its own UTF-8 logs.  Scheduler's Hidden setting
    only hides the task in its UI; it does not hide a console executable.
    """
    _validate_name(name)
    if not isinstance(command, (list, tuple)) or not command:
        raise ValueError("task command must be a nonempty argument list")
    if any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("task arguments must be strings without NUL")
    executable = Path(command[0])
    directory = Path(cwd)
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("task executable must be an existing absolute path")
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("task working directory must be an existing absolute path")
    if executable.suffix.lower() not in (".exe", ".com"):
        raise ValueError("task action must be an executable, not a shell script")
    return _request("register", name, executable=str(executable.resolve()),
                    arguments=subprocess.list2cmdline(command[1:]),
                    cwd=str(directory.resolve()))


def run_task(name: str) -> dict:
    """Request one on-demand run; duplicate concurrent instances are ignored."""
    return _request("run", name)


def task_status(name: str) -> dict:
    """Return scheduler evidence; a missing task has exists=False."""
    return _request("status", name)


def delete_task(name: str) -> dict:
    """Unregister an owned task only; this does not stop its running action."""
    return _request("delete", name)


def current_user_sid() -> str:
    return _request("identity")["current_user_sid"]
