"""Synthetic API-count and negative-validation regressions. No GPU or books."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import paragraph_units as pu
import translate as mt


def payload(messages):
    user = messages[-1]["content"]
    marker = pu.MARKER.search(user)
    if marker:
        text = user[marker.start():]
        parts = [{"fragment_id": m[2]} for m in pu.MARKER.finditer(text) if not m[1]]
        return parts, pu.parse_response(text, parts)
    return [], [user.split("\n\n", 1)[-1]]


def run(root, source, answer, *extra):
    src, dst = root / "source.md", root / "target.md"
    src.write_text(source, encoding="utf-8", newline="")
    argv = ["translate", str(src), str(dst), "--cache-dir", str(root / "cache"),
            "--group-chars", "900", "--context-per-request", "16384", *extra]
    calls = []

    def model(host, model, messages, temperature, top_p, **kwargs):
        parts, bodies = payload(messages)
        calls.append(bodies)
        values = answer(bodies)
        return pu.encode(parts, values) if parts else values[0]

    with patch.object(sys, "argv", argv), patch.object(mt, "call_api", side_effect=model), \
            patch.object(mt.time, "sleep"):
        try:
            mt.main()
            rc = 0
        except SystemExit as exc:
            rc = exc.code
    return rc, calls, json.loads(Path(str(dst) + ".report.json").read_text(encoding="utf-8"))


def test_good_fragment_survives_failed_group_and_resume(tmp_path):
    source = "First result is 42.\n\nSecond result is 57."
    def bad(bodies):
        return ["Первый результат 42." if "First" in b else "Второй результат 58." for b in bodies]
    rc, calls, _ = run(tmp_path, source, bad, "--split-chars", "25")
    assert rc == 4
    assert sum("First" in b for c in calls for b in c) == 1
    rc, calls, report = run(tmp_path, source,
        lambda bodies: ["Второй результат 57." for _ in bodies], "--split-chars", "25")
    assert rc == 0
    assert calls and all("First" not in b for c in calls for b in c)
    assert report["fragment_cache"]["reused"] >= 1


def test_good_paragraph_inside_rejected_batch_is_not_resent(tmp_path):
    source = "First result is 42.\n\nSecond result is 57."
    def answer(bodies):
        return ["Первый результат 42." if "First" in b else
                ("Второй результат 58." if len(bodies) > 1 else "Второй результат 57.") for b in bodies]
    rc, calls, report = run(tmp_path, source, answer)
    assert rc == 0
    assert calls == [["First result is 42.", "Second result is 57."], ["Second result is 57."]]
    assert report["kept_source"] == 0


def test_interruption_checkpoints_success_before_whole_group(tmp_path):
    source = "First result is 42.\n\nSecond result is 57."
    def interrupt(bodies):
        if any("Second" in b for b in bodies):
            raise KeyboardInterrupt()
        return ["Первый результат 42."]
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, source, interrupt, "--split-chars", "25")
    rc, calls, _ = run(tmp_path, source, lambda bodies: ["Второй результат 57."], "--split-chars", "25")
    assert rc == 0 and calls == [["Second result is 57."]]


def test_fast_report_revalidates_stricter_policy_without_overwriting_old_cache(tmp_path):
    source = ("The theorem establishes existence and uniqueness under the stated assumptions. " * 6).strip()
    rc, _, _ = run(tmp_path, source, lambda bodies: ["Краткий перевод." for _ in bodies])
    assert rc == 0
    before = {p: p.read_bytes() for p in (tmp_path / "cache").rglob("*.json")}
    rc, calls, report = run(tmp_path, source, lambda bodies: ["Полный проверенный перевод теоремы. " * 20 for _ in bodies],
                            "--min-ratio", ".9")
    assert rc == 0 and calls
    assert report["policy"]["min_ratio"] == .9
    assert all(p.read_bytes() == data for p, data in before.items())


def test_unchanged_valid_resume_makes_zero_requests(tmp_path):
    source = "First result is 42.\n\nSecond result is 57."
    answer = lambda bodies: ["Первый результат 42." if "First" in b else "Второй результат 57." for b in bodies]
    assert run(tmp_path, source, answer)[0] == 0
    rc, calls, _ = run(tmp_path, source, answer)
    assert rc == 0 and calls == []


def test_whitespace_normalization_does_not_relax_ids_or_numbers():
    rows = pu.source_paragraphs(["First result is 42.", "Second result is 57."])
    fragment = pu.plan_group(rows)[0]
    good = pu.encode(fragment["paragraphs"], ["Первый результат 42.", "Второй результат 57."])
    padded = "\n".join(" \t" + line + "  \r" if pu.MARKER.fullmatch(line) else line for line in good.split("\n"))
    assert mt.validate_fragment(fragment, padded).ok
    assert not mt.validate_fragment(fragment, padded.replace("42", "43")).ok
    assert not mt.validate_fragment(fragment, padded + "\nлишний текст").ok
    assert not mt.validate_fragment(fragment, pu.encode(list(reversed(fragment["paragraphs"])))).ok
    assert not mt.validate_fragment(fragment, good + "\n" + good).ok


def test_parallel_identical_requests_have_one_producer(tmp_path):
    source = "First result is 42.\n\nFirst result is 42.\n\nFirst result is 42."
    rc, calls, report = run(tmp_path, source, lambda bodies: ["Первый результат 42."],
                            "--group-chars", "0", "--parallel", "3")
    assert rc == 0 and len(calls) == 1 and report["blocks_total"] == 3


def test_repair_stage_rechecks_settings_and_current_validator(tmp_path):
    from dataclasses import replace
    source = "First result is 42."
    rc, _, report = run(tmp_path, source, lambda bodies: ["Первый результат 42."], "--group-chars", "0")
    assert rc == 0
    src, dst = tmp_path / "source.md", tmp_path / "target.md"
    config, policy = report["request_config"], mt.ValidationPolicy()
    assert mt.reusable_plain_stage(src, dst, config, policy)
    assert not mt.reusable_plain_stage(src, dst, dict(config, model="different"), policy)
    assert not mt.reusable_plain_stage(src, dst, config, replace(policy, min_ratio=2, length_min_chars=1))
    dst.write_text("Повреждённый перевод 43.", encoding="utf-8")
    assert not mt.reusable_plain_stage(src, dst, config, policy)


def test_changed_numbers_masks_and_inline_enumerations_always_rejected():
    assert not mt.validate_candidate("Text α) 42 β) 57 ⟦M000001⟧", "Текст α) 42 57 ⟦M000001⟧").ok
    assert not mt.validate_candidate("Result 42 ⟦M000001⟧", "Результат 42 ⟦M000002⟧").ok
    assert mt.validate_candidate("The image im(γ) is defined.", "Образ im(γ) определён.").ok


def test_lossless_whitespace_split_preserves_protected_content():
    from request_plan import plan_fragments
    source = "A word without punctuation " * 60 + "⟦M000001⟧ `protected code` «quoted string»"
    fragments = plan_fragments(source, 100)
    assert len(fragments) > 1 and "".join(f["text"] for f in fragments) == source
    for protected in ("⟦M000001⟧", "`protected code`", "«quoted string»"):
        assert sum(protected in f["text"] for f in fragments) == 1
    abbreviation = "z. B. " * 50
    assert "".join(f["text"] for f in plan_fragments(abbreviation, 7)) == abbreviation
    assert all(f["text"].count("z. B.") == f["text"].count("z.")
               for f in plan_fragments(abbreviation, 7))


def test_disk_write_failure_is_not_retried_as_model_error(tmp_path):
    from fragment_cache import FragmentCache
    calls = []
    def answer(bodies):
        calls.append(bodies)
        return ["Первый результат 42."]
    with patch.object(FragmentCache, "put", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            run(tmp_path, "First result is 42.", answer)
    assert len(calls) == 1
    assert not (tmp_path / "target.md").exists()


@pytest.mark.parametrize("status, expected_calls, expected_rc", [(400, 1, 3), (503, 3, 4)])
def test_http_retry_classification_preserves_cache_and_target(tmp_path, status, expected_calls, expected_rc):
    from urllib.error import HTTPError
    calls = []
    def failure(bodies):
        calls.append(bodies)
        raise HTTPError("http://localhost:1234", status, "synthetic error", {}, None)
    rc, _, report = run(tmp_path, "First result is 42.", failure)
    assert rc == expected_rc and len(calls) == expected_calls
    assert report["status"] == "failed"
    assert not list((tmp_path / "cache").rglob("*.json"))
    if status == 400:
        assert not (tmp_path / "target.md").exists()


def test_refused_endpoint_stops_before_retrying_other_blocks(tmp_path):
    from urllib.error import URLError
    calls = []
    def failure(bodies):
        calls.append(bodies)
        raise URLError(ConnectionRefusedError(10061, "synthetic offline server"))
    rc, _, report = run(tmp_path, "First result is 42.\n\nSecond result is 57.",
                        failure, "--group-chars", "0")
    assert rc == 3 and len(calls) == 1 and report["aborted"]
    assert not (tmp_path / "target.md").exists()
