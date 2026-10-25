"""A dedicated, current-user LM Studio service for a detached pipeline worker.

Only the dedicated profile is writable. Existing desktop services, model files,
and model caches are neither adopted nor stopped. No model is loaded here.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

import psutil


class RuntimeError_(RuntimeError):
    pass


MARKER = "freetranslate-profile.json"
RECEIPT = "freetranslate-runtime.json"
BACKEND_INVENTORY = "freetranslate-backend-inventory.json"


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _backend_source(backend_dir):
    """Validate one explicitly selected Windows GGUF engine and its vendors."""
    source = Path(backend_dir)
    if _linked(source):
        raise RuntimeError_("linked backend directory is not accepted")
    source = source.resolve()
    try:
        manifest = json.loads((source / "backend-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError_("backend directory must contain backend-manifest.json") from exc
    if (not isinstance(manifest, dict) or manifest.get("manifest_version") != "4"
            or manifest.get("extension_type") != "engine" or manifest.get("engine") != "llama.cpp"
            or manifest.get("platform") != "win" or not isinstance(manifest.get("cpu"), dict)
            or manifest["cpu"].get("architecture") != "x86_64"
            or not isinstance(manifest.get("supported_model_formats"), list) or "gguf" not in manifest["supported_model_formats"]
            or not isinstance(manifest.get("domains"), list) or "llm" not in manifest["domains"]):
        raise RuntimeError_("backend manifest is incompatible with this Windows GGUF launcher")
    name, version = manifest.get("name"), manifest.get("version")
    safe = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
    if (not isinstance(name, str) or not safe.fullmatch(name) or not isinstance(version, str)
            or not safe.fullmatch(version) or source.name != name + "-" + version):
        raise RuntimeError_("backend directory name must match manifest name-version")
    protocol = manifest.get("engine_protocol_server", {})
    if not isinstance(protocol, dict):
        raise RuntimeError_("backend engine protocol declaration is invalid")
    executable = protocol.get("executable_relative_path")
    libraries = manifest.get("target_libraries")
    if (protocol.get("runtime_kind") != "llama-server" or not isinstance(executable, str)
            or not isinstance(libraries, list) or not libraries):
        raise RuntimeError_("backend manifest must identify llama-server and target libraries")
    required = [executable]
    for library in libraries:
        if not isinstance(library, dict) or not isinstance(library.get("name"), str):
            raise RuntimeError_("backend target library declaration is invalid")
        required.append(library["name"])
    for item in required:
        path = source / item
        if not path.resolve().is_relative_to(source) or not path.is_file():
            raise RuntimeError_("backend executable or target library is missing/outside its directory")
    vendors = manifest.get("vendor_lib_package_names", [])
    if (not isinstance(vendors, list) or any(not isinstance(x, str) or not safe.fullmatch(x) for x in vendors)
            or len(set(vendors)) != len(vendors)):
        raise RuntimeError_("backend vendor dependency list is invalid")
    trees = [(source, Path(source.name))]
    for vendor in vendors:
        path = source.parent / "vendor" / vendor
        if not path.is_dir() or _linked(path) or _linked(path.parent):
            raise RuntimeError_("backend vendor dependency is missing or linked: " + vendor)
        trees.append((path, Path("vendor") / vendor))
    entries = []
    for tree, target in trees:
        for path in sorted(tree.rglob("*")):
            if _linked(path) or not path.resolve().is_relative_to(tree.resolve()):
                raise RuntimeError_("backend/vendor trees may not contain links outside the copied snapshot")
            if path.is_file():
                entries.append({"path": (target / path.relative_to(tree)).as_posix(),
                                "sha256": _sha_file(path), "size": path.stat().st_size})
    return {"source": str(source), "engine": name, "version": version, "entries": entries,
            "trees": [relative.as_posix() for _, relative in trees]}, trees


def _check_backend_files(profile, inventory):
    destination = profile / ".lmstudio" / "extensions" / "backends"
    for path in (profile, profile / ".lmstudio", profile / ".lmstudio" / "extensions", destination):
        if _linked(path):
            raise RuntimeError_("owned backend snapshot contains a linked directory")
    expected = {row["path"]: row for row in inventory["entries"]}
    actual = set()
    for relative in inventory["trees"]:
        tree = destination / relative
        if (not tree.resolve().is_relative_to(destination.resolve()) or not tree.is_dir()
                or any(_linked(p) for p in (tree, *tree.parents) if p.is_relative_to(destination))):
            raise RuntimeError_("owned backend snapshot contains a missing/linked tree")
        for path in tree.rglob("*"):
            if _linked(path) or not path.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError_("owned backend snapshot contains a linked directory/file")
            if path.is_file():
                key = path.relative_to(destination).as_posix()
                actual.add(key)
                if key not in expected or _sha_file(path) != expected[key]["sha256"]:
                    raise RuntimeError_("owned backend snapshot was changed; use a new managed profile")
    if actual != set(expected):
        raise RuntimeError_("owned backend snapshot was changed; use a new managed profile")


def _copy_backend(profile, inventory, trees):
    destination = profile / ".lmstudio" / "extensions" / "backends"
    for source, relative in trees:
        shutil.copytree(source, destination / relative, copy_function=shutil.copy2)
    _check_backend_files(profile, inventory)
    _write(profile / BACKEND_INVENTORY, inventory)
    return _sha_file(profile / BACKEND_INVENTORY)


def _verify_backend_snapshot(profile, marker):
    expected = marker.get("backend_inventory_sha256")
    if not expected:
        raise RuntimeError_("owned profile has no backend snapshot identity")
    inventory_path = profile / BACKEND_INVENTORY
    if _sha_file(inventory_path) != expected:
        raise RuntimeError_("owned backend inventory was changed")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    _check_backend_files(profile, inventory)


def endpoint(host):
    p = urlsplit(host)
    if (p.scheme != "http" or p.hostname not in ("localhost", "127.0.0.1")
            or p.username or p.password or p.query or p.fragment
            or p.path.rstrip("/") not in ("", "/v1")):
        raise RuntimeError_("managed LM Studio requires http://127.0.0.1:PORT[/v1]")
    port = p.port or 80
    return f"http://127.0.0.1:{port}", port


def profile_environment(profile_dir):
    """Node os.homedir() uses USERPROFILE on Windows; do not alter the parent."""
    profile = Path(profile_dir).resolve()
    return dict(os.environ, USERPROFILE=str(profile),
                APPDATA=str(profile / "AppData" / "Roaming"),
                LOCALAPPDATA=str(profile / "AppData" / "Local"))


def _write(path, value):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("x", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write("\n")
    os.replace(tmp, path)


def find_app(lms_path, app_path=None):
    if app_path:
        app = Path(app_path).resolve()
    else:
        install = Path(lms_path).resolve().parent.parent / ".internal" / "app-install-location.json"
        try:
            app = Path(json.loads(install.read_text(encoding="utf-8"))["path"]).resolve()
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError_("cannot locate installed LM Studio; specify --lmstudio-app") from exc
    if not app.is_file() or app.name.lower() not in ("lm studio.exe", "llmster.exe"):
        raise RuntimeError_("LM Studio executable is missing; use LM Studio.exe or llmster.exe")
    return app


def prepare_profile(profile_dir, app, models_dir=None, backend_dir=None):
    profile = Path(profile_dir).resolve()
    current = Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve()
    if profile == current:
        raise RuntimeError_("refusing to use the current user's ordinary profile")
    models = str(Path(models_dir).resolve()) if models_dir else None
    backend = str(Path(backend_dir).resolve()) if backend_dir else None
    if models and not Path(models).is_dir():
        raise RuntimeError_("explicit LM Studio model directory does not exist")
    marker_path = profile / MARKER
    if profile.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError_("existing LM Studio profile is not owned by this launcher; use a new directory") from exc
        if (marker.get("schema") != 1 or marker.get("profile") != str(profile)
                or marker.get("app") != str(app) or marker.get("models_dir") != models
                or marker.get("backend_source") != backend):
            raise RuntimeError_("managed profile settings differ; use a new profile directory")
        if backend:
            _verify_backend_snapshot(profile, marker)
        return profile, marker
    backend_snapshot = _backend_source(backend_dir) if backend_dir else None
    if backend_snapshot and any(profile.is_relative_to(tree.resolve()) for tree, _ in backend_snapshot[1]):
        raise RuntimeError_("managed profile may not be inside a backend or vendor source directory")
    profile.mkdir(parents=True, exist_ok=False)
    lmhome = profile / ".lmstudio"
    for path in (lmhome / ".internal", profile / "AppData" / "Local", profile / "AppData" / "Roaming"):
        path.mkdir(parents=True, exist_ok=True)
    (profile / ".lmstudio-home-pointer").write_text(str(lmhome), encoding="utf-8")
    if models:
        _write(lmhome / "settings.json", {"downloadsFolder": models})
    marker = {"schema": 1, "profile": str(profile), "app": str(app),
              "models_dir": models, "token": uuid.uuid4().hex, "backend_source": backend}
    if backend_snapshot:
        marker["backend_inventory_sha256"] = _copy_backend(profile, *backend_snapshot)
    _write(marker_path, marker)
    return profile, marker


@contextmanager
def _profile_lock(profile):
    """OS lease serializes starts/stops; a crashed worker releases it automatically."""
    import msvcrt
    with (profile / "freetranslate-lifecycle.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError_("managed LM Studio profile is being started/stopped by another worker") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _profile_processes(profile, app):
    expected = _instance_argument(profile, app)
    active = []
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if (process.info.get("exe") and Path(process.info["exe"]).resolve() == app
                    and expected in (process.info.get("cmdline") or [])):
                active.append(process.info["pid"])
        except (OSError, psutil.Error):
            continue
    return active


def _other_core_processes(profile):
    """Desktop 0.4.19 opens a modal when any other core is already running."""
    own_argument = "--user-data-dir=" + str(profile / "AppData" / "Roaming" / "LM Studio")
    rows = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        args = process.info.get("cmdline") or []
        if ((process.info.get("name") or "").lower() in ("lm studio.exe", "llmster.exe")
                and not any(arg.startswith("--type=") for arg in args)
                and own_argument not in args):
            rows.append(process.info["pid"])
    return rows


def process_identity(pid):
    p = psutil.Process(pid)
    return {"pid": p.pid, "create_time": p.create_time(), "username": p.username(),
            "exe": str(Path(p.exe()).resolve()), "cmdline": p.cmdline()}


def _same_process(saved):
    try:
        actual = process_identity(saved["pid"])
        return all(actual[k] == saved[k] for k in ("pid", "create_time", "username", "exe"))
    except (psutil.Error, KeyError, OSError):
        return False


def _verify_identity(identity, app, profile):
    expected_arg = _instance_argument(profile, app)
    if (identity["username"].casefold() != psutil.Process().username().casefold()
            or Path(identity["exe"]).resolve() != app
            or (app.name.lower() == "lm studio.exe" and "--run-as-service" not in identity["cmdline"])
            or expected_arg not in identity["cmdline"]):
        raise RuntimeError_("LM Studio process identity/profile/current-user check failed")


def _instance_argument(profile, app):
    if app.name.lower() == "llmster.exe":
        marker = json.loads((profile / MARKER).read_text(encoding="utf-8"))
        return "--start-timestamp=" + marker["token"]
    return "--user-data-dir=" + str(profile / "AppData" / "Roaming" / "LM Studio")


def _server_identity(identity, listeners):
    if listeners == {identity["pid"]}:
        return identity
    if len(listeners) == 1 and None not in listeners:
        try:
            process = psutil.Process(next(iter(listeners)))
            if any(parent.pid == identity["pid"] and parent.create_time() == identity["create_time"]
                   for parent in process.parents()):
                actual = process_identity(process.pid)
                if actual["username"] == identity["username"]:
                    return actual
        except psutil.Error:
            pass
    raise RuntimeError_("LM Studio listener ownership does not match the launched process")


def _listener_pid(port):
    listeners = {c.pid for c in psutil.net_connections(kind="tcp")
                 if c.status == psutil.CONN_LISTEN and c.laddr.port == port
                 and c.laddr.ip in ("127.0.0.1", "0.0.0.0", "::", "::1")}
    return listeners


def _http_json(base, path, body=None):
    # The newly-created isolated profile uses no API key. No global proxy or redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError_("managed LM Studio health endpoint redirected")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
    with opener.open(request, timeout=30) as response:
        return json.load(response)


def _probe(base):
    data = _http_json(base, "/v1/models")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RuntimeError_("LM Studio health response has no model list")
    return len(data["data"])


def _stop_new_process(proc):
    """Clean an unpublished startup through its Popen handle, never an old receipt."""
    children = []
    if proc.poll() is None:
        try:
            children = psutil.Process(proc.pid).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        proc.terminate()
    for child in children:
        try:
            child.terminate()  # psutil checks creation time against PID reuse.
        except psutil.NoSuchProcess:
            pass
    proc.wait(timeout=15)
    _, pending = psutil.wait_procs(children, timeout=15)
    if pending:
        raise RuntimeError_("new unpublished LM Studio descendants did not stop")


def _ensure_server(host, lms_path, logdir, *, profile_dir, app_path=None,
                   models_dir=None, backend_dir=None, timeout=120, _owned):
    """Call inside a detached worker; never reconfigure an ordinary LM Studio.

    The caller owns lifecycle and must call stop_server(receipt). A failed start
    leaves a durable ownership receipt for safe, explicit cleanup/retry.
    """
    if os.name != "nt":
        raise RuntimeError_("managed LM Studio startup is supported on Windows only")
    base, port = endpoint(host)
    lms = Path(lms_path or shutil.which("lms") or "").resolve()
    if not lms.is_file():
        raise RuntimeError_("lms CLI not found; specify --lms-exe")
    app = find_app(lms, app_path)
    profile, marker = prepare_profile(profile_dir, app, models_dir, backend_dir)
    logs = Path(logdir).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    env = profile_environment(profile)
    receipt_path = profile / RECEIPT
    previous = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else None
    if _profile_processes(profile, app):
        raise RuntimeError_("isolated LM Studio runtime is already live; refusing adoption from another worker")
    else:
        if _other_core_processes(profile):
            raise RuntimeError_("LM Studio/llmster cannot start a second independent core while another runtime is live; existing runtime is untouched")
        if previous and _same_process(previous.get("process", {})):
            raise RuntimeError_("owned LM Studio is alive but its control API is unavailable; no duplicate startup")
        if _listener_pid(port):
            raise RuntimeError_("requested LM Studio port is already occupied; existing server is untouched")
        with socket.socket() as guard:
            guard.bind(("127.0.0.1", port))
        # lms CLI versions may discover another running profile through global
        # well-known ports. Never use that CLI to manage an isolated runtime.
        # This file schema is verified by the real installed-app integration.
        _write(profile / ".lmstudio" / ".internal" / "http-server-config.json", {
            "autoStartOnLaunch": True, "port": port, "networkInterface": "127.0.0.1",
            "cors": False, "justInTimeModelLoading": True, "logSensitiveData": False,
            "logIncomingTokens": False, "verbose": False, "fileLoggingMode": "succinct"})
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        command = [str(app), _instance_argument(profile, app)]
        if app.name.lower() == "lm studio.exe":
            command.insert(1, "--run-as-service")
        with (logs / "lmstudio-app.log").open("ab") as log:
            proc = subprocess.Popen(command, cwd=str(app.parent), env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    close_fds=True, creationflags=flags, startupinfo=startup)
        try:
            identity = process_identity(proc.pid)
            previous = {"schema": 1, "token": marker["token"], "profile": str(profile),
                        "process": identity, "host": base, "state": "starting",
                        "lms": str(lms), "logdir": str(logs)}
            _write(receipt_path, previous)
        except Exception:
            # No durable receipt exists yet; only this local Popen handle is owned.
            _stop_new_process(proc)
            raise
        _owned.append(previous)
        _verify_identity(identity, app, profile)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            listeners = _listener_pid(port)
            if listeners:
                server_identity = _server_identity(identity, listeners)
                try:
                    count = _probe(base)
                    break
                except (OSError, ValueError):
                    pass
            if proc.poll() is not None:
                raise RuntimeError_(f"isolated LM Studio exited ({proc.returncode}); see lmstudio-app.log")
            time.sleep(0.5)
        else:
            raise RuntimeError_("isolated LM Studio did not become ready before timeout")
    receipt = dict(previous, state="ready", host=base, process=identity,
                   server_process=server_identity, models_visible=count, ready_at=time.time())
    _write(receipt_path, receipt)
    return receipt


def ensure_server(host, lms_path, logdir, *, profile_dir, app_path=None,
                  models_dir=None, backend_dir=None, timeout=120):
    """Start a dedicated service INSIDE the independent scheduled worker.

    A live runtime is never adopted, even with a previous ownership receipt.
    On startup failure only this call's newly-created process is cleaned up.
    """
    if os.name != "nt":
        raise RuntimeError_("managed LM Studio startup is supported on Windows only")
    endpoint(host)
    lms = Path(lms_path or shutil.which("lms") or "").resolve()
    if not lms.is_file():
        raise RuntimeError_("lms CLI not found; specify --lms-exe")
    app = find_app(lms, app_path)
    profile, _ = prepare_profile(profile_dir, app, models_dir, backend_dir)
    with _profile_lock(profile):
        owned = []
        try:
            return _ensure_server(host, lms, logdir, profile_dir=profile,
                                  app_path=app, models_dir=models_dir, backend_dir=backend_dir, timeout=timeout,
                                  _owned=owned)
        except Exception as original:
            if owned:
                try:
                    _stop_server(owned[0])
                except Exception as cleanup:
                    raise RuntimeError_(f"LM Studio startup failed: {original}; owned-runtime cleanup failed: {cleanup}") from original
            raise


def _stop_server(receipt):
    """Stop only the exact owned profile/process; never issue global lms commands."""
    profile = Path(receipt["profile"]).resolve()
    marker = json.loads((profile / MARKER).read_text(encoding="utf-8"))
    saved = json.loads((profile / RECEIPT).read_text(encoding="utf-8"))
    if (marker.get("profile") != str(profile) or marker.get("token") != receipt.get("token")
            or saved.get("process") != receipt.get("process")):
        raise RuntimeError_("runtime ownership receipt mismatch; refusing to stop")
    identity = receipt["process"]
    if not _same_process(identity):
        # An exited PID can be reused; never terminate the replacement.
        return {"stopped": False, "reason": "owned process no longer exists"}
    _verify_identity(identity, Path(marker["app"]).resolve(), profile)
    root = psutil.Process(identity["pid"])
    children = [(p, p.create_time()) for p in root.children(recursive=True)]
    root.terminate()
    for child, created in children:
        try:
            if child.create_time() == created and child.username() == identity["username"]:
                child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs([root, *(p for p, _ in children)], timeout=15)
    if alive:
        raise RuntimeError_("owned LM Studio processes did not stop; no force-kill was attempted")
    _write(profile / RECEIPT, dict(saved, state="stopped", stopped_at=time.time()))
    return {"stopped": True, "pid": identity["pid"]}


def stop_server(receipt):
    with _profile_lock(Path(receipt["profile"]).resolve()):
        return _stop_server(receipt)


def _assert_live_receipt(receipt):
    profile = Path(receipt["profile"]).resolve()
    marker = json.loads((profile / MARKER).read_text(encoding="utf-8"))
    saved = json.loads((profile / RECEIPT).read_text(encoding="utf-8"))
    if (marker.get("token") != receipt.get("token") or marker.get("profile") != str(profile)
            or saved.get("process") != receipt.get("process") or saved.get("host") != receipt.get("host")
            or saved.get("state") != "ready" or not _same_process(receipt["process"])):
        raise RuntimeError_("managed LM Studio receipt is not the live owned runtime")
    _verify_identity(receipt["process"], Path(marker["app"]).resolve(), profile)
    base, port = endpoint(receipt["host"])
    server = receipt.get("server_process", receipt["process"])
    if not _same_process(server) or _listener_pid(port) != {server["pid"]}:
        raise RuntimeError_("managed LM Studio endpoint no longer belongs to the owned process")
    return base


def _loaded_ids(data):
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise RuntimeError_("native LM Studio model list is unavailable; refusing global CLI fallback")
    ids = []
    for model in data["models"]:
        if not isinstance(model, dict) or not isinstance(model.get("loaded_instances"), list):
            raise RuntimeError_("native model list has no reliable loaded_instances field")
        for instance in model["loaded_instances"]:
            if not isinstance(instance, dict) or not isinstance(instance.get("id"), str) or not instance["id"]:
                raise RuntimeError_("native model list has invalid loaded instance identity")
            if instance["id"] in ids:
                raise RuntimeError_("native model list contains duplicate instance identities")
            ids.append(instance["id"])
    return ids


def unload_owned_models(receipt):
    """GPU handoff through the exact owned endpoint; no lms global discovery."""
    with _profile_lock(Path(receipt["profile"]).resolve()):
        base = _assert_live_receipt(receipt)
        ids = _loaded_ids(_http_json(base, "/api/v1/models"))
        for instance in ids:
            _assert_live_receipt(receipt)
            reply = _http_json(base, "/api/v1/models/unload", {"instance_id": instance})
            if not isinstance(reply, dict) or reply.get("instance_id") != instance:
                raise RuntimeError_("native model unload did not confirm the requested instance")
        _assert_live_receipt(receipt)
        remaining = _loaded_ids(_http_json(base, "/api/v1/models"))
        if remaining:
            raise RuntimeError_("managed LM Studio still has loaded models after GPU handoff")
        return {"unloaded": ids, "loaded_remaining": [], "host": base}
