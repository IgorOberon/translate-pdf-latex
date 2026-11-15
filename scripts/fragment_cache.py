"""Immutable, independently validated MT checkpoints below the outer group."""
import json
from pathlib import Path
import threading
from contextlib import contextmanager

from provenance import canonical_hash, sha_text, write_json


class FragmentCache:
    def __init__(self, root, config):
        self.root = Path(root) / "fragments-v1"
        self.config = config
        self.lock = threading.RLock()
        self.reused = 0
        self.saved = 0
        self.claims = {}

    @contextmanager
    def claim(self, source, kind):
        """One in-flight producer for an identical request in this process."""
        key = self.identity(source, kind)
        with self.lock:
            claim = self.claims.setdefault(key, threading.RLock())
        with claim:
            yield

    def identity(self, source, kind):
        return canonical_hash({"schema": "fragment-v1", "config": self.config,
                               "source": source, "kind": kind})

    def get(self, source, kind, validate):
        key = self.identity(source, kind)
        with self.lock:
            for path in sorted((self.root / key[:2]).glob(key[2:32] + ".*.json")):
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    text = entry["response"]
                    if (entry.get("schema") != "fragment-v1"
                            or entry.get("key") != key
                            or entry.get("source_sha256") != sha_text(source)
                            or entry.get("response_sha256") != sha_text(text)):
                        continue
                    checked = validate(text)
                    if checked.ok:
                        self.reused += 1
                        return checked.text
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return None

    def put(self, source, kind, text, validate, origin):
        checked = validate(text)
        if not checked.ok:
            return False
        key = self.identity(source, kind)
        path = self.root / key[:2] / (key[2:32] + "." + sha_text(checked.text)[:16] + ".json")
        with self.lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    write_json(path, {"schema": "fragment-v1", "key": key,
                                     "source_sha256": sha_text(source),
                                     "response": checked.text,
                                     "response_sha256": sha_text(checked.text),
                                     "origin": origin})
                except OSError as exc:
                    raise RuntimeError("cannot persist validated MT fragment") from exc
                self.saved += 1
        return True

    def split_required(self, source, policy):
        key = self.identity(source, {"split_strategy": 1, "policy": policy})
        path = self.root / "strategies" / (key + ".json")
        try:
            return json.loads(path.read_text(encoding="utf-8")) == {"key": key, "split": True}
        except (OSError, ValueError):
            return False

    def require_split(self, source, policy):
        key = self.identity(source, {"split_strategy": 1, "policy": policy})
        path = self.root / "strategies" / (key + ".json")
        with self.lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                write_json(path, {"key": key, "split": True})


def group_entries(path):
    """Old v5 entries stay readable; changed candidates never overwrite them."""
    path = Path(path)
    return ([path] if path.is_file() else []) + sorted(
        (path.parent / "revisions-v1" / path.stem[:2]).glob(path.stem[2:32] + ".*.json"))


def save_group(path, entry):
    path = Path(path)
    if path.exists():
        path = (path.parent / "revisions-v1" / path.stem[:2]
                / (path.stem[2:32] + "." + canonical_hash(entry)[:16] + ".json"))
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, entry)
