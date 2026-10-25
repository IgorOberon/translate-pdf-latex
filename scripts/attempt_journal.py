"""Immutable diagnostics for each model attempt, separate from accepted caches.

Write the request before network IO, response bytes before JSON decoding, and the
validation result afterwards. An unfinished attempt intentionally remains on disk
with its last completed event. No headers are accepted or recorded. Secret fields,
URL credentials and known API tokens are redacted; recorded original hashes allow
comparison without putting secrets back into the trace.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote_plus, urlsplit, urlunsplit
import uuid


SCHEMA = 1
_CURRENT = ContextVar("translation_attempt", default=None)
_REDACTED = "[REDACTED]"
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SECRET_KEYS = re.compile(
    r"^(?:authorization|proxyauthorization|apikey|xapikey|accesstoken|"
    r"refreshtoken|idtoken|token|secret|clientsecret|password|passwd|"
    r"credential|credentials|cookie|setcookie|headers|requestheaders|responseheaders)$"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|passwd|secret|token)([\"']?\s*[=:]\s*[\"']?)([^\s,;\"'&}\]]+)"
)
_AUTH = re.compile(
    r"(?i)\b((?:proxy[-_])?authorization[\"']?\s*[:=]\s*[\"']?)"
    r"(?:(?:bearer|basic)\s+)?[^\r\n,;\"'}]+"
)
_API_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _is_secret_key(key):
    return bool(_SECRET_KEYS.fullmatch(re.sub(r"[^a-z0-9]", "", str(key).lower())))


def _safe_url(match):
    value = match.group(0)
    try:
        parts = urlsplit(value)
        # Split at the last @: usernames/passwords can themselves contain @.
        netloc = parts.netloc.rsplit("@", 1)[-1]
        if "@" in parts.netloc:
            netloc = _REDACTED + "@" + netloc
        # Keep harmless URLs byte-for-byte, including escape spelling, repeated
        # parameters and anchors occurring in source/model text.
        fields = []
        for field in parts.query.split("&"):
            key, separator, val = field.partition("=")
            fields.append(key + "=" + _REDACTED if _is_secret_key(unquote_plus(key)) else field)
        query = "&".join(fields)
        if netloc == parts.netloc and query == parts.query:
            return value
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except ValueError:
        return "[REDACTED_URL]"


def sanitize(value, secrets=()):
    """Copy JSON-like data, preserving ordinary model text and generation fields."""
    if isinstance(value, dict):
        return {str(key): _REDACTED if _is_secret_key(key) else sanitize(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        text = value
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            text = text.replace(secret, _REDACTED)
        text = _URL.sub(_safe_url, text)
        text = _AUTH.sub(lambda m: m[1] + _REDACTED, text)
        text = _SECRET_ASSIGNMENT.sub(lambda m: m[1] + m[2] + _REDACTED, text)
        return _API_TOKEN.sub(_REDACTED, text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"journal metadata must be JSON-compatible, got {type(value).__name__}")


def _exclusive_json(path, value):
    # Never use atomic replace: an existing journal is evidence, not a cache.
    data = _json_bytes(value)
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def current_attempt():
    """Current worker's attempt, or None for uninstrumented standalone calls."""
    return _CURRENT.get()


class AttemptJournal:
    """A fresh run directory below ``root``; repeated/concurrent runs coexist."""

    def __init__(self, root, metadata=None, secrets=()):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Known credentials are only used in memory for redaction, never emitted.
        self._secrets = tuple(secrets) + tuple(
            os.environ[key] for key in ("LLM_API_KEY", "OPENAI_API_KEY")
            if os.environ.get(key))
        self.run_id = uuid.uuid4().hex
        self.path = self.root / ("run-" + self.run_id)
        self.path.mkdir()  # Collisions fail closed instead of adopting old data.
        _exclusive_json(self.path / "run.json", {
            "schema": SCHEMA, "run_id": self.run_id, "started_at": _utc_now(),
            "metadata": sanitize(metadata or {}, self._secrets),
            "redacted": True, "purpose": "diagnostics; not accepted translation cache",
        })

    def attempt(self, context=None):
        return AttemptRecord(self, context or {})


