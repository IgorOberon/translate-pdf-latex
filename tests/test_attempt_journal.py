"""Synthetic request diagnostics, without models, books, or accepted caches."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from attempt_journal import AttemptJournal, current_attempt, sanitize


def events(attempt):
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(attempt.path.glob("*.json"))]


class AttemptJournalTests(unittest.TestCase):
    def test_exact_request_and_raw_rejected_response_survive_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = AttemptJournal(Path(tmp) / "attempts")
            payload = {"model": "hy-mt2", "messages": [{"role": "user", "content": "First.\n\nSecond.\n"}],
                       "max_tokens": 4096, "temperature": .2}
            raw = '{"model":"served-model","choices":[{"finish_reason":"stop","message":{"content":"  <think>x</think>Первый.\\n"}}],"usage":{"total_tokens":27}}'.encode()
            with journal.attempt({"block_index": 817, "retry": 1}) as attempt:
                self.assertIs(current_attempt(), attempt)
                attempt.request("http://localhost:1234/v1/chat/completions", payload)
                # The request is readable before a response exists.
                self.assertEqual(events(attempt)[-1]["payload"], payload)
                attempt.response(raw, status=200, elapsed_seconds=.3)
                attempt.model_result(json.loads(raw))
                attempt.validation(False, ["missing paragraph"], "Первый.")
            self.assertIsNone(current_attempt())
            rows = events(attempt)
            self.assertEqual([row["event"] for row in rows],
                             ["started", "request", "response", "model-result", "validation", "finished"])
            self.assertEqual(rows[2]["raw_text"], raw.decode())
            self.assertEqual(rows[2]["raw_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(rows[3]["returned_model"], "served-model")
            self.assertEqual(rows[3]["finish_reasons"], ["stop"])
            self.assertEqual(rows[3]["usage"], {"total_tokens": 27})
            self.assertEqual(rows[-1]["status"], "rejected")

    def test_truncated_refused_and_malformed_responses_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = AttemptJournal(tmp)
            for reason in ("length", "content_filter", "malformed-json"):
                with self.subTest(reason=reason):
                    attempt = journal.attempt({"case": reason})
                    with self.assertRaises(ValueError), attempt:
                        attempt.request("http://localhost:1234", {"model": "hy-mt2", "messages": []})
                        raw = b'{"choices":' if reason == "malformed-json" else json.dumps({
                            "model": "actual", "choices": [{"finish_reason": reason,
                            "message": {"content": None, "refusal": "Cannot translate"}}]}).encode()
                        attempt.response(raw)
                        data = json.loads(raw)
                        attempt.model_result(data)
                        raise ValueError("truncation/refusal")
                    rows = events(attempt)
                    self.assertEqual(rows[2]["raw_text"], raw.decode())
                    self.assertEqual(rows[-1]["status"], "failed")
                    self.assertIn(rows[-2]["error_type"], ("ValueError", "JSONDecodeError"))

    def test_interruption_preserves_request_and_partial_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = AttemptJournal(tmp)
            with self.assertRaises(KeyboardInterrupt):
                with journal.attempt() as attempt:
                    attempt.request("http://localhost", {"messages": ["source"]})
                    attempt.response(b'{"partial":', partial=True)
                    raise KeyboardInterrupt()
            rows = events(attempt)
            self.assertEqual(rows[1]["event"], "request")
            self.assertTrue(rows[2]["partial"])
            self.assertEqual(rows[-1]["status"], "interrupted")
            self.assertIsNone(current_attempt())

    def test_transport_error_scrubs_credentials_and_keeps_request(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"LLM_API_KEY": "private-key-value"}):
            journal = AttemptJournal(tmp)
            with self.assertRaises(TimeoutError):
                with journal.attempt() as attempt:
                    attempt.request("http://alice:pass@localhost:1234/v1?api_key=url-secret&model=hy#access_token=fragment-secret",
                                    {"model": "hy", "messages": [{"role": "user", "content": "Exact\n source"}],
                                     "api_key": "payload-secret", "headers": {"Other": "header-secret"}})
                    raise TimeoutError("private-key-value http://bob:password@localhost:1234/?token=query-secret Authorization: Bearer auth-secret")
            text = "\n".join(p.read_text(encoding="utf-8") for p in attempt.path.glob("*.json"))
            for secret in ("private-key-value", "alice:pass", "bob:password", "url-secret", "fragment-secret",
                           "payload-secret", "header-secret", "query-secret", "auth-secret"):
                self.assertNotIn(secret, text)
            self.assertIn("model=hy", text)
            self.assertEqual(events(attempt)[-1]["status"], "failed")

    def test_sanitization_preserves_content_and_generation_settings(self):
        payload = {"max_tokens": 4096, "prompt_tokens": 12, "model": "local",
                   "messages": [{"content": "  Точный ответ.\n\nДругой абзац.  "}],
                   "metadata": {"X-API-Key": "secret", "access_token": "secret2"}}
        sanitized = sanitize(payload)
        self.assertEqual(sanitized["messages"], payload["messages"])
        self.assertEqual(sanitized["max_tokens"], 4096)
        self.assertEqual(sanitized["prompt_tokens"], 12)
        self.assertEqual(sanitized["model"], "local")
        self.assertEqual(sanitized["metadata"], {"X-API-Key": "[REDACTED]", "access_token": "[REDACTED]"})
        self.assertEqual(payload["metadata"]["X-API-Key"], "secret")

    def test_response_credentials_are_redacted_even_in_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = AttemptJournal(tmp)
            for raw in ('{"api_key":"raw-secret", "headers":{"X-Other":"header-secret"}}',
                        '{"Authorization":"Bearer auth-secret","access_token":"token-secret",bad'):
                with journal.attempt() as attempt:
                    attempt.response(raw)
                row = events(attempt)[1]
                for secret in ("raw-secret", "header-secret", "auth-secret", "token-secret"):
                    self.assertNotIn(secret, row["raw_text"])
                self.assertTrue(row["redacted"])
                self.assertEqual(row["raw_sha256"], hashlib.sha256(raw.encode()).hexdigest())

    def test_harmless_urls_are_not_normalized_or_stripped_from_raw_text(self):
        original = '  See https://example.test/paper?q=a%20b&n=2&n=1#section-2\n'
        self.assertEqual(sanitize(original), original)
        with tempfile.TemporaryDirectory() as tmp:
            with AttemptJournal(tmp).attempt() as attempt:
                attempt.response(original)
            row = events(attempt)[1]
            self.assertEqual(row["raw_text"], original)
            self.assertFalse(row["redacted"])

    def test_parallel_runs_and_attempts_never_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            journals = [AttemptJournal(tmp), AttemptJournal(tmp)]

            def worker(number):
                with journals[number % 2].attempt({"number": number}) as attempt:
                    attempt.request("http://localhost", {"model": str(number)})
                    attempt.response(str(number))
                    self.assertIs(current_attempt(), attempt)
                    attempt.validation(True, candidate_text=str(number))
                self.assertIsNone(current_attempt())
                return attempt

            with ThreadPoolExecutor(max_workers=8) as pool:
                attempts = list(pool.map(worker, range(30)))
            self.assertEqual(len({a.path for a in attempts}), 30)
            self.assertEqual(len(list(Path(tmp).glob("run-*"))), 2)
            for attempt in attempts:
                rows = events(attempt)
                self.assertEqual(len(rows), 5)
                self.assertEqual(rows[-1]["status"], "accepted")
                number = rows[0]["context"]["number"]
                self.assertEqual(rows[1]["payload"]["model"], str(number))

    def test_existing_event_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt = AttemptJournal(tmp).attempt()
            path = attempt.path / "0002-request.json"
            path.write_bytes(b"preserved evidence")
            with self.assertRaises(FileExistsError):
                attempt.request("http://localhost", {"model": "new"})
            self.assertEqual(path.read_bytes(), b"preserved evidence")

    def test_nested_context_is_restored_and_unvalidated_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = AttemptJournal(tmp)
            with journal.attempt() as outer:
                with journal.attempt() as inner:
                    self.assertIs(current_attempt(), inner)
                self.assertIs(current_attempt(), outer)
            self.assertIsNone(current_attempt())
            self.assertEqual(events(inner)[-1]["status"], "unvalidated")
            self.assertEqual(events(outer)[-1]["status"], "unvalidated")

    def test_handled_transport_error_is_not_mistaken_for_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with AttemptJournal(tmp).attempt() as attempt:
                attempt.error(TimeoutError("transport timed out"), stage="transport")
            rows = events(attempt)
            self.assertEqual(rows[-2]["error_type"], "TimeoutError")
            self.assertEqual(rows[-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
