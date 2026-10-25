"""Read-only identity/context checks for an explicitly owned LM Studio runtime.

This never loads a model or changes runtime/global settings. The first request
may use ordinary JIT loading; its reply is checked again before acceptance.
"""
import json
import os
from pathlib import Path

from attempt_journal import current_attempt
from request_budget import estimate_messages


class ManagedModelError(ValueError):
    """Deterministic runtime mismatch; retrying translation cannot fix it."""

    def __init__(self, message, observation=None):
        super().__init__(message)
        self.observation = observation or {}


def inspect_model(data, requested, estimated_tokens, declared_context=0, *, allow_unloaded=False):
    """Resolve an exact native key/instance ID; never infer from model maximums."""
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ManagedModelError("managed native model catalogue is unavailable or invalid")
    matching_keys, matching_instances = [], []
    for model in data["models"]:
        if (not isinstance(model, dict) or not isinstance(model.get("key"), str)
                or not isinstance(model.get("loaded_instances"), list)):
            raise ManagedModelError("managed native model catalogue has invalid model identity")
        if model["key"] == requested:
            matching_keys.append(model)
        for instance in model["loaded_instances"]:
            if not isinstance(instance, dict) or not isinstance(instance.get("id"), str) or not instance["id"]:
                raise ManagedModelError("managed native model catalogue has invalid instance identity")
            if instance["id"] == requested:
                matching_instances.append((model, instance))
    if len(matching_keys) > 1 or len(matching_instances) > 1:
        raise ManagedModelError("managed model ID is ambiguous; specify one exact loaded instance")
    instance = None
    if matching_instances:
        model, instance = matching_instances[0]
        if matching_keys and matching_keys[0] is not model:
            raise ManagedModelError("managed model key and instance ID identify different models")
    elif matching_keys:
        model = matching_keys[0]
        if len(model["loaded_instances"]) > 1:
            raise ManagedModelError("managed model has multiple loaded instances; specify an exact instance ID")
        if model["loaded_instances"]:
            instance = model["loaded_instances"][0]
    else:
        raise ManagedModelError("requested model ID is absent from the managed native catalogue; use an exact key or instance ID")
    if model.get("type") != "llm":
        raise ManagedModelError("requested managed model is not a language model")
    observed = {"requested_model": requested, "model_key": model["key"],
                "instance_id": instance["id"] if instance else None,
                "estimated_tokens": estimated_tokens, "declared_context": declared_context,
                "actual_context": None, "state": "unloaded" if instance is None else "loaded"}
    if instance is None:
        if not allow_unloaded:
            raise ManagedModelError("managed model has no loaded instance after generation; reply is not accepted", observed)
        return observed
    config = instance.get("config")
    actual = config.get("context_length") if isinstance(config, dict) else None
    if type(actual) is not int or actual <= 0:
        raise ManagedModelError("managed loaded instance has no confirmed positive context_length", observed)
    observed["actual_context"] = actual
    if declared_context and actual < declared_context:
        raise ManagedModelError(f"managed loaded context {actual} is below declared per-request context {declared_context}; configure the intended model explicitly", observed)
    if estimated_tokens > actual:
        raise ManagedModelError(f"request budget exceeded: estimated {estimated_tokens} tokens > managed loaded context {actual}", observed)
    return observed


def check_managed_model(host, model, messages, context_per_request=0, *, phase="before", previous=None):
    """Check only managed endpoints; ordinary compatible servers are unchanged."""
    path = os.environ.get("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT")
    if not path and previous is None:
        return None
    trace = current_attempt()
    try:
        if phase not in ("before", "after"):
            raise ManagedModelError("invalid managed model check phase")
        if not path:
            raise ManagedModelError("managed model receipt disappeared during generation")
        import lmstudio_runtime as runtime
        receipt_path = Path(path).resolve()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or receipt_path.name != runtime.RECEIPT
                or receipt_path.parent != Path(receipt["profile"]).resolve()):
            raise ManagedModelError("managed model receipt is not bound to its profile")
        expected_base, _ = runtime.endpoint(host)
        receipt_base, _ = runtime.endpoint(receipt["host"])
        if receipt_base != expected_base:
            raise ManagedModelError("managed model receipt does not belong to the requested endpoint")
        base = runtime._assert_live_receipt(receipt)
        if base != expected_base:
            raise ManagedModelError("managed live runtime does not match the requested endpoint")
        data = runtime._http_json(base, "/api/v1/models")
        result = inspect_model(data, model, estimate_messages(messages), context_per_request,
                               allow_unloaded=phase == "before")
        if phase == "after":
            if not isinstance(previous, dict) or previous.get("state") not in ("loaded", "unloaded"):
                raise ManagedModelError("managed reply has no matching pre-generation observation", result)
            observed = dict(result, before_model_key=previous["model_key"],
                            before_instance_id=previous["instance_id"],
                            before_actual_context=previous["actual_context"])
            if (result["requested_model"] != previous["requested_model"]
                    or result["model_key"] != previous["model_key"]):
                raise ManagedModelError("managed model identity changed during generation; reply is not accepted", observed)
            # An unloaded key may JIT-load one verified instance. An already
            # loaded instance must retain both its ID and its actual context.
            if previous["state"] == "loaded" and (
                    result["instance_id"] != previous["instance_id"]
                    or result["actual_context"] != previous["actual_context"]):
                raise ManagedModelError("managed loaded instance or context changed during generation; reply is not accepted", observed)
        if trace:
            trace.event("runtime-model", phase=phase, ok=True, **result)
        return result
    except Exception as exc:
        if trace:
            details = {"requested_model": model, "declared_context": context_per_request,
                       **getattr(exc, "observation", {})}
            trace.event("runtime-model", phase=phase, ok=False, reason=str(exc), **details)
        if isinstance(exc, ManagedModelError):
            raise
        raise ManagedModelError("managed model verification failed: " + str(exc)) from exc
