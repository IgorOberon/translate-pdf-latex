"""Bounded sequential pipeline controller. Never retry deterministic failures."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def classify_failure(text, returncode=1):
    lower = text.lower()
    if returncode == 130 or "keyboardinterrupt" in lower:
        return "interrupted"
    # Deterministic failures take precedence over incidental network messages.
    if any(s in lower for s in ("missing character", "latex failed", "unchanged build",
                                "unchanged technical", "did not stabilize")):
        return "build"
    if any(s in lower for s in ("managed model refused", "request budget exceeded",
                                "context limit", "extraction", "source changed",
                                "source/report identity", "settings/profile changed")):
        return "configuration"
    codes = [int(code) for code in re.findall(r"http(?: error)?[ :]+(\d{3})", lower)]
    if any(400 <= code < 500 and code not in (408, 429) for code in codes):
        return "configuration"
    if any(s in lower for s in ("connectionrefused", "connection refused", "10061",
                                "urlerror", "server not reachable", "endpoint unavailable")):
        return "offline"
    if any(code in (408, 429) or code >= 500 for code in codes) or any(
            s in lower for s in ("timeouterror", "timed out", "stage timeout", "connectionreset")):
        return "transient"
    return "validation"  # Unknown errors require inspection, not blind retries.


def drain(jobs, run, healthy, sleep=time.sleep, retry_delays=(5, 15)):
    """At most 1 + len(retry_delays) runs per job; bad content stops immediately."""
    results = []
    for job in jobs:
        attempts = 0
        while True:
            attempts += 1
            result = dict(run(job), job=str(job), attempts=attempts)
            if result.get("returncode") == 0:
                break
            failure = result.get("failure", "validation")
            if failure not in ("offline", "transient") or attempts > len(retry_delays):
                break
            sleep(retry_delays[attempts - 1])
            if failure == "offline" and not healthy(job):
                result["retry_stopped"] = "endpoint still unavailable"
                break
        results.append(result)
        if result.get("failure") == "interrupted":
            break
    return results


def run_pipeline(job):
    import pipeline
    prior = pipeline.status(job)
    if prior.get("status") == "done":
        return {"returncode": 0, "status": "done", "reused": True}
    rc = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).with_name("pipeline.py")),
                         "run", str(job)]).returncode
    state = pipeline.status(job)
    text = state.get("reason", "")
    log = state.get("log")
    if log and Path(log).is_file():
        with Path(log).open("rb") as stream:
            stream.seek(max(0, Path(log).stat().st_size - 65536))
            text += "\n" + stream.read().decode("utf-8", errors="replace")
    return {"returncode": rc, "status": state.get("status"),
            "failure": classify_failure(text, rc) if rc else None, "log": log}


def endpoint_healthy(job):
    import pipeline
    from endpoint_policy import model_opener
    from translate import api_url
    try:
        settings = pipeline.load_job(job)["settings"]
        url = api_url(settings["host"]).rsplit("/", 1)[0]  # /v1/chat
        url = url.rsplit("/", 1)[0] + "/models"
        with model_opener().open(url, timeout=5) as response:
            return isinstance(json.load(response).get("data"), list)
    except (OSError, ValueError, KeyError):
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("jobs", nargs="+", type=Path)
    args = ap.parse_args(argv)
    results = drain([job.resolve() for job in args.jobs], run_pipeline, endpoint_healthy)
    print(json.dumps({"jobs": results}, ensure_ascii=False, indent=2))
    return 0 if all(row["returncode"] == 0 for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
