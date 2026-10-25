"""Real isolated LM Studio + Task Scheduler + closing a private console.

No model is loaded; ordinary LM Studio and its profile stay untouched. Output
must be a new directory. This opt-in integration does not run in unit/smoke tests.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
import uuid

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lmstudio_runtime as lm
import windows_tasks as tasks


def write(path, value):
    lm._write(Path(path), value)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def wait_file(path, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return read(path)
        time.sleep(0.2)
    raise RuntimeError("timeout waiting for " + str(path))


def shared_state(lms):
    state = {}
    for key, argv in (("daemon", ["daemon", "status", "--json"]),
                      ("server", ["server", "status", "--json"]), ("models", ["ps", "--json"])):
        p = subprocess.run([lms, *argv], capture_output=True, text=True, encoding="utf-8",
                           timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        state[key] = {"rc": p.returncode, "data": json.loads(p.stdout) if p.stdout.strip() else None}
        if key == "daemon" and (p.returncode or (state[key]["data"] or {}).get("status") != "running"):
            # lms ps can wake an ordinary app. Do not run it if no shared core exists.
            break
    return state


def worker(out):
    config = read(out / "config.json")
    receipt = None
    result = {"status": "failed"}
    with (out / "worker.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = log
        sys.stderr = log
        try:
            receipt = lm.ensure_server(config["host"], config["lms"], out / "lm-logs",
                                       profile_dir=out / "lm-profile", app_path=config["app"], timeout=90)
            ready = {"pid": os.getpid(), "username": psutil.Process().username(),
                     "receipt": receipt, "time": time.time(),
                     "native_gpu_handoff": lm.unload_owned_models(receipt)}
            write(out / "worker.ready.json", ready)
            deadline = time.monotonic() + 180
            ticks = 0
            while not (out / "stop.json").exists():
                ticks += 1
                write(out / "heartbeat.json", {"tick": ticks, "time": time.time(), "models_visible": lm._probe(config["host"]), "worker_pid": os.getpid()})
                if time.monotonic() > deadline:
                    raise RuntimeError("test coordinator did not send stop before safety deadline")
                time.sleep(0.5)
            result = {"status": "passed", "ticks": ticks}
        except Exception as exc:
            traceback.print_exc()
            result = {"status": "failed", "error": str(exc)}
        finally:
            if receipt:
                try:
                    result["cleanup"] = lm.stop_server(receipt)
                except Exception as exc:
                    traceback.print_exc()
                    result.update(status="failed", cleanup_error=str(exc))
            write(out / "worker.final.json", result)
    return 0 if result["status"] == "passed" else 1


def submit(out):
    config = read(out / "config.json")
    result = {"pid": os.getpid(), "username": psutil.Process().username(), "registered": False}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleWindow.restype = ctypes.c_void_p
    result["console_hwnd"] = int(kernel.GetConsoleWindow() or 0)
    try:
        command = [str(Path(sys.executable).with_name("pythonw.exe")), "-X", "utf8", str(Path(__file__).resolve()), "--worker", str(out)]
        result["registration"] = tasks.register_task(config["task"], command, ROOT)
        result["registered"] = True
        result["run"] = tasks.run_task(config["task"])
        result["status"] = "submitted"
    except Exception as exc:
        result.update(status="failed", error=str(exc))
    write(out / "submit.json", result)
    if result["status"] != "submitted":
        return 1
    # Remain in our own console so the coordinator can really send WM_CLOSE.
    time.sleep(240)
    return 2


def integration(out, lms, app):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = {"task": "FreeTranslate-" + uuid.uuid4().hex, "lms": str(Path(lms).resolve()),
              "app": str(Path(app).resolve()), "host": f"http://127.0.0.1:{port}"}
    write(out / "config.json", config)
    report = {"status": "failed", "host": config["host"], "task": config["task"],
              "before_shared": shared_state(config["lms"]), "model_inference": "not_run"}
    launched = None
    submission = {}
    try:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        with (out / "submit.log").open("wb") as log:
            launched = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--submit", str(out)],
                                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        submission = wait_file(out / "submit.json", 60)
        report["submission"] = submission
        if submission["status"] != "submitted":
            raise RuntimeError("scheduled worker was not submitted: " + submission.get("error", "unknown"))
        deadline = time.monotonic() + 120
        while not (out / "worker.ready.json").exists():
            if (out / "worker.final.json").exists():
                raise RuntimeError("LM worker failed: " + str(read(out / "worker.final.json")))
            if time.monotonic() > deadline:
                raise RuntimeError("LM worker readiness timed out")
            time.sleep(0.25)
        ready = read(out / "worker.ready.json")
        report["ready"] = ready
        if ready["username"] != psutil.Process().username() or submission["username"] != ready["username"]:
            raise RuntimeError("test processes do not run as current user")
        launch_tree = {launched.pid, *(p.pid for p in psutil.Process(launched.pid).children(recursive=True))}
        if not submission["console_hwnd"] or submission["pid"] not in launch_tree:
            raise RuntimeError("private submitting console identity not established")
        heartbeat_before = wait_file(out / "heartbeat.json")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        if not user32.PostMessageW(submission["console_hwnd"], 0x10, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        report["console_close_rc"] = launched.wait(timeout=15)
        closed_at = time.time()
        time.sleep(3)
        heartbeat_after = read(out / "heartbeat.json")
        report["heartbeat_before"] = heartbeat_before
        report["heartbeat_after"] = heartbeat_after
        report["after_console_models_visible"] = lm._probe(config["host"])
        report["after_console_loaded_instances"] = lm._loaded_ids(lm._http_json(config["host"], "/api/v1/models"))
        report["runtime_alive_after_console"] = lm._same_process(ready["receipt"]["process"])
        if (heartbeat_after["tick"] <= heartbeat_before["tick"] or heartbeat_after["time"] <= closed_at
                or not report["runtime_alive_after_console"] or report["after_console_loaded_instances"]):
            raise RuntimeError("detached worker/isolated LM Studio did not survive console closure")
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        write(out / "stop.json", {"time": time.time()})
        if submission.get("status") == "submitted":
            try:
                final = wait_file(out / "worker.final.json", 120)
                report["worker_final"] = final
                if final.get("status") != "passed":
                    report["status"] = "failed"
            except Exception as exc:
                report.update(status="failed", cleanup_error=str(exc))
        if launched and launched.poll() is None:
            descendants = psutil.Process(launched.pid).children(recursive=True)
            for child in descendants:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            launched.terminate()  # Exact private test launcher handle only.
            launched.wait(timeout=10)
        if submission.get("registered"):
            try:
                report["task_cleanup"] = tasks.delete_task(config["task"])
            except Exception as exc:
                report.update(status="failed", task_cleanup_error=str(exc))
        report["after_shared"] = shared_state(config["lms"])
        report["shared_runtime_unchanged"] = report["before_shared"] == report["after_shared"]
        report["isolated_listener_gone"] = not lm._listener_pid(port)
        if not report["shared_runtime_unchanged"] or not report["isolated_listener_gone"]:
            report["status"] = "failed"
        write(out / "summary.json", report)
    print(json.dumps({"status": report["status"], "report": str(out / "summary.json"), "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path)
    p.add_argument("--lms")
    p.add_argument("--app")
    p.add_argument("--worker", type=Path)
    p.add_argument("--submit", type=Path)
    a = p.parse_args()
    if a.worker:
        return worker(a.worker.resolve())
    if a.submit:
        return submit(a.submit.resolve())
    if not (a.out and a.lms and a.app):
        p.error("--out (new directory), --lms and --app are required")
    return integration(a.out, a.lms, a.app)


if __name__ == "__main__":
    raise SystemExit(main())
