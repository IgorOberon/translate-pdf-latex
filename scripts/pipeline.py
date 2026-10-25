"""Low-context dispatcher: init INPUT --out NEW_DIR; run JOB; status JOB.

No LLM controller needed. Fixed commands, local MT by default, no shell strings,
no automatic polishing or provider fallback. Work is isolated from the source.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from endpoint_policy import validate_endpoint
from paths import ROOT, venv_python
from provenance import inside, sha_file, canonical_hash, write_json, load_manifest
from procutils import PipelineLock, _kill_tree

JOB = "pipeline.job.json"
STATE = "pipeline.status.json"
BOOK_EXT = {".pdf", ".epub", ".fb2", ".docx", ".odt", ".html", ".htm", ".rst", ".txt", ".md", ".djvu", ".mobi", ".azw"}
DEFAULTS = {"host": "http://localhost:1234", "model": "hy-mt2-7b", "mode": "hymt",
            "allow_remote": False, "polish": False, "parallel": 1,
            "extract": "auto", "source_lang": "English", "target_lang": "Russian",
            "split_chars": 900, "group_chars": 900, "layout": "compact", "context_per_request": 0, "parts": 100,
            "timeout": 86400, "mineru_timeout": 3600, "title": "", "author": "", "keep_source": "",
            "glossary_profile": "", "book_profile": ""}
SKIP = {".git", "venv", ".venv", "__pycache__", ".tcache", "_skeleton_work", ".build-candidates", "build", "preview", ".sync-backups"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class StateFileError(ValueError):
    """Corrupt state is diagnostic evidence, never an instruction to reset it."""

    def __init__(self, path, raw, error):
        self.details = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw), "error_type": type(error).__name__}
        if isinstance(error, json.JSONDecodeError):
            self.details.update(line=error.lineno, column=error.colno)
        super().__init__(f"invalid pipeline state: {path}; bytes preserved; {error}")


def _state_digest(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in value)):
        raise ValueError(label + " must be a SHA-256 hex string")


def _state_hashes(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be a path-to-hash object")
    for path, digest in value.items():
        if not isinstance(path, str) or not path:
            raise ValueError(label + " must contain nonempty path strings")
        _state_digest(digest, label + "." + path)


def read_state(root):
    path = Path(root) / STATE
    raw = path.read_bytes()
    try:
        state = json.loads(raw.decode("utf-8"))
        if (not isinstance(state, dict) or type(state.get("schema")) is not int
                or state.get("schema") != 1):
            raise ValueError("expected a schema=1 JSON object")
        if state.get("status") not in {"ready", "running", "done", "needs_attention", "interrupted", "stale"}:
            raise ValueError("unknown or missing pipeline status")
        # Validate what resume/status consume before acquiring a lock or writing
        # anything. Optional schema-1 fields and unknown metadata remain valid.
        if "steps" in state:
            if not isinstance(state["steps"], dict):
                raise ValueError("steps must be an object")
            for name, step in state["steps"].items():
                if not isinstance(name, str) or not name or not isinstance(step, dict):
                    raise ValueError("steps must map nonempty names to objects")
                if "status" in step and not isinstance(step["status"], str):
                    raise ValueError("step status must be a string: " + name)
                if "outputs" in step:
                    _state_hashes(step["outputs"], "steps." + name + ".outputs")
        if "artifacts" in state:
            _state_hashes(state["artifacts"], "artifacts")
        for key in ("job_sha256", "code_sha256"):
            if key in state:
                _state_digest(state[key], key)
        for key in ("pid", "child_pid"):
            value = state.get(key)
            if value is not None and (type(value) is not int or not 0 < value <= 0xFFFFFFFF):
                raise ValueError(key + " must be a positive process ID or null")
        for key in ("started", "child_started"):
            value = state.get(key)
            if value is not None:
                try:
                    valid = type(value) in (int, float) and value > 0 and math.isfinite(value)
                except OverflowError:
                    valid = False
                if not valid:
                    raise ValueError(key + " must be a finite positive process timestamp or null")
        return state
    except (UnicodeError, ValueError, TypeError) as exc:
        raise StateFileError(path, raw, exc) from exc


def copy_files(source, destination, files):
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("linked input outside project refused")
        target = inside(destination, path.relative_to(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def tree_files(folder):
    return sorted(p for p in folder.rglob("*") if p.is_file() and not set(p.relative_to(folder).parts) & SKIP)


def detect(source):
    if source.is_file():
        if source.suffix.lower() not in BOOK_EXT:
            raise ValueError("unsupported book format")
        return "books"
    if (source / "pages").is_dir() and (source / "structure.json").is_file():
        return "ocr"
    if (source / "book/main.tex").is_file() or (source / "main.tex").is_file():
        return "skeleton"
    return "books"


def validate_settings(cfg, route):
    if set(cfg) - set(DEFAULTS):
        raise ValueError("unknown settings: " + ", ".join(sorted(set(cfg) - set(DEFAULTS))))
    cfg = dict(DEFAULTS, **cfg)
    if type(cfg["allow_remote"]) is not bool or type(cfg["polish"]) is not bool:
        raise ValueError("allow_remote/polish must be JSON booleans")
    validate_endpoint(cfg["host"], cfg["allow_remote"])
    if cfg["mode"] not in ("hymt", "generic") or cfg["extract"] not in ("auto", "digital", "scan", "math"):
        raise ValueError("unsupported mode/extractor")
    for key in ("parallel", "parts", "timeout", "mineru_timeout"):
        if type(cfg[key]) is not int or cfg[key] <= 0:
            raise ValueError(key + " must be positive integer")
    for key in ("split_chars", "group_chars", "context_per_request"):
        if type(cfg[key]) is not int or cfg[key] < 0:
            raise ValueError(key + " must be nonnegative integer")
    if cfg["layout"] not in ("screen", "print", "compact"):
        raise ValueError("layout must be screen, print or compact")
    for key in ("model", "source_lang", "target_lang", "title", "author", "keep_source", "glossary_profile", "book_profile"):
        if not isinstance(cfg[key], str) or (key in ("model", "source_lang", "target_lang") and not cfg[key].strip()):
            raise ValueError(key + " must be a string")
    if cfg["polish"]:
        raise ValueError("dispatcher does not polish; explicitly use the optional polish workflow in КОНФИГУРАЦИИ.md")
    if cfg["keep_source"] and route != "books":
        raise ValueError("keep_source is currently supported for a single book job")
    if cfg["book_profile"] and route != "books":
        raise ValueError("book_profile is currently supported for a single book job")
    if route != "ocr" and (cfg["source_lang"] != "English" or cfg["target_lang"] != "Russian"):
        raise ValueError("books/skeleton currently support EN->RU; use OCR route or explicit advanced commands for other languages")
    return cfg


def initialize(source, out, settings=None):
    source, out = Path(source).resolve(), Path(out).resolve()
    if not source.exists() or out.exists():
        raise ValueError("input must exist; --out must be a NEW directory")
    route = detect(source)
    cfg = dict(DEFAULTS, source_lang="German" if route == "ocr" else "English")
    if route == "ocr":
        # OCR manifests bind the original aligned blocks. Older jobs stored the
        # global default (900), which this route always ignored; load_job keeps
        # those jobs resumable, while new jobs expose the effective setting.
        cfg["group_chars"] = 0
        if (settings or {}).get("group_chars", 0) != 0:
            raise ValueError("OCR route requires group_chars=0 to preserve manifest block alignment")
    cfg.update(settings or {})
    cfg = validate_settings(cfg, route)
    books = []
    if route == "books":
        # Directory mode does not accidentally translate README/glossary/logs.
        books = [source] if source.is_file() else sorted(p for p in source.iterdir()
                 if p.is_file() and p.suffix.lower() in BOOK_EXT - {".md", ".txt", ".rst", ".html", ".htm"}
                 and not p.name.endswith(".ru.pdf"))
        if not books:
            raise ValueError("no books; for Markdown/text pass the single file explicitly")
        files = []
    elif route == "ocr":
        files = [p for p in tree_files(source) if p.relative_to(source).parts[0] in
                 {"pages", "images", "structure.json", "glossary.md", "glossary.profile.json", "pagemap.json", "figures.json"}]
    else:
        files = tree_files(source)
        pdfs = list(source.glob("*.pdf"))
        if len(pdfs) != 1:
            raise ValueError("skeleton needs exactly one source PDF in project root; prepare a separate input project")
    policy_path = None
    if cfg["keep_source"]:
        if len(books) != 1:
            raise ValueError("keep_source requires a single book, with its own unit indices")
        from source_preservation import load_policy
        policy_path = Path(cfg["keep_source"]).resolve()
        load_policy(policy_path)
    profiles = {}
    for key, filename in (("glossary_profile", "request-glossary.profile.json"),
                          ("book_profile", "book.profile.json")):
        if cfg[key]:
            path = Path(cfg[key]).resolve()
            if key == "book_profile" and len(books) != 1:
                raise ValueError("book_profile requires one book with exact content decisions")
            if key == "glossary_profile":
                from glossary_selection import load_profile
                load_profile(path)
            else:
                from content_coverage import load_profile
                load_profile(path)
            profiles[key] = (path, filename)
    out.mkdir(parents=True)
    source_copy = out / "input"
    source_copy.mkdir()
    entries = []
    if route == "books":
        for n, book in enumerate(books, 1):
            key = f"book-{n:03d}"
            dest = source_copy / key
            dest.mkdir()
            shutil.copy2(book, dest / book.name)
            for asset in ("images", "glossary.md", "glossary.profile.json"):
                path = book.parent / asset
                if path.is_dir():
                    copy_files(book.parent, dest, tree_files(path))
                elif path.is_file() and path != book:
                    shutil.copy2(path, dest / asset)
            entries.append({"id": key, "source": f"input/{key}/{book.name}", "name": book.name})
    else:
        copy_files(source, source_copy, files)
    if policy_path:
        shutil.copy2(policy_path, source_copy / "keep-source.json")
        cfg["keep_source"] = "input/keep-source.json"
    for key, (path, filename) in profiles.items():
        shutil.copy2(path, source_copy / filename)
        cfg[key] = "input/" + filename
    inputs = {p.relative_to(out).as_posix(): sha_file(p) for p in tree_files(source_copy)}
    job = {"schema": 1, "route": route, "original": str(source), "settings": cfg,
           "books": entries, "inputs": inputs}
    write_json(out / JOB, job)
    write_json(out / STATE, {"schema": 1, "status": "ready", "route": route,
                           "completed": 0, "next_action": "run", "polish": "not_requested"})
    return job


def load_job(root):
    job = read(root / JOB)
    if job.get("schema") != 1 or job.get("route") not in ("books", "ocr", "skeleton"):
        raise ValueError("invalid job schema/route")
    job["settings"] = validate_settings(job["settings"], job["route"])
    if {p.relative_to(root).as_posix() for p in tree_files(root / "input")} != set(job["inputs"]):
        raise ValueError("input snapshot inventory changed; initialize a new job")
    for path, digest in job["inputs"].items():
        if sha_file(inside(root, path)) != digest:
            raise ValueError("input snapshot changed; initialize a new job: " + path)
    return job


def command(script, *args):
    return [venv_python(), "-X", "utf8", str(ROOT / script), *map(str, args)]


def code_identity():
    return canonical_hash({p.relative_to(ROOT).as_posix(): sha_file(p)
                           for folder in ("scripts", "skeleton", "assets") for p in (ROOT / folder).glob("*")
                           if p.is_file() and p.suffix in (".py", ".ps1", ".tex", ".lua")})


def steps_for(root, job):
    cfg = job["settings"]
    common = ["--host", cfg["host"], "--model", cfg["model"], "--mode", cfg["mode"], "--parallel", str(cfg["parallel"])]
    common += ["--split-chars", str(cfg["split_chars"]),
               "--context-per-request", str(cfg["context_per_request"])]
    def glossary_profile(folder):
        path = inside(root, cfg["glossary_profile"]) if cfg.get("glossary_profile") else folder / "glossary.profile.json"
        return ["--glossary-profile", path] if path.is_file() else []
    work = root / "work"
    if job["route"] == "books":
        return [(entry["id"], command("scripts/translate_book.py", inside(root, entry["source"]),
                 "--workdir", work / entry["id"], "--extract", cfg["extract"],
                  "--mineru-timeout", cfg["mineru_timeout"], *common,
                  "--author", cfg["author"],
                  "--group-chars", str(cfg["group_chars"]),
                  "--layout", cfg["layout"],
                 *glossary_profile(inside(root, entry["source"]).parent),
                 *(["--book-profile", inside(root, cfg["book_profile"])] if cfg.get("book_profile") else []),
                 *(["--keep-source", inside(root, cfg["keep_source"])] if cfg["keep_source"] else []),

                 *(["--title", cfg["title"]] if cfg["title"] else []),
                 *(["--glossary", inside(root, entry["source"]).parent / "glossary.md"]
                   if (inside(root, entry["source"]).parent / "glossary.md").is_file() else [])),
                 work / entry["id"] / (Path(entry["name"]).stem + ".ru.pdf")) for entry in job["books"]]
    if job["route"] == "skeleton":
        main = "book/main.pdf" if (root / "input/book/main.tex").is_file() else "main.pdf"
        return [("skeleton", command("skeleton/translate_into_skeleton.py", work,
                 "--extract", cfg["extract"], "--mineru-timeout", cfg["mineru_timeout"], *common,
                 "--group-chars", cfg["group_chars"], *glossary_profile(root / "input")), work / main)]
    glossary = ["--glossary", work / "glossary.md"] if (root / "input/glossary.md").is_file() else []
    gp = glossary_profile(root / "input")
    extras = []
    for flag, name in (("--pagemap", "pagemap.json"), ("--figures", "figures.json")):
        if (root / "input" / name).is_file():
            extras += [flag, work / name]
    return [("prepare", command("scripts/build_ocr_md.py", work, "--parts", cfg["parts"], *extras), None),
            ("translate", command("scripts/run_doc_translate.py", work, *common, *glossary, *gp,
                 "--source-lang", cfg["source_lang"], "--target-lang", cfg["target_lang"]), None),
            ("build", command("scripts/assemble_ru_book.py", work, "--title", cfg["title"] or "Перевод",
                 "--author", cfg["author"], "--pdf", root / "result.pdf"), root / "result.pdf")]


def process_stamp(pid):
    import psutil
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def alive(pid, stamp):
    return bool(pid and stamp and process_stamp(pid) == stamp)


def receipt(pdf):
    rep = read(str(pdf) + ".release.json")
    if rep.get("status") != "passed" or rep.get("profile") != "release" or rep.get("pdf_sha256") != sha_file(pdf):
        raise ValueError("missing/invalid technical release: " + str(pdf))
    return rep


def fingerprints(root, files):
    return {p.relative_to(root).as_posix(): sha_file(p) for p in files if p.is_file()}


def outputs_for(root, name, pdf):
    if pdf:
        receipt(pdf)
        return fingerprints(root, [pdf, Path(str(pdf) + ".release.json")])
    work = root / "work"
    manifest = load_manifest(work)
    if name == "prepare":
        paths = [work / "parts.manifest.json", work / "book.md", work / "structure.trace.json", work / "headings.ru.json"]
        paths += [inside(work, r["path"]) for r in manifest["parts"]]
    else:
        summary = read(work / "translation.run.json")
        if summary.get("status") != "passed" or summary.get("scope") != "all_parts":
            raise ValueError("incomplete translation receipt")
        paths = [work / "translation.run.json"]
        paths += [inside(work, r["output"] + suffix) for r in manifest["parts"] for suffix in ("", ".report.json")]
    return fingerprints(root, paths)


def unchanged(root, hashes):
    try:
        return bool(hashes) and all(sha_file(inside(root, p)) == h for p, h in hashes.items())
    except (OSError, ValueError):
        return False


def status(root):
    try:
        state = read_state(root) if (root / STATE).is_file() else {"status": "not_initialized"}
    except StateFileError as exc:
        return {"schema": 1, "status": "needs_attention", "next_action": "inspect_state",
                "reason": str(exc), "state_error": exc.details}
    if state.get("status") == "running" and not alive(state.get("pid"), state.get("started")):
        state = dict(state, status="needs_attention" if alive(state.get("child_pid"), state.get("child_started")) else "interrupted",
                     next_action="inspect_live_child" if alive(state.get("child_pid"), state.get("child_started")) else "run")
    if state.get("status") == "done":
        try:
            job = load_job(root)
            if state.get("job_sha256") != canonical_hash(job) or state.get("code_sha256") != code_identity() or not unchanged(root, state.get("artifacts", {})):
                state = dict(state, status="stale", next_action="run")
        except (OSError, ValueError, KeyError):
            state = dict(state, status="stale", next_action="check_inputs")
    # No book text, secrets or logs in the controller's context.
    return {k: v for k, v in state.items() if k not in ("steps", "artifacts", "pid", "started", "child_pid", "child_started")}


def run_job(root):
    job = load_job(root)
    old = read_state(root)
    holders = [(old.get("child_pid"), old.get("child_started"))]
    if old.get("status") == "running":
        holders.append((old.get("pid"), old.get("started")))
    for pid, stamp in holders:
        if alive(pid, stamp):
            raise ValueError("job or orphan child still alive; no duplicate launch")
    identity = canonical_hash(job)
    code = code_identity()
    if old.get("job_sha256") == identity and old.get("code_sha256") == code and old.get("status") == "done" and unchanged(root, old.get("artifacts", {})):
        return 0
    # Fail rather than queue silently behind a long run; no spinning controller.
    lock = PipelineLock(note="dispatcher:" + root.name)
    try:
        lock.acquire(wait=False)
    except SystemExit as exc:
        raise ValueError("pipeline busy; wait for the other run: " + str(exc)) from exc
    state = {"schema": 1, "status": "running", "route": job["route"], "job_sha256": identity,
             "code_sha256": code, "pid": os.getpid(), "started": process_stamp(os.getpid()),
             "steps": {}, "completed": 0, "polish": "not_requested", "semantic_review": "not_run",
             "local_only": not job["settings"]["allow_remote"], "next_action": "wait"}
    child = None
    try:
        work = root / "work"
        if not work.exists():
            work.mkdir()
            if job["route"] != "books":
                copy_files(root / "input", work, tree_files(root / "input"))
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1",
                   FREETRANSLATE_LOCAL_ONLY="0" if job["settings"]["allow_remote"] else "1")
        if not job["settings"]["allow_remote"]:
            env.pop("LLM_API_KEY", None)
        env["PATH"] = str(Path(venv_python()).parent) + os.pathsep + env.get("PATH", "")
        steps = steps_for(root, job)
        state["total"] = len(steps)
        reusable = old.get("job_sha256") == identity and old.get("code_sha256") == code
        artifacts = {}
        for index, (name, cmd, pdf) in enumerate(steps):
            prior = old.get("steps", {}).get(name, {})
            if reusable and prior.get("status") == "passed" and unchanged(root, prior.get("outputs", {})):
                state["steps"][name] = prior
                state["completed"] += 1
                artifacts.update(prior["outputs"])
                continue
            reusable = False  # downstream stage identities must be re-established
            log = root / "logs" / (name + "-" + uuid.uuid4().hex[:8] + ".log")
            log.parent.mkdir(exist_ok=True)
            state.update(stage=name, log=str(log), elapsed_seconds=0)
            write_json(root / STATE, state)
            with log.open("wb") as stream:
                child = subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                         stdout=stream, stderr=subprocess.STDOUT)
                start = time.monotonic()
                state.update(child_pid=child.pid, child_started=process_stamp(child.pid))
                while child.poll() is None:
                    state.update(elapsed_seconds=round(time.monotonic() - start), log_bytes=log.stat().st_size,
                                 updated_at=time.time())
                    write_json(root / STATE, state)
                    if time.monotonic() - start > job["settings"]["timeout"]:
                        _kill_tree(child.pid)
                        child.wait(timeout=30)
                        raise TimeoutError("stage timeout; inspect log, then resume from disk cache")
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                rc = child.returncode
                child = None
            state.pop("child_pid", None)
            state.pop("child_started", None)
            if rc:
                state.update(status="needs_attention", returncode=rc, next_action="inspect_log_then_run",
                             reason="stage refused; cached successes retained; no automatic cloud/polish/force")
                write_json(root / STATE, state)
                return rc if rc in (3, 4, 5, 124) else 1
            outputs = outputs_for(root, name, pdf)
            state["steps"][name] = {"status": "passed", "outputs": outputs}
            artifacts.update(outputs)
            state["completed"] = index + 1
            write_json(root / STATE, state)
        state.update(status="done", artifacts=artifacts, next_action="open_results",
                     results=[str(pdf) for _, _, pdf in steps if pdf], acceptance="technical_only")
        write_json(root / STATE, state)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if child and child.poll() is None:
            _kill_tree(child.pid)
            child.wait(timeout=30)
        state.pop("child_pid", None)
        state.pop("child_started", None)
        state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "needs_attention",
                     reason=str(exc), next_action="inspect_log_then_run")
        write_json(root / STATE, state)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        lock.release()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("input")
    init.add_argument("--out", required=True)
    init.add_argument("--settings", help="JSON with explicit model/endpoint/options; defaults are local")
    for action in ("run", "status", "plan", "background-status", "stop"):
        sub.add_parser(action).add_argument("job")
    start = sub.add_parser("start", help="Windows: one-shot hidden task as current user")
    start.add_argument("job")
    start.add_argument("--start-lmstudio", action="store_true")
    start.add_argument("--lmstudio-profile")
    start.add_argument("--lmstudio-models")
    start.add_argument("--lmstudio-backend")
    start.add_argument("--lmstudio-app")
    start.add_argument("--lms-exe")
    start.add_argument("--startup-timeout", type=int, default=120)
    args = ap.parse_args()
    try:
        if args.action == "init":
            initialize(args.input, args.out, read(args.settings) if args.settings else None)
            result = status(Path(args.out).resolve())
        else:
            root = Path(args.job).resolve()
            if args.action in ("start", "background-status", "stop"):
                import background_run
                if args.action == "start":
                    result = background_run.start(root, start_lmstudio=args.start_lmstudio,
                        lmstudio_profile=args.lmstudio_profile, lmstudio_models=args.lmstudio_models,
                        lmstudio_backend=args.lmstudio_backend,
                        lms_exe=args.lms_exe, lmstudio_app=args.lmstudio_app, startup_timeout=args.startup_timeout)
                else:
                    result = (background_run.stop if args.action == "stop" else background_run.status)(root)
                print(json.dumps(result, ensure_ascii=False))
                return 0
            if args.action == "run":
                rc = run_job(root)
                print(json.dumps(status(root), ensure_ascii=False))
                return rc
            if args.action == "plan":
                job = load_job(root)
                result = {"route": job["route"], "local_only": not job["settings"]["allow_remote"],
                          "polish": False, "steps": [{"stage": n, "argv": cmd} for n, cmd, _ in steps_for(root, job)]}
            else:
                result = status(root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"status": "needs_attention", "reason": str(exc), "next_action": "check_job"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
