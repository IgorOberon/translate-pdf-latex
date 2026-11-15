"""Read-only, bounded pipeline diagnosis. No API calls, launches or repairs."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
import pipeline
from pipeline_queue import classify_failure
from provenance import check_result, canonical_hash
from attempt_journal import sanitize

JSON_LIMIT = 16 * 1024 * 1024
LOG_LIMIT = 65536
REPORT_LIMIT = 80
DIRECTORY_LIMIT = 2000


def local_path(root, value):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("missing artifact path")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("artifact outside job")
    return path


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(JSON_LIMIT + 1)
    if len(raw) > JSON_LIMIT:
        raise ValueError("JSON exceeds diagnostic size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def report_paths(root, warnings):
    count = 0
    for visited, (directory, dirs, names) in enumerate(os.walk(root / "work"), 1):
        if visited > DIRECTORY_LIMIT:
            warnings.append("directory_scan_limited")
            return
        dirs[:] = sorted(d for d in dirs if d not in {
            "build", "extract", "images", "media", "__pycache__", ".lineage", ".compile-work"}
            and not d.startswith((".tcache", ".targeted-cache"))
            and not d.endswith(".attempts")
            and not (Path(directory) / d).is_symlink()
            and not getattr(Path(directory) / d, "is_junction", lambda: False)())
        for name in sorted(names):
            if not name.endswith(".report.json"):
                continue
            count += 1
            if count > REPORT_LIMIT:
                warnings.append("report_scan_limited")
                return
            try:
                yield local_path(root, Path(directory) / name)
            except ValueError:
                warnings.append("outside_job_report_ignored")


def mt_summary(root, path):
    row = {"path": path.relative_to(root).as_posix(), "identity": "unverified"}
    try:
        data, digest = read_json(path)
        if data.get("stage") != "raw_mt":
            return None
        row["report_sha256"] = digest
        row["declared_status"] = data.get("status") if data.get("status") in ("passed", "failed") else "invalid"
        source = local_path(root, data.get("src"))
        target = path.with_name(path.name.removesuffix(".report.json"))
        if data.get("dst") and local_path(root, data["dst"]) != target:
            raise ValueError("report target differs")
        # Provenance/coverage, not semantic acceptance or a substitute for the
        # current MT candidate validator. Suppress imported modules' banners.
        with redirect_stdout(io.StringIO()):
            check_result(source, target)
        counts = {key: data.get(key) for key in ("blocks_total", "from_cache", "translated", "kept_source")}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError("invalid report counts")
        failed = data.get("failed_blocks", [])
        if not isinstance(failed, list):
            raise ValueError("invalid failed blocks")
        fragment = data.get("fragment_cache", {})
        if not isinstance(fragment, dict):
            raise ValueError("invalid fragment counts")
        fragments = {k: fragment.get(k, 0) for k in ("reused", "saved")}
        if any(type(value) is not int or value < 0 for value in fragments.values()):
            raise ValueError("invalid fragment counts")
        row.update(identity="verified", counts=counts, fragments=fragments,
                   failed_count=len(failed), candidate_validation="not_repeated",
                   request_config_sha256=data.get("request_config_sha256"))
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        row["issue"] = type(exc).__name__  # no raw source/reply/error text
    return row


def compare(snapshot, previous):
    if previous is None:
        return {"activity": "not_sampled", "useful_rate_established": False}
    if (not isinstance(previous, dict) or previous.get("schema") != 1
            or previous.get("job") != snapshot["job"]):
        raise ValueError("previous snapshot belongs to another job or schema")
    if previous.get("run_identity") != snapshot["run_identity"]:
        return {"activity": "different_run", "useful_rate_established": False}
    keys = ("completed_stages", "stage", "log", "mt_reports")
    changed = any(previous.get(key) != snapshot.get(key) for key in keys)
    return {"activity": "observed" if changed else "not_observed",
            "useful_rate_established": False,
            "interpretation": "activity is not an accepted-text rate; unchanged snapshot is not proof of a hang"}


def diagnose(root, previous=None, status_reader=None):
    root = Path(root).resolve()
    warnings = []
    status = (status_reader or pipeline.status)(root)
    state = {}
    try:
        state = pipeline.read_state(root)
    except (OSError, ValueError):
        pass  # status already describes missing/corrupt state; never repair it
    result = {"schema": 1, "job": str(root), "observed_at": time.time(),
              "status": status.get("status"), "stage": status.get("stage"),
              "completed_stages": status.get("completed", 0), "total_stages": status.get("total"),
              "run_identity": canonical_hash([state.get(k) for k in
                  ("job_sha256", "code_sha256", "pid", "started")]),
              "warnings": warnings, "mt_reports": [], "log": None,
              "failure": None, "next_action": "inspect_state", "read_only": True}
    log_text = ""
    if status.get("log"):
        try:
            log = local_path(root, status["log"])
            with log.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - LOG_LIMIT))
                raw = stream.read(LOG_LIMIT)
            log_text = raw.decode("utf-8", errors="replace")
            result["log"] = {"path": log.relative_to(root).as_posix(), "bytes": size,
                             "tail_bytes": len(raw), "tail_sha256": hashlib.sha256(raw).hexdigest()}
        except (OSError, ValueError):
            warnings.append("current_log_unreadable_or_outside_job")
    for path in report_paths(root, warnings):
        row = mt_summary(root, path)
        if row is not None:
            result["mt_reports"].append(row)
    if status.get("state_error"):
        result["failure"] = "corrupt_state"
    elif status.get("next_action") == "inspect_live_child":
        result["next_action"] = "inspect_live_child_no_duplicate_launch"
    elif result["status"] == "running":
        result["next_action"] = "observe_no_duplicate_launch"
    elif result["status"] == "done":
        result["next_action"] = "open_verified_results"
    elif result["status"] == "ready":
        result["next_action"] = "run_when_requested"
    elif result["status"] == "not_initialized":
        result["next_action"] = "locate_job_or_init_new_when_requested"
    elif result["status"] == "stale":
        changes = []
        if state.get("code_sha256") != pipeline.code_identity():
            changes.append("code")
        try:
            if state.get("job_sha256") != canonical_hash(pipeline.load_job(root)):
                changes.append("job_configuration")
        except (OSError, ValueError, KeyError):
            changes.append("inputs_unverified")
        if not pipeline.unchanged(root, state.get("artifacts", {})):
            changes.append("artifacts_unverified")
        result.update(identity_changes=changes, next_action="inspect_identity_then_revalidate_retained_cache")
    elif result["status"] == "interrupted":
        result.update(failure="interrupted", next_action="resume_from_verified_cache_when_requested")
    else:
        category = classify_failure(log_text + "\n" + str(status.get("reason", "")),
                                    status.get("returncode", 1))
        # Unknown failures need inspection; classifier's conservative default
        # is not evidence that an otherwise unreported validation failed.
        if category == "validation" and not any(r.get("failed_count") for r in result["mt_reports"]):
            category = "unknown"
        result.update(failure=category, next_action={
            "build": "repair_build_inputs_before_retry", "configuration": "repair_configuration_before_retry",
            "offline": "check_endpoint_before_uncached_resume", "transient": "bounded_retry_when_requested",
            "validation": "inspect_rejected_units_preserve_good_fragments",
            "interrupted": "resume_from_verified_cache_when_requested",
        }.get(category, "inspect_current_log_and_reports"))
    result["comparison"] = compare(result, previous)
    return sanitize(result)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("job", type=Path)
    ap.add_argument("--previous", type=Path, help="previous diagnostic JSON; read only")
    args = ap.parse_args(argv)
    try:
        previous = read_json(args.previous)[0] if args.previous else None
        result = diagnose(args.job, previous)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"schema": 1, "status": "diagnostic_error", "error_type": type(exc).__name__,
                          "read_only": True, "next_action": "inspect_paths_or_snapshot"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
