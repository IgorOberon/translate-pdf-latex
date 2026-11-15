---
name: local-mt-batch
description: >-
  Run and resume bulk document translation with a local MT model. Use for
  interrupted translation, rejected paragraphs, formula masking, glossary
  injection, repeated requests or excessive MT work. In translate-pdf-latex
  projects use the current pipeline engine and its validated fragment cache.
---

# Resumable local machine translation

Locate the user's active project and read its AGENTS.md and SKILL.md. In a
translate-pdf-latex project, use that project's interpreter and
`scripts/pipeline.py init`, `plan`, `run`, `status`. The project owns the engine.
Historical scripts beside an installed copy of this skill must not replace it.
Outside that project, identify the actual translator and its resume contract
before applying these principles; do not invent compatible flags or receipts.

## Preserve the source and request identity

Keep original books, accepted caches, maps and reports. Cleaning must record
source-bound decisions; an index, contents, footnote or CJK quotation is not
disposable merely because it complicates translation. Inspect suspicious OCR
against its source. Prose accidentally captured inside display math needs
extraction/masking repair before it can be considered translated.

Mask formulas, code, references and URLs; retain the exact map. Masking protects
identified spans, not OCR accuracy or text that masking missed. Check token ID,
count and order on every response and after assembly. A known bracket repair
must preserve the original ID and pass all other checks. Unresolved tokens,
missing notes, changed numbers or lost table cells prevent release.

Agree the glossary before a long run when requested. Do not change language,
model, endpoint, prompt, glossary or splitting settings just to obtain a cache
hit. A change may legitimately invalidate a request. Old cache entries remain
evidence; never rewrite their hashes to make them match.

## Resume at the smallest verified unit

The current engine stores accepted requests immediately in `fragments-v1`.
If a grouped response has exact paragraph IDs, order and nonempty boundaries,
independently valid paragraph bodies can also be retained. Only rejected bodies
need isolated translation. Ambiguous, missing or reordered boundaries are not
permission to guess an alignment or salvage text under the wrong ID.

Whole-group cache hits, fragment hits and previous-report fast paths all pass
the current validator. A stricter policy may reject an old result. Good earlier
requests survive failure or interruption of a later request. New alternatives
to a legacy group entry are append-only revisions; the old entry is preserved.
Identical concurrent requests use one producer within the process.

On failure, read the report rather than restarting the entire book in a loop:

| Evidence | Action |
|---|---|
| One bad paragraph, stable boundaries | Let the engine retain valid bodies and retry only the bad body |
| Interrupted request | Resume unchanged inputs from verified checkpoints |
| Permanent HTTP 4xx or insufficient context | Correct the documented cause before another run |
| Refused endpoint | Restore/check the endpoint; do not resubmit all remaining blocks |
| Disk write error | Resolve storage first; it is not a model-quality error |
| Same validation failure after bounded fallback | Preserve evidence and inspect that unit; do not accept the least-bad reply |

`translate.py` exit 4 means a partial candidate with failed blocks; exit 3
means aborted work whose new output was not published. Neither means a release.
`pipeline.py status` and the final release receipt determine technical completion.
Intentional unchanged passages require the existing explicit source-preservation
policy; a retry failure does not create such a policy.

For slow/repeated work, run the project's `scripts/diagnose_pipeline.py JOB`.
Read `docs/INCREMENTAL_MT.md` and `docs/INTEGRITY.md` in that project only when
repair/resume details are needed. Do not manually edit cache or coverage reports.

## Prompt size and measured throughput

Keep the specialized model's configured prompt template. Auxiliary glossary and
token-copy instructions belong where that template expects them; do not append
instructions to the payload. The current pipeline selects relevant terms and
critical entries while retaining the complete glossary for validation.

Measure useful accepted text, request latency, prompt/output tokens, retry share
and cache reuse on comparable samples. Busy VRAM, a process being alive, or a
growing log alone does not establish useful progress. Parallelism is a measured
setting, not a promised multiplier. Verify per-request context rather than
dividing an advertised model limit by a guessed slot count. Preserve the user's
chosen model and local-only/no-polish settings; no automatic cloud fallback.

Before release, verify source/target coverage, formulas, figures, notes and
failed/kept-source counts through the normal acceptance path. Script/language
heuristics and fluent samples do not prove semantic completeness. Semantic
editing and stylistic polish remain separately requested work.