class AttemptRecord:
    def __init__(self, journal, context):
        self.journal = journal
        self.attempt_id = uuid.uuid4().hex
        self.path = journal.path / ("attempt-" + self.attempt_id)
        self.path.mkdir()
        self._lock = threading.Lock()
        self._sequence = 0
        self._started = time.monotonic()
        self._token = None
        self._entered = False
        self._validation_ok = None
        self._error_recorded = False
        self.event("started", context=context)

    def event(self, kind, **details):
        """Persist one immutable event; a write failure prevents silent success."""
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", kind):
            raise ValueError("invalid journal event kind")
        with self._lock:
            self._sequence += 1
            record = {"schema": SCHEMA, "run_id": self.journal.run_id,
                      "attempt_id": self.attempt_id, "sequence": self._sequence,
                      "event": kind, "at": _utc_now(),
                      "elapsed_seconds": time.monotonic() - self._started,
                      **sanitize(details, self.journal._secrets)}
            path = self.path / f"{self._sequence:04d}-{kind}.json"
            _exclusive_json(path, record)
            return path

    def request(self, url, payload):
        """Call before IO. ``payload`` is the exact JSON object given to the API."""
        return self.event("request", endpoint=url, payload=payload,
                          payload_sha256=_sha(_json_bytes(payload)))

    def response(self, body, status=None, elapsed_seconds=None, partial=False):
        """Save bytes/text before parsing or stripping thinking/whitespace.

        Invalid UTF-8 bytes are represented by escape sequences; their original
        byte hash is retained. Secret redaction is the only text transformation
        for valid UTF-8 responses.
        """
        if not isinstance(body, (bytes, str)):
            raise TypeError("response body must be bytes or str")
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        original = raw.decode("utf-8", errors="backslashreplace")
        safe_text = sanitize(original, self.journal._secrets)
        # A server can echo arbitrary headers/credentials as JSON fields. Walk
        # these as data too; malformed JSON still keeps its scrubbed raw text.
        try:
            parsed = json.loads(safe_text)
            scrubbed = sanitize(parsed, self.journal._secrets)
            if scrubbed != parsed:
                safe_text = json.dumps(scrubbed, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        return self.event("response", raw_text=safe_text, redacted=safe_text != original,
                          raw_sha256=_sha(raw), raw_bytes=len(raw), http_status=status,
                          transport_seconds=elapsed_seconds, partial=bool(partial))

    def model_result(self, data):
        """Save parsed metadata and original content, even for refusal/truncation."""
        data = data if isinstance(data, dict) else {"unrecognized_response": data}
        choices = data.get("choices")
        if not isinstance(choices, list):
            choices = []
        return self.event("model-result", returned_model=data.get("model"),
                          finish_reasons=[choice.get("finish_reason")
                                          for choice in choices if isinstance(choice, dict)],
                          usage=data.get("usage"), choices=choices)

    def validation(self, ok, reasons=(), candidate_text=None, repaired=False, stage="candidate"):
        self._validation_ok = bool(ok)
        return self.event("validation", ok=bool(ok), reasons=list(reasons),
                          candidate_text=candidate_text,
                          candidate_sha256=(_sha(candidate_text.encode("utf-8"))
                                            if candidate_text is not None else None),
                          repaired=bool(repaired), stage=stage)

    def error(self, exc, stage="attempt"):
        self._error_recorded = True
        return self.event("error", error_type=type(exc).__name__, message=str(exc), stage=stage)

    def __enter__(self):
        if self._entered:
            raise RuntimeError("an attempt context cannot be reused")
        self._entered = True
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc is not None and not self._error_recorded:
                self.error(exc)
            if exc is not None:
                status = "failed" if isinstance(exc, Exception) else "interrupted"
            elif self._error_recorded:
                status = "failed"
            elif self._validation_ok is None:
                status = "unvalidated"
            else:
                status = "accepted" if self._validation_ok else "rejected"
            self.event("finished", status=status)
        except OSError as journal_error:
            if exc is None:
                raise
            # Preserve the original interruption/error; request/response events
            # already on disk remain evidence of incomplete finalization.
            if hasattr(exc, "add_note"):
                exc.add_note(f"attempt journal finalization failed: {type(journal_error).__name__}")
        finally:
            _CURRENT.reset(self._token)
        return False
