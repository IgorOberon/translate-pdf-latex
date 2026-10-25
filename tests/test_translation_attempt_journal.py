"""Exercise the real MT loop and API decoder with an in-memory HTTP opener."""
import hashlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import translate as mt
from provenance import atomic_text, check_result, sha_file


def response(content="Измерение равно 42.", finish="stop", **message_fields):
    return json.dumps({"model": "served-mock", "choices": [{"finish_reason": finish,
        "message": {"content": content, **message_fields}}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 9}}, ensure_ascii=False).encode("utf-8")


class FakeResponse(io.BytesIO):
    status = 200


class FakeOpener:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(json.loads(request.data))
        value = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return FakeResponse(value)


def attempts(report):
    root = Path(report["attempt_journal"])
    rows = [[json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]
            for directory in root.glob("attempt-*")]
    return sorted(rows, key=lambda events: (events[0]["context"].get("retry", 100), events[0]["at"]))


def event(events, name):
    return next(row for row in events if row["event"] == name)


class TranslationAttemptJournalTests(unittest.TestCase):
    def translate(self, root, responses):
        src, dst = root / "source.md", root / "target.md"
        if not src.exists():
            atomic_text(src, "The measurement is 42.")
        opener = FakeOpener(responses)
        args = ["translate", str(src), str(dst), "--cache-dir", str(root / "cache")]
        with patch.object(sys, "argv", args), patch.object(mt, "OPENER", opener), patch.object(mt.time, "sleep"):
            try:
                mt.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        report = json.loads(Path(str(dst) + ".report.json").read_text(encoding="utf-8"))
        self.assertEqual(check_result(src, dst), report)
        return src, dst, report, code, opener

    def test_rejected_number_and_raw_thinking_survive_but_only_accepted_reply_is_cached(self):
        rejected = response("  <think>private reasoning marker</think>Измерение равно 41.  ")
        accepted = response("  Измерение равно 42.  ")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, dst, report, code, opener = self.translate(root, [rejected, accepted])
            self.assertEqual((code, len(opener.requests)), (0, 2))
            records = attempts(report)
            self.assertEqual(len(records), 3)  # two API attempts and assembled result
            first, second = records[:2]
            self.assertEqual(event(first, "response")["raw_text"], rejected.decode("utf-8"))
            self.assertEqual(event(first, "response")["raw_sha256"], hashlib.sha256(rejected).hexdigest())
            self.assertEqual(event(first, "request")["payload"], opener.requests[0])
            self.assertEqual(event(second, "request")["payload"], opener.requests[1])
            self.assertIn("previous attempt changed a number", opener.requests[1]["messages"][0]["content"])
            self.assertEqual(event(first, "finished")["status"], "rejected")
            self.assertTrue(any("numbers changed" in r for r in event(first, "validation")["reasons"]))
            self.assertNotIn("think", event(first, "validation")["candidate_text"])
            self.assertEqual(event(second, "finished")["status"], "accepted")
            cache = list((root / "cache").glob("*.json"))
            self.assertEqual(len(cache), 1)
            cached = json.loads(cache[0].read_text(encoding="utf-8"))
            self.assertEqual(cached["response"], "Измерение равно 42.")
            self.assertEqual(cached["pieces"], ["Измерение равно 42."])
            self.assertEqual(dst.read_text(encoding="utf-8"), cached["response"])

    def test_refusal_truncation_invalid_json_and_invalid_shapes_are_preserved(self):
        cases = {"length": response(finish="length"),
                 "content-filter": response(finish="content_filter"),
                 "stop-with-refusal": response(refusal="Cannot translate this"),
                 "malformed-json": b'{"choices":',
                 "choice-not-object": b'{"choices":[123]}',
                 "empty-choices": b'{"choices":[]}',
                 "content-null": response(None),
                 "list-response": b'[]'}
        for name, raw in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                src, dst, report, code, opener = self.translate(root, [raw])
                self.assertEqual((code, len(opener.requests)), (4, 3))
                self.assertEqual(src.read_bytes(), dst.read_bytes())
                self.assertFalse(list((root / "cache").glob("*.json")))
                records = attempts(report)
                self.assertEqual(len(records), 3)
                for record in records:
                    self.assertEqual(event(record, "response")["raw_text"], raw.decode("utf-8"))
                    self.assertEqual(event(record, "finished")["status"], "failed")
                    self.assertIn(event(record, "error")["error_type"], ("ValueError", "JSONDecodeError"))
                    if name in ("length", "content-filter", "stop-with-refusal"):
                        self.assertEqual(event(record, "model-result")["returned_model"], "served-mock")

    def test_http_error_and_partial_transport_response_are_retained(self):
        raw = b'{"partial-response":'

        def http_error():
            raise urllib.error.HTTPError("http://localhost:1234/v1/chat/completions", 503,
                                         "Unavailable", {}, io.BytesIO(b"Temporary failure body"))

        class PartialResponse(FakeResponse):
            def read(self, *args):
                raise http.client.IncompleteRead(raw, 99)

        for name, item, body in (("http", http_error, b"Temporary failure body"),
                                 ("partial", lambda: PartialResponse(b""), raw)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                _, _, report, code, _ = self.translate(Path(tmp), [item])
                self.assertEqual(code, 4)
                for record in attempts(report):
                    saved = event(record, "response")
                    self.assertEqual(saved["raw_text"], body.decode())
                    self.assertEqual(saved["partial"], name == "partial")
                    if name == "http":
                        self.assertEqual(saved["http_status"], 503)

    def test_restart_keeps_failed_trace_and_cache_hit_creates_no_fake_api_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, failed, code, _ = self.translate(root, [response(finish="length")])
            self.assertEqual(code, 4)
            old_run = Path(failed["attempt_journal"])
            saved = {p.relative_to(old_run): sha_file(p) for p in old_run.rglob("*.json")}
            _, _, accepted, code, opener = self.translate(root, [response()])
            self.assertEqual((code, len(opener.requests)), (0, 1))
            self.assertNotEqual(failed["attempt_run_id"], accepted["attempt_run_id"])
            self.assertEqual(saved, {p.relative_to(old_run): sha_file(p) for p in old_run.rglob("*.json")})
            _, _, cached, code, opener = self.translate(root, [AssertionError("must use accepted cache")])
            self.assertEqual((code, len(opener.requests), cached["from_cache"]), (0, 0, 1))
            self.assertEqual(attempts(cached), [])
            self.assertEqual(len(list(Path(str(root / "target.md") + ".attempts").glob("run-*"))), 3)


if __name__ == "__main__":
    unittest.main()
