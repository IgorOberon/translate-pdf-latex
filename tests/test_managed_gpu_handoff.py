"""Managed extraction must never unload the user's separate LM Studio."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import procutils
import lmstudio_runtime


def test_managed_handoff_uses_owned_receipt_only(tmp_path, monkeypatch):
    receipt = {"profile": str(tmp_path), "token": "test-identity"}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setenv("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT", str(path))
    calls = []
    monkeypatch.setattr(lmstudio_runtime, "unload_owned_models", lambda value: calls.append(value))
    monkeypatch.setattr(procutils.subprocess, "run", lambda *a, **k: pytest.fail("global CLI called"))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    procutils.unload_llm()
    assert calls == [receipt]


def test_managed_handoff_failure_does_not_fallback(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT", str(path))
    def refused(value):
        raise RuntimeError("wrong endpoint owner")
    monkeypatch.setattr(lmstudio_runtime, "unload_owned_models", refused)
    monkeypatch.setattr(procutils.subprocess, "run", lambda *a, **k: pytest.fail("global CLI fallback"))
    with pytest.raises(RuntimeError, match="wrong endpoint owner"):
        procutils.unload_llm()
