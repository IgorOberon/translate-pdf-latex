"""Opt-in Windows console-close integration, synthetic book and mock MT only.

Creates THREE hidden private consoles, never attaches to the calling console.
Each starts an ordinary console child (negative control), a scheduled mock HTTP
server, and the real pipeline background launcher. One receives CTRL_C_EVENT;
another receives WM_CLOSE; the third is terminated by its private kill-on-close
Job Object. Both controls must die while the scheduled work
finishes a real Pandoc/XeLaTeX PDF. All evidence is retained in a NEW directory.

python tests/run_detached_integration.py --out <new-directory>
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
sys.path.insert(0, str(ROOT / "scripts"))
from provenance import sha_file, write_json
from paths import venv_python


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def identity(pid=None):
    proc = psutil.Process(pid or os.getpid())
    row = {"pid": proc.pid, "create_time": proc.create_time(),
           "username": proc.username()}
    if proc.pid == os.getpid() and os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleWindow.restype = ctypes.c_void_p
        row["console_hwnd"] = int(kernel.GetConsoleWindow() or 0)
        pids = (ctypes.c_ulong * 32)()
        count = kernel.GetConsoleProcessList(pids, len(pids))
        row["console_pids"] = list(pids[:min(count, len(pids))])
    return row


def alive(row):
    try:
        return psutil.Process(row["pid"]).create_time() == row["create_time"]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def wait_for(predicate, timeout=90, *, label="condition", case=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if case and (case / "parent.error.json").is_file():
            raise RuntimeError("console parent failed: " + str(read(case / "parent.error.json")))
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError(label + " did not complete")


def http_json(url):
    # Ignore system proxies: this integration only talks to its own loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:
        return json.load(response)


def install_event_recorder(path):
    """Observe native control events; return FALSE so default termination runs."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)
    pid = os.getpid()
    # A supervising runner may ignore Ctrl+C. Only our private test console
    # should behave like an ordinary interactive shell, so clear that inherited
    # ignore attribute before creating/observing its negative control.
    if not kernel.SetConsoleCtrlHandler(None, False):
        raise ctypes.WinError(ctypes.get_last_error())

    @handler_type
    def record(event):
        with open(path, "ab", buffering=0) as stream:
            stream.write((json.dumps({"event": event, "pid": pid,
                                      "time_ns": time.time_ns()}) + "\n").encode())
        return 0

    if not kernel.SetConsoleCtrlHandler(record, True):
        raise ctypes.WinError(ctypes.get_last_error())
    # Keep callback alive for the lifetime of this process.
    return record


def close_handle(handle):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    if not kernel.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def kill_on_close_job(pids):
    """Own a job containing only these new, parked fixture processes."""
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", ctypes.c_ulong), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_limit", ctypes.c_ulong),
                    ("affinity", ctypes.c_size_t), ("priority", ctypes.c_ulong),
                    ("scheduling", ctypes.c_ulong)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        for pid in set(pids):
            process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                close_handle(process)
        return handle
    except BaseException:
        close_handle(handle)
        raise


