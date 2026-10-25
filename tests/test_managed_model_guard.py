"""Native context/ID verification uses only fake owned runtimes and new caches."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lmstudio_runtime as runtime
import managed_model_guard as guard
import translate as mt
from attempt_journal import AttemptJournal
from provenance import write_json


def catalogue(context=16384, *, key="hy-mt2", instance="hy-instance", loaded=True):
    return {"models": [{"key": key, "type": "llm", "max_context_length": 262144,
                        "loaded_instances": [{"id": instance, "config": {
                            "context_length": context, "parallel": 4}}] if loaded else []}]}


@pytest.fixture
def owned(tmp_path, monkeypatch):
    profile = tmp_path / "isolated-profile"
    profile.mkdir()
    receipt = profile / runtime.RECEIPT
    write_json(receipt, {"profile": str(profile), "host": "http://127.0.0.1:16661"})
    monkeypatch.setenv("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT", str(receipt))
    monkeypatch.setattr(runtime, "_assert_live_receipt", lambda row: "http://127.0.0.1:16661")
    return receipt


def test_exact_native_key_and_instance_are_resolved_without_context_division():
    data = catalogue()
    for model_id in ("hy-mt2", "hy-instance"):
        result = guard.inspect_model(data, model_id, 7000, 8192)
        assert result["actual_context"] == 16384
        assert result["model_key"] == "hy-mt2" and result["instance_id"] == "hy-instance"


@pytest.mark.parametrize("context", [None, 0, -1, True, "16384"])
def test_architectural_maximum_never_substitutes_for_loaded_context(context):
    with pytest.raises(guard.ManagedModelError, match="confirmed positive"):
        guard.inspect_model(catalogue(context), "hy-mt2", 7000, 8192)


def test_unloaded_model_only_allowed_before_jit():
    data = catalogue(loaded=False)
    result = guard.inspect_model(data, "hy-mt2", 7000, allow_unloaded=True)
    assert result["state"] == "unloaded" and result["actual_context"] is None
    with pytest.raises(guard.ManagedModelError, match="after generation"):
        guard.inspect_model(data, "hy-mt2", 7000)


def test_multiple_instances_require_exact_instance_id():
    data = catalogue()
    data["models"][0]["loaded_instances"].append({"id": "another", "config": {"context_length": 8192}})
    with pytest.raises(guard.ManagedModelError, match="multiple loaded instances"):
        guard.inspect_model(data, "hy-mt2", 6000)
    assert guard.inspect_model(data, "another", 6000)["actual_context"] == 8192


def test_wrong_type_missing_model_and_key_instance_collision_refused():
    with pytest.raises(guard.ManagedModelError, match="absent"):
        guard.inspect_model(catalogue(), "somewhere/hy-mt2.gguf", 5000)
    wrong = catalogue()
    wrong["models"][0]["type"] = "embedding"
    with pytest.raises(guard.ManagedModelError, match="not a language model"):
        guard.inspect_model(wrong, "hy-mt2", 5000)
    collision = catalogue()
    collision["models"].extend(catalogue(key="different", instance="hy-mt2")["models"])
    with pytest.raises(guard.ManagedModelError, match="different models"):
        guard.inspect_model(collision, "hy-mt2", 5000)


def test_actual_context_rejects_estimate_even_without_declared_limit():
    with pytest.raises(guard.ManagedModelError, match="estimated 9000.*8192"):
        guard.inspect_model(catalogue(8192), "hy-mt2", 9000)


def test_unmanaged_endpoint_does_not_probe_or_require_native_api(monkeypatch):
    monkeypatch.delenv("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT", raising=False)
    monkeypatch.setattr(runtime, "_http_json", lambda *a, **kw: pytest.fail("no managed endpoint"))
    assert guard.check_managed_model("http://localhost:1234", "ordinary-model", []) is None


def test_receipt_is_bound_to_exact_endpoint_before_any_http(owned, monkeypatch):
    monkeypatch.setattr(runtime, "_http_json", lambda *a, **kw: pytest.fail("wrong endpoint must not be contacted"))
    with pytest.raises(guard.ManagedModelError, match="requested endpoint"):
        guard.check_managed_model("http://127.0.0.1:16662", "hy-mt2", [])


def test_dead_or_replaced_owned_runtime_refuses_probe(owned, monkeypatch):
    def dead(receipt):
        raise runtime.RuntimeError_("owned process identity changed")
    monkeypatch.setattr(runtime, "_assert_live_receipt", dead)
    monkeypatch.setattr(runtime, "_http_json", lambda *a, **kw: pytest.fail("unowned endpoint"))
    with pytest.raises(guard.ManagedModelError, match="identity changed"):
        guard.check_managed_model("http://127.0.0.1:16661", "hy-mt2", [])


def test_unverifiable_process_ownership_becomes_explicit_guard_refusal(owned, monkeypatch):
    def denied(receipt):
        raise runtime.psutil.AccessDenied(pid=9002)
    monkeypatch.setattr(runtime, "_assert_live_receipt", denied)
    with pytest.raises(guard.ManagedModelError, match="verification failed"):
        guard.check_managed_model("http://127.0.0.1:16661", "hy-mt2", [])


def translate_once(tmp_path, monkeypatch, native_responses, *, source_text="The vessel contains water."):
    source = tmp_path / "source.md"
    target = tmp_path / "translation.md"
    cache = tmp_path / "new-cache"
    source.write_bytes(source_text.encode("utf-8"))
    responses = iter(native_responses)
    probes, generations = [], []
    def native(base, path, body=None):
        assert base == "http://127.0.0.1:16661" and path == "/api/v1/models"
        assert body is None, "guard must never load/change models"
        probes.append(path)
        return next(responses)
    def generate(host, model, messages, temperature, top_p):
        generations.append(messages)
        return "Сосуд содержит воду."
    monkeypatch.setattr(runtime, "_http_json", native)
    monkeypatch.setattr(mt, "call_api", generate)
    monkeypatch.setattr(mt.time, "sleep", lambda *a: pytest.fail("configuration errors must not retry"))
    monkeypatch.setattr(sys, "argv", ["translate", str(source), str(target),
        "--host", "http://127.0.0.1:16661", "--model", "hy-mt2", "--mode", "hymt",
        "--cache-dir", str(cache), "--context-per-request", "16384", "--parallel", "1"])
    rc = 0
    try:
        mt.main()
    except SystemExit as exc:
        rc = exc.code
    report = json.loads(Path(str(target) + ".report.json").read_text(encoding="utf-8"))
    events = [json.loads(path.read_text(encoding="utf-8"))
              for path in Path(report["attempt_journal"]).glob("attempt-*/*.json")]
    return rc, probes, generations, report, events, target, cache


@pytest.mark.parametrize("first", [catalogue(16384), catalogue(loaded=False)])
def test_jit_or_reload_shrinking_context_rejects_reply_before_cache(owned, tmp_path, monkeypatch, first):
    rc, probes, generations, report, events, target, cache = translate_once(
        tmp_path, monkeypatch, [copy.deepcopy(first), catalogue(8192)])
    assert rc == 3 and report["status"] == "failed" and report["aborted"] is True
    assert len(probes) == 2 and len(generations) == 1
    assert not target.exists() and not list(cache.glob("*.json"))
    checks = [event for event in events if event["event"] == "runtime-model"]
    assert any(event["phase"] == "before" and event["ok"] for event in checks)
    assert any(event["phase"] == "after" and not event["ok"]
               and event["actual_context"] == 8192 and "8192" in event["reason"] for event in checks)
    assert "8192" in report["failed_blocks"][0]["reason"]


def test_invalid_model_id_aborts_without_generation_or_retry(owned, tmp_path, monkeypatch):
    rc, probes, generations, report, events, target, cache = translate_once(
        tmp_path, monkeypatch, [catalogue(key="different", instance="different-instance")])
    assert rc == 3 and not generations and len(probes) == 1
    assert "absent" in report["failed_blocks"][0]["reason"]
    assert not target.exists() and not list(cache.glob("*.json"))


def test_confirmed_loaded_context_accepts_without_mutating_receipt(owned, tmp_path, monkeypatch):
    before = owned.read_bytes()
    rc, probes, generations, report, events, target, cache = translate_once(
        tmp_path, monkeypatch, [catalogue(), catalogue()])
    assert rc == 0 and report["status"] == "passed"
    assert len(probes) == 2 and len(generations) == 1
    assert target.read_text(encoding="utf-8") == "Сосуд содержит воду."
    assert len(list(cache.glob("*.json"))) == 1
    assert owned.read_bytes() == before
    checks = [event for event in events if event["event"] == "runtime-model"]
    assert {event["actual_context"] for event in checks} == {16384}


@pytest.mark.parametrize("after", [catalogue(instance="replacement-instance"), catalogue(32768),
                                   catalogue(key="replacement-model", instance="hy-mt2")])
def test_loaded_model_identity_and_context_must_stay_bound_across_generation(owned, tmp_path, monkeypatch, after):
    rc, probes, generations, report, events, target, cache = translate_once(
        tmp_path, monkeypatch, [catalogue(), after])
    assert rc == 3 and report["aborted"] is True
    assert len(probes) == 2 and len(generations) == 1
    assert not target.exists() and not list(cache.glob("*.json"))
    rejected = next(event for event in events if event["event"] == "runtime-model"
                    and event["phase"] == "after" and not event["ok"])
    assert rejected["before_instance_id"] == "hy-instance"
    assert rejected["before_actual_context"] == 16384
    assert "changed during generation" in report["failed_blocks"][0]["reason"]


def test_unloaded_key_can_jit_load_one_confirmed_instance(owned, tmp_path, monkeypatch):
    rc, probes, generations, report, events, target, cache = translate_once(
        tmp_path, monkeypatch, [catalogue(loaded=False), catalogue()])
    assert rc == 0 and report["status"] == "passed"
    assert len(probes) == 2 and len(generations) == 1
    assert target.exists() and len(list(cache.glob("*.json"))) == 1


def test_receipt_disappearing_after_generation_does_not_disable_guard(owned, monkeypatch):
    monkeypatch.setattr(runtime, "_http_json", lambda *a, **kw: catalogue())
    before = guard.check_managed_model("http://127.0.0.1:16661", "hy-mt2", [])
    monkeypatch.delenv("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT")
    with pytest.raises(guard.ManagedModelError, match="receipt disappeared"):
        guard.check_managed_model("http://127.0.0.1:16661", "hy-mt2", [], phase="after", previous=before)


def test_all_accepted_cache_skips_runtime_probe_and_generation(owned, tmp_path, monkeypatch):
    first = translate_once(tmp_path, monkeypatch, [catalogue(), catalogue()])
    assert first[0] == 0
    cache = first[-1]
    original = {path.name: path.read_bytes() for path in cache.glob("*.json")}
    rc, probes, generations, report, events, target, cache = translate_once(tmp_path, monkeypatch, [])
    assert rc == 0 and report["from_cache"] == 1
    assert not probes and not generations
    assert {path.name: path.read_bytes() for path in cache.glob("*.json")} == original


def test_refusal_keeps_prior_accepted_cache_and_existing_target_bytes(owned, tmp_path, monkeypatch):
    first = translate_once(tmp_path, monkeypatch, [catalogue(), catalogue()])
    assert first[0] == 0
    target, cache = first[-2:]
    old_target = target.read_bytes()
    old_cache = {path.name: path.read_bytes() for path in cache.glob("*.json")}
    rc, probes, generations, report, events, target, cache = translate_once(
        tmp_path, monkeypatch, [catalogue(), catalogue(instance="replacement")],
        source_text="The vessel contains water.\n\nThe new vessel contains gas.")
    assert rc == 3 and report["aborted"] is True and report["from_cache"] == 1
    assert len(probes) == 2 and len(generations) == 1
    assert target.read_bytes() == old_target
    assert {path.name: path.read_bytes() for path in cache.glob("*.json")} == old_cache


@pytest.mark.parametrize("payload", [{}, {"data": []}, {"models": [{"key": "hy-mt2"}]}])
def test_missing_native_schema_is_a_refusal_not_a_claim_of_capacity(owned, monkeypatch, payload):
    monkeypatch.setattr(runtime, "_http_json", lambda *a, **kw: payload)
    with pytest.raises(guard.ManagedModelError, match="catalogue"):
        guard.check_managed_model("http://127.0.0.1:16661", "hy-mt2", [])
