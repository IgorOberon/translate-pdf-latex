"""One-shot current-user scheduled supervisor, with file-backed receipts.

No shell detachment tricks, recurring schedule, credential storage, or automatic
restart. Task Scheduler is the boundary from the initiating console/job object.
"""
from contextlib import contextmanager
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

import psutil

from paths import ROOT, venv_python, lms
from provenance import canonical_hash, inside, write_json
from endpoint_policy import validate_endpoint
import windows_tasks


TERMINAL = {"done", "failed", "stopped", "needs_attention"}
SUBMISSION_GRACE_SECONDS = 30
ENVIRONMENT_PATHS = ("PATH", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
                     "TRANSFORMERS_CACHE", "TORCH_HOME", "TESSDATA_PREFIX",
                     "TESSERACT_EXE", "PANDOC_EXE", "XELATEX_EXE", "MINERU_EXE", "MARKER_EXE")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def identity(pid=None):
    process = psutil.Process(pid or os.getpid())
    row = {"pid": process.pid, "create_time": process.create_time(), "username": process.username()}
    if pid is None and os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleWindow.restype = ctypes.c_void_p
        row["console_hwnd"] = int(kernel.GetConsoleWindow() or 0)
    return row


def alive(row):
    try:
        return bool(row and psutil.Process(row["pid"]).create_time() == row["create_time"])
    except (KeyError, psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def stop_child(row):
    """Only the recorded child identity and its current descendants, no name scan."""
    if not alive(row):
        return
    process = psutil.Process(row["pid"])
    victims = process.children(recursive=True) + [process]
    for victim in victims:
        try:
            victim.kill()  # psutil checks process identity against PID reuse.
        except psutil.NoSuchProcess:
            pass
    _, pending = psutil.wait_procs(victims, timeout=20)
    if pending:
        raise RuntimeError("owned pipeline child did not stop")


@contextmanager
def launch_lock(folder):
    """OS lock is released even if the submitting console disappears."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "launch.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def latest_run(root):
    folder = Path(root).resolve() / "background"
    if not (folder / "latest.json").is_file():
        return None
    pointer = read(folder / "latest.json")
    run = inside(folder, pointer["run"])
    if run.parent != folder or not run.name.startswith("run-"):
        raise ValueError("invalid background run pointer")
    return run


def status(root):
    run = latest_run(root)
    if run is None:
        return {"status": "not_started"}
    state = read(run / "state.json")
    result = dict(state, run_dir=str(run))
    if state["status"] in TERMINAL:
        return result
    if alive(state.get("worker")):
        return result
    try:
        scheduler = windows_tasks.task_status(state["task_name"])
        result["scheduler"] = scheduler
    except windows_tasks.TaskSchedulerError as exc:
        result.update(status="needs_attention", reason=str(exc))
        return result
    age = time.time() - state.get("submitted_at", 0)
    submitting = (state["status"] == "submitted" and not state.get("worker")
                  and 0 <= age < SUBMISSION_GRACE_SECONDS
                  and not any((run / name).exists() for name in (
                      "task.started.json", "submission.error.json", "bootstrap.error.json", "stop.json")))
    if (run / "bootstrap.error.json").exists():
        result.update(status="needs_attention", reason="worker bootstrap failed; inspect bootstrap.error.json")
    elif scheduler.get("state_name") in {"running", "queued"}:
        result["status"] = "starting" if not state.get("worker") else "needs_attention"
    elif submitting:
        # latest.json is visible before RegisterTask/Run finish. A missing or
        # ready task in this bounded window is not a failed worker receipt.
        result.update(status="starting", reason="task submission is in progress")
    else:
        result.update(status="needs_attention", reason="scheduled worker is not running; inspect receipt/log")
    if alive(state.get("child")):
        result.update(status="needs_attention", reason="orphan pipeline child is still alive; no duplicate launch")
    return result


def start(root, *, start_lmstudio=False, lmstudio_profile=None, lmstudio_models=None, lmstudio_backend=None,
          lms_exe=None, lmstudio_app=None, startup_timeout=120):
    if os.name != "nt":
        raise ValueError("background launch requires Windows Task Scheduler")
    import pipeline
    root = Path(root).resolve()
    job = pipeline.load_job(root)
    validate_endpoint(job["settings"]["host"], allow_remote=False)
    if not 1 <= startup_timeout <= 1800:
        raise ValueError("startup timeout must be between 1 and 1800 seconds")
    if not start_lmstudio and any((lmstudio_profile, lmstudio_models, lmstudio_backend, lms_exe, lmstudio_app)):
        raise ValueError("LM Studio options require --start-lmstudio")
    state = pipeline.status(root)
    if state["status"] == "done":
        return {"status": "done", "reason": "completed job verified; no processes started"}
    if state["status"] == "running":
        raise ValueError("pipeline already running")
    raw_state = pipeline.read_state(root)
    if pipeline.alive(raw_state.get("child_pid"), raw_state.get("child_started")):
        raise ValueError("pipeline child is still alive; no duplicate launch")
    python = Path(venv_python()).resolve()
    pythonw = python.with_name("pythonw.exe")
    if not pythonw.is_file():
        raise ValueError("pythonw.exe beside the project Python is required for a hidden scheduled worker")
    folder = root / "background"
    with launch_lock(folder):
        previous = latest_run(root)
        if previous:
            old = status(root)
            if (old["status"] not in TERMINAL or old.get("orphan_untracked")
                    or alive(old.get("worker")) or alive(old.get("child"))):
                raise ValueError("background run or its child is still active; inspect background-status")
            task = windows_tasks.task_status(old["task_name"])
            if task.get("state_name") in {"running", "queued"}:
                raise ValueError("previous task still active; no duplicate launch")
        run_id = uuid.uuid4().hex
        run = folder / ("run-" + run_id)
        run.mkdir()
        task_name = "FreeTranslate-" + run_id
        config = {"schema": 1, "job": str(root), "run_id": run_id, "task_name": task_name,
                  "python": str(python), "job_sha256": canonical_hash(job),
                  "code_sha256": pipeline.code_identity(), "user_sid": windows_tasks.current_user_sid(),
                  "environment": {key: os.environ[key] for key in ENVIRONMENT_PATHS if key in os.environ},
                  "start_lmstudio": bool(start_lmstudio), "startup_timeout": startup_timeout}
        if start_lmstudio:
            config["lmstudio"] = {
                "profile_dir": str(Path(lmstudio_profile).resolve() if lmstudio_profile else folder / "lmstudio-profile"),
                "lms_path": str(Path(lms_exe or lms()).resolve()),
                "app_path": str(Path(lmstudio_app).resolve()) if lmstudio_app else None,
                "models_dir": str(Path(lmstudio_models).resolve()) if lmstudio_models else None,
                "backend_dir": str(Path(lmstudio_backend).resolve()) if lmstudio_backend else None}
        write_json(run / "request.json", config)
        write_json(run / "state.json", {"schema": 1, "status": "submitted", "task_name": task_name,
                   "run_id": run_id, "request_sha256": canonical_hash(config),
                   "submitted_at": time.time(), "log": str(run / "worker.log")})
        write_json(folder / "latest.json", {"schema": 1, "run": run.name})
        try:
            registration = windows_tasks.register_task(task_name,
                [str(pythonw), "-X", "utf8", str(Path(__file__).resolve()), "worker", str(run / "request.json")], ROOT)
            write_json(run / "task.registered.json", registration)
            write_json(run / "task.started.json", windows_tasks.run_task(task_name))
        except Exception as exc:
            # The scheduler may have accepted a request before a transport error.
            # Preserve the task/receipt; never fall back to a duplicate direct run.
            write_json(run / "submission.error.json", {"reason": str(exc), "at": time.time()})
            raise
    return {"status": "submitted", "run_id": run_id, "task_name": task_name,
            "run_dir": str(run), "next_action": "background-status", "log": str(run / "worker.log")}


def stop(root):
    run = latest_run(root)
    if run is None:
        return {"status": "not_started"}
    # Serialize cancellation with submission, including the interval between
    # registration and Run(). A ready task can then be safely unregistered.
    with launch_lock(run.parent):
        return _stop_run(root, latest_run(root))


def _stop_run(root, run):
    stored = read(run / "state.json")
    expected_task = "FreeTranslate-" + run.name.removeprefix("run-")
    if stored.get("task_name") != expected_task:
        raise ValueError("background task does not match its run directory")
    if (stored.get("status") in TERMINAL and stored.get("task_removed")
            and not alive(stored.get("worker")) and not alive(stored.get("child"))
            and not stored.get("orphan_untracked")):
        if not stored.get("server") or stored.get("server_stopped"):
            return dict(stored, run_dir=str(run), stop_requested=False)
    write_json(run / "stop.json", {"requested_at": time.time()})
    result = status(root)
    if not alive(result.get("worker")):
        # Explicit stop can recover a supervisor crash. Receipts bind each
        # operation to its original process, never to a process name alone.
        if result.get("orphan_untracked"):
            return dict(result, status="needs_attention", stop_requested=True,
                        reason="unrecorded child requires manual inspection")
        scheduler = windows_tasks.task_status(expected_task)
        if scheduler.get("state_name") in {"running", "queued"}:
            # A worker may be starting before it has written its identity. It
            # will see stop.json itself; do not race its resource ownership.
            return dict(result, stop_requested=True)
        stop_child(result.get("child"))
        if result.get("server") and not result.get("server_stopped"):
            from lmstudio_runtime import stop_server
            stop_server(result["server"])
            result["server_stopped"] = True
        windows_tasks.delete_task(result["task_name"])
        result.update(status="stopped", task_removed=True, finished_at=time.time())
        write_json(run / "state.json", {k: v for k, v in result.items() if k not in ("run_dir", "scheduler")})
    result["stop_requested"] = True
    return result


def worker(request_path):
    request_path = Path(request_path).resolve()
    run = request_path.parent
    # pythonw has no usable stdio. Open binary-compatible UTF-8 files explicitly.
    # Open before parsing so a damaged request/state remains diagnosable even
    # when the supervisor cannot yet establish ownership of the task.
    with (run / "worker.log").open("a", encoding="utf-8", buffering=1) as log:
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = log, log
        try:
            try:
                config = read(request_path)
                state = read(run / "state.json")
                if not isinstance(config, dict) or not isinstance(state, dict):
                    raise ValueError("background request and state must be JSON objects")
            except Exception as exc:
                traceback.print_exc(file=log)
                write_json(run / "bootstrap.error.json", {
                    "schema": 1, "status": "failed", "stage": "bootstrap",
                    "reason": str(exc), "error_type": type(exc).__name__, "at": time.time()})
                # Preserve the invalid bytes and existing receipt. The parent
                # can report failed startup and explicitly stop the ready task.
                return 1
            return _worker(config, run, state, log)
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr


def _worker(config, run, state, log):
    child = None
    server = None
    exit_code = 1
    outcome = "failed"
    bound_task = None
    class Stopped(Exception):
        pass
    def save(**fields):
        state.update(fields, updated_at=time.time())
        write_json(run / "state.json", state)
    def stop_spawned():
        if child is None or child.poll() is not None:
            return
        if state.get("child"):
            stop_child(state["child"])
        else:
            # Popen may succeed before the receipt can be written. Its live
            # process handle establishes ownership even if identity() failed.
            proc = psutil.Process(child.pid)
            stop_child({"pid": proc.pid, "create_time": proc.create_time()})
        child.wait(timeout=20)
    try:
        expected_task = "FreeTranslate-" + run.name.removeprefix("run-")
        if (config.get("schema") != 1 or config["task_name"] != state["task_name"]
                or config["task_name"] != expected_task
                or canonical_hash(config) != state.get("request_sha256")):
            raise ValueError("invalid background request")
        if windows_tasks.current_user_sid() != config["user_sid"]:
            raise ValueError("scheduled worker identity differs from submitting user")
        bound_task = expected_task
        os.environ.update(config["environment"])
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("FREETRANSLATE_LOCK", None)
        os.environ.pop("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT", None)
        os.environ.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", FREETRANSLATE_LOCAL_ONLY="1")
        import pipeline
        root = Path(config["job"]).resolve()
        job = pipeline.load_job(root)
        if canonical_hash(job) != config["job_sha256"] or pipeline.code_identity() != config["code_sha256"]:
            raise ValueError("job or pipeline code changed after submission; no stages started")
        save(status="starting", worker=identity(), user_sid=config["user_sid"])
        if (run / "stop.json").exists():
            raise Stopped("stopped before startup")
        if config["start_lmstudio"]:
            from lmstudio_runtime import ensure_server
            save(stage="lmstudio_start")
            server = ensure_server(job["settings"]["host"], config["lmstudio"]["lms_path"], run,
                profile_dir=config["lmstudio"]["profile_dir"], app_path=config["lmstudio"]["app_path"],
                models_dir=config["lmstudio"]["models_dir"], backend_dir=config["lmstudio"].get("backend_dir"),
                timeout=config["startup_timeout"])
            save(server=server)
            os.environ["FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT"] = str(
                Path(server["profile"]) / "freetranslate-runtime.json")
        if (run / "stop.json").exists():
            raise Stopped("stopped before pipeline startup")
        # A hidden new console belongs to this scheduled pipeline child. All
        # its console descendants inherit it, never the submitting console.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        command = [config["python"], "-X", "utf8", str(ROOT / "scripts/pipeline.py"), "run", str(root)]
        with (run / "pipeline.log").open("ab", buffering=0) as output:
            child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, startupinfo=startup,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            child_identity = identity(child.pid)
            save(status="running", stage="pipeline", child=child_identity,
                 pipeline_log=str(run / "pipeline.log"))
            while child.poll() is None:
                if (run / "stop.json").exists():
                    raise Stopped("explicit stop request")
                save()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            exit_code = child.returncode
        result = pipeline.status(root)
        passed = exit_code == 0 and result.get("status") == "done"
        outcome = "done" if passed else "failed"
        exit_code = 0 if passed else exit_code or 1
        save(status="finishing", returncode=exit_code, pipeline=result,
             reason=None if passed else "pipeline refused; inspect pipeline log; no automatic retry")
    except Stopped as exc:
        stop_spawned()
        outcome, exit_code = "stopped", 130
        save(status="finishing", reason=str(exc))
    except BaseException as exc:
        traceback.print_exc(file=log)
        try:
            stop_spawned()
        except Exception as cleanup:
            save(orphan_untracked=True, child_stop_error=str(cleanup))
        save(status="finishing", reason=str(exc), error_type=type(exc).__name__)
        outcome, exit_code = "failed", 1
    finally:
        save(status="finishing")
        if server is not None:
            try:
                from lmstudio_runtime import stop_server
                stop_server(server)
                save(server_stopped=True)
            except Exception as exc:
                outcome, exit_code = "needs_attention", 1
                save(server_stop_error=str(exc))
        try:
            # One-shot tasks are not persistent services or scheduled retries.
            if bound_task is not None:
                windows_tasks.delete_task(bound_task)
                save(task_removed=True)
        except Exception as exc:
            outcome, exit_code = "needs_attention", 1
            save(task_cleanup_error=str(exc))
        save(status=outcome, returncode=exit_code, finished_at=time.time())
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("worker",))
    parser.add_argument("request")
    args = parser.parse_args()
    return worker(args.request)


if __name__ == "__main__":
    sys.exit(main())