def in_job(pid, job):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    process = kernel.OpenProcess(0x0400, False, pid)  # QUERY_INFORMATION
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = ctypes.c_int()
        if not kernel.IsProcessInJob(process, job, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(result.value)
    finally:
        close_handle(process)


def mock_server(case):
    # Import the same deterministic fixture translator as ordinary integrations.
    sys.path.insert(0, str(ROOT / "tests"))
    from run_integrations import mock_translate_request
    who = identity()

    class MockMT(BaseHTTPRequestHandler):
        def reply(self, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply({"data": [{"id": "mock"}], "identity": who,
                        "path": self.path, "time_ns": time.time_ns()})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not (case / "request-received.json").exists():
                write_json(case / "request-received.json", {"time_ns": time.time_ns(),
                                                           "identity": who})
            wait_for(lambda: (case / "release-request").is_file(), timeout=180,
                     label="test controller to release held mock request")
            self.reply({"model": "mock", "choices": [{"message": {
                "content": mock_translate_request(payload["messages"][-1]["content"])},
                "finish_reason": "stop"}]})

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockMT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    write_json(case / "server.ready.json", {**who, "host": f"http://127.0.0.1:{server.server_port}"})
    try:
        wait_for(lambda: (case / "server.stop").is_file(), timeout=600,
                 label="mock server stop marker")
    finally:
        server.shutdown()
        server.server_close()
        write_json(case / "server.stopped.json", {**who, "time_ns": time.time_ns()})


def negative_control(case):
    callback = install_event_recorder(case / "control.events.jsonl")
    write_json(case / "control.ready.json", identity())
    while callback:
        time.sleep(0.1)


def console_parent(case):
    from pipeline import initialize
    from background_run import latest_run
    from windows_tasks import register_task, run_task
    callback = install_event_recorder(case / "parent.events.jsonl")
    try:
        python = venv_python()
        pythonw = Path(python).with_name("pythonw.exe")
        if not pythonw.is_file():
            raise RuntimeError("This Windows integration requires pythonw.exe beside python.exe")
        write_json(case / "parent.identity.json", identity())
        # The controller may first assign this parked helper and its venv
        # launcher to a private Job Object; no descendants exist yet.
        wait_for(lambda: (case / "begin-parent").is_file(), label="test controller start")
        # No creation flags here: the child really shares this private console.
        with (case / "control.log").open("wb") as log:
            subprocess.Popen([python, "-X", "utf8", str(SELF),
                              "--role", "control", "--out", str(case)],
                             cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        wait_for(lambda: (case / "control.ready.json").is_file(), label="negative control")
        name = read(case / "case.json")["server_task"]
        register_task(name, [str(pythonw), "-X", "utf8", str(SELF),
                             "--role", "server", "--out", str(case)], str(ROOT))
        write_json(case / "server.task.registered.json", {"name": name})
        run_task(name)
        wait_for(lambda: (case / "server.ready.json").is_file(), label="scheduled mock HTTP")
        host = read(case / "server.ready.json")["host"]
        initialize(case / "source.md", case / "job", {
            "host": host, "model": "mock", "group_chars": 900,
            "split_chars": 900, "timeout": 300})
        with (case / "launcher.log").open("wb") as log:
            result = subprocess.run([python, "-X", "utf8", str(ROOT / "scripts/pipeline.py"),
                                     "start", str(case / "job")], cwd=ROOT,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=90)
        if result.returncode:
            raise RuntimeError(f"pipeline start returned {result.returncode}; see launcher.log")
        wait_for(lambda: (case / "request-received.json").is_file(), label="actual pipeline MT request")
        state = read(case / "job/pipeline.status.json")
        background = latest_run(case / "job")
        supervisor = read(background / "state.json")
        if state["status"] != "running":
            raise RuntimeError("pipeline was not active at fault injection")
        write_json(case / "parent.ready.json", {"parent": identity(),
            "control": read(case / "control.ready.json"),
            "server": read(case / "server.ready.json"),
            "supervisor": supervisor["worker"], "background_run": str(background),
            "pipeline": identity(state["pid"]),
            "child": identity(state["child_pid"]), "time_ns": time.time_ns()})
        while callback:
            if (case / "send-ctrl-c").is_file():
                # This helper owns the new console. Zero broadcasts exclusively
                # to that console, never to the Codex/PowerShell caller.
                if not ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
                time.sleep(10)
                raise RuntimeError("CTRL_C_EVENT failed to terminate private console")
            time.sleep(0.1)
    except Exception:
        write_json(case / "parent.error.json", {"traceback": traceback.format_exc()})
        raise


def event_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def cleanup_case(case):
    """Cooperative shutdown; only exact synthetic identities may be terminated."""
    from windows_tasks import delete_task, task_status
    (case / "release-request").touch(exist_ok=True)
    (case / "server.stop").touch(exist_ok=True)
    if (case / "job/background/latest.json").is_file():
        with (case / "cleanup.log").open("ab") as log:
            subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts/pipeline.py"),
                            "stop", str(case / "job")], stdout=log, stderr=subprocess.STDOUT,
                           cwd=ROOT, timeout=30)
    if (case / "server.ready.json").is_file():
        wait_for(lambda: not alive(read(case / "server.ready.json")), timeout=15,
                 label="scheduled mock server shutdown")
    if (case / "server.task.registered.json").is_file():
        name = read(case / "case.json")["server_task"]
        wait_for(lambda: task_status(name).get("state_name") not in ("running", "queued"),
                 timeout=15, label="mock server task action to exit")
        delete_task(name)
        if task_status(name).get("exists"):
            raise RuntimeError("mock server task remained registered after cleanup")
    for filename in ("control.ready.json", "parent.identity.json"):
        if (case / filename).is_file():
            row = read(case / filename)
            if alive(row):
                process = psutil.Process(row["pid"])
                # Exact identity and this isolated path must both match.
                if str(case).casefold() not in " ".join(process.cmdline()).casefold():
                    raise RuntimeError("Refusing cleanup of a process outside this fixture")
                process.terminate()
                process.wait(timeout=10)


def run_case(out, event):
    case = out / event
    case.mkdir()
    write_json(case / "case.json", {"event": event, "server_task": "FreeTranslate-" + str(uuid.uuid4())})
    (case / "source.md").write_text(
        "# Mechanics\n\nThe equation $F=ma$ defines the force.\n\n"
        "The energy is $$E=mc^2.$$\n", encoding="utf-8")
    source_hash = sha_file(case / "source.md")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    row = {"status": "failed", "event": event}
    job_handle = None
    try:
        # The venv launcher can own its own child Job Object. A single native
        # interpreter is needed for an unambiguous private parent Job; only
        # this fixture process receives an explicit venv site-packages path.
        parent_python = sys._base_executable if event == "job-close" else sys.executable
        environment = None
        if event == "job-close":
            environment = dict(os.environ, PYTHONPATH=str(Path(psutil.__file__).resolve().parent.parent))
        with (case / "parent.log").open("wb") as log:
            parent = subprocess.Popen([parent_python, "-X", "utf8", str(SELF),
                                       "--role", "parent", "--out", str(case)],
                                      cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NEW_CONSOLE,
                                      startupinfo=startup, env=environment)
        wait_for(lambda: (case / "parent.identity.json").is_file(), label="private console identity", case=case)
        if event == "job-close":
            job_handle = kill_on_close_job([parent.pid, read(case / "parent.identity.json")["pid"]])
        (case / "begin-parent").touch()
        wait_for(lambda: (case / "parent.ready.json").is_file(), timeout=120,
                 label="parent to start scheduled work", case=case)
        before = read(case / "parent.ready.json")
        # Windows venv launchers can create a base-Python process. The recorded
        # native console owner must be that exact launcher or its descendant.
        assert before["parent"]["pid"] == parent.pid or parent.pid in {
            p.pid for p in psutil.Process(before["parent"]["pid"]).parents()}
        assert before["parent"]["console_hwnd"]
        assert before["control"]["console_hwnd"] == before["parent"]["console_hwnd"]
        assert before["control"]["pid"] in before["parent"]["console_pids"]
        assert before["server"]["console_hwnd"] != before["parent"]["console_hwnd"]
        assert all(before[key]["pid"] not in before["parent"]["console_pids"]
                   for key in ("server", "supervisor", "pipeline", "child"))
        owner = psutil.Process().username().casefold()
        assert all(before[key]["username"].casefold() == owner
                   for key in ("parent", "control", "server", "supervisor", "pipeline", "child"))
        assert all(alive(before[key]) for key in ("parent", "control", "server", "supervisor", "pipeline", "child"))
        if job_handle:
            membership = {key: in_job(before[key]["pid"], job_handle)
                          for key in ("parent", "control", "server", "supervisor", "pipeline", "child")}
            assert membership == {"parent": True, "control": True, "server": False,
                                  "supervisor": False, "pipeline": False, "child": False}
            write_json(case / "job.membership.json", membership)
        injected = time.time_ns()
        if event == "ctrl-c":
            (case / "send-ctrl-c").touch()
            event_number = 0
        elif event == "console-close":
            event_number = 2
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            if not user32.PostMessageW(before["parent"]["console_hwnd"], 0x0010, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            event_number = None
            close_handle(job_handle)
            job_handle = None
        wait_for(lambda: not alive(before["parent"]) and not alive(before["control"]),
                 timeout=20, label="both console processes to die")
        parent.wait(timeout=5)
        if event_number is not None:
            assert any(e["event"] == event_number for e in event_records(case / "parent.events.jsonl"))
            assert any(e["event"] == event_number for e in event_records(case / "control.events.jsonl"))
        assert all(alive(before[key]) for key in ("server", "supervisor", "pipeline", "child"))
        nonce = uuid.uuid4().hex
        probe = http_json(before["server"]["host"] + "/health?probe=" + nonce)
        assert nonce in probe["path"] and probe["time_ns"] > injected
        assert probe["identity"]["pid"] == before["server"]["pid"]
        write_json(case / "survived.json", {"before": before, "injected_time_ns": injected,
            "server_probe": probe, "parent_returncode": parent.returncode,
            "parent_events": event_records(case / "parent.events.jsonl"),
            "control_events": event_records(case / "control.events.jsonl")})
        (case / "release-request").touch()

        def completed():
            state = read(case / "job/pipeline.status.json")
            if state["status"] in ("needs_attention", "interrupted", "stale"):
                raise RuntimeError("isolated pipeline failed: " + str(state))
            return state if state["status"] == "done" else None

        final = wait_for(completed, timeout=300, label="real PDF release")
        def background_completed():
            result = read(Path(before["background_run"]) / "state.json")
            if result["status"] in ("failed", "stopped", "needs_attention"):
                raise RuntimeError("background supervisor failed: " + str(result))
            return result if result["status"] == "done" and result.get("task_removed") else None

        background_final = wait_for(background_completed, timeout=30,
                                    label="supervisor completion and one-shot task cleanup")
        pdfs = list((case / "job").rglob("*.ru.pdf"))
        assert len(pdfs) == 1, pdfs
        pdf = pdfs[0]
        receipt = read(str(pdf) + ".release.json")
        assert receipt["profile"] == "release" and receipt["status"] == "passed"
        assert sha_file(case / "source.md") == source_hash
        row.update(status="passed", identities=before, parent_returncode=parent.returncode,
                   source_sha256=source_hash, pdf=str(pdf.relative_to(out)), pdf_sha256=sha_file(pdf),
                   pipeline_status=final["status"], receipt_status=receipt["status"],
                   background_status=background_final["status"], task_removed=background_final["task_removed"],
                   parent_events=event_records(case / "parent.events.jsonl"),
                   control_events=event_records(case / "control.events.jsonl"))
    except Exception as exc:
        row.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        try:
            if job_handle:
                close_handle(job_handle)
            cleanup_case(case)
            row["cleanup"] = "passed"
        except Exception as exc:
            row.update(status="failed", cleanup="failed", cleanup_error=str(exc))
        write_json(case / "result.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--events", nargs="+", choices=("ctrl-c", "console-close", "job-close"),
                        default=["ctrl-c", "console-close", "job-close"])
    parser.add_argument("--role", choices=("parent", "control", "server"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This opt-in integration requires Windows")
    out = args.out.resolve()
    if args.role:
        try:
            return {"parent": console_parent, "control": negative_control, "server": mock_server}[args.role](out)
        except Exception:
            write_json(out / (args.role + ".error.json"), {"traceback": traceback.format_exc()})
            raise
    if out.exists():
        parser.error("Use a NEW output directory; existing evidence is never overwritten")
    out.mkdir(parents=True)
    missing = [name for name in ("pandoc", "xelatex") if not shutil.which(name)]
    if missing:
        write_json(out / "summary.json", {"status": "not_run", "missing": missing})
        return 2
    if len(set(args.events)) != len(args.events):
        parser.error("Each fault event may be requested only once per output directory")
    results = [run_case(out, event) for event in args.events]
    summary = {"status": "passed" if all(r["status"] == "passed" for r in results) else "failed",
               "tests": results, "MT": "mock HTTP; no model/GPU", "books": "new synthetic sources only",
               "limits": "Console events and private Job Object only; logoff/reboot and real LM Studio inference not tested"}
    write_json(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
