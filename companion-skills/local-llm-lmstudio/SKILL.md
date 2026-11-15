---
name: local-llm-lmstudio
description: >-
  Diagnose and operate a user's local LM Studio or compatible LLM endpoint.
  Use for unavailable servers, request/context failures, slow generation,
  parallelism or GPU memory contention. Preserve the selected model and
  unrelated processes; distinguish cached work from work needing an API call.
---

# Local model operation from observed evidence

Read the active project's instructions and settings. Identify the endpoint,
exact requested model, model instance when available, interpreter and processes
belonging to this job. Use the current project's runtime adapter; old probe or
translator scripts shipped beside this skill are not an authoritative engine.

## Establish whether the server is needed

In translate-pdf-latex, start with `scripts/pipeline.py status JOB` or
`scripts/diagnose_pipeline.py JOB`. These are read-only and do not call the model.
A fully cached translation or completed build can be revalidated offline. Do not
start or reload a model merely to examine progress or reuse accepted artifacts.

When an uncached request is needed, check the selected server's model catalogue
and current logs. An advertised maximum context is not proof of the loaded
instance's context. If the endpoint is managed by the project, use its receipt
and native model checks before/after generation; an absent or changed instance
must not silently accept a reply from another model.

For compatible local HTTP, bypass system proxies and retain the project's
loopback/redirect checks. Do not disable them to work around connection errors.
Use the user's configured URL; localhost versus 127.0.0.1 may matter during
diagnosis, but changing a saved endpoint is a configuration change. Credentials
belong in the supported environment variable, not saved commands or logs.

## Classify before retrying

| Evidence | Next step |
|---|---|
| Connection refused / server absent | Check the intended server, then resume; preserve caches |
| HTTP 400/401/403/404 | Inspect the response and configuration; retries do not repair a malformed request or missing authorization |
| Context refusal | Inspect the actual request plus output reserve and loaded context; do not infer capacity from VRAM alone |
| HTTP 408/429 or transient 5xx | Use bounded delays/retries; preserve successful requests |
| Truncated/refused response | Reject it and inspect output cap or model behavior; never declare it complete |
| Windows memory/commit error | Check process ownership, commit and available memory before changing inference settings |
| Disk/permission failure while saving | Resolve local storage; do not repeat the model call as network recovery |

An HTTP status alone does not prove memory exhaustion or a bad prompt. Record
which observation supports the cause. In this pipeline `pipeline_queue.py`
provides bounded orchestration; it is not an invitation to wrap `run` in an
unlimited shell loop. Never substitute another provider/model after failure.

## Tune only from measurements

For a comparable representative sample record context, generation settings,
parallel requests, prompt/output tokens, total elapsed time, accepted output and
retry rate. Compare useful throughput and correctness. Changing parallel from
1 to 3 does not establish a threefold gain. Keep per-request context explicit;
do not assume slots divide a shared context or each receives the advertised
maximum without runtime evidence. Token estimates are estimates, not a tokenizer.

Check the configured model template and supported reasoning controls before
changing them. A model-specific switch is not universal. Long outputs may be
reasoning, echoed instructions, repetition, or a legitimately long translation.
Use recorded usage/finish reasons to distinguish them. Small requests need a
reasonable output reserve, but a short cap is never permission to accept truncation.

GPU load and VRAM allocation are diagnostic signals, not progress counters.
Check appropriate compute metrics and system memory pressure. Use the project's
ownership-aware handoff for OCR/MT. Do not routinely run `lms unload --all`, kill
all Python processes or terminate another project's GPU work. Release only a
known job-owned resource within the user's authorized scope; otherwise coordinate.
Do not reintroduce historical HOLD, owner-reservation or cross-chat ACK rules
that the user/project has revoked.

For unattended operation use the project's supported background launcher and
read its status. Console lifetime, OS sleep and display state are separate from
model health. Do not change global power/runtime settings merely to inspect a job.
