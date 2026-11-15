---
name: latex-build-doctor
description: >-
  Diagnose LaTeX, XeLaTeX or MiKTeX build failures, stale or incomplete PDFs,
  missing fonts/glyphs, broken references and figure acceptance failures.
  Preserve completed outputs and valid build checkpoints; repair the earliest
  supported cause and verify the resulting candidate before publication.
---

# LaTeX build recovery without losing accepted work

Locate the actual build directory, command, engine log and release receipt.
Read project instructions first. In translate-pdf-latex use its existing
`scripts/build_release.py` and `scripts/pipeline.py`; do not replace these with
historical helper scripts or a direct shell loop over the finished book.

## Read the first useful error

The first fatal message usually explains later cascades. With file-line errors,
look for `file.tex:line: message` as well as `!`. Use the project's `tex_logs.py`
reader: logs may be UTF-8, BOM-marked or UTF-16. If TeX never started, inspect the
engine's stderr and OS exit code. A shared MiKTeX log must be matched to this
project and time; unrelated entries are not evidence for this failure.

A PDF existing, a plausible byte size, or `Output written` alone is not success.
A small PDF can also be perfectly valid. Check the process result, committed
build state, log, actual pages, references and mandatory release acceptance.

## Preserve checkpoints and distinguish fixes from retries

Do not delete `main.pdf`, `.aux`, `.toc`, caches or logs as a routine remedy.
An interrupted pass must not overwrite the last successful checkpoint. The
pipeline uses separate pass directories and verifies source/dependency hashes
before resuming. Completed builds are reusable only while inputs, PDF and log
still match their receipts. A changed font/package can require a new candidate.

For an unchanged interrupted candidate the standalone command is:

```text
python scripts/build_release.py CANDIDATE/main.tex --output CANDIDATE
```

Run it from the verified pipeline root with its interpreter. For a book job,
prefer resuming through `pipeline.py run JOB` so translation and release checks
remain connected. Never test repairs by writing into a finished user book.

Correct deterministic TeX/font/content problems before retrying. Repeating the
same failed candidate without changed inputs is not a repair. If a prepared
candidate is stale, make a new candidate and preserve the previous evidence;
do not edit state hashes to claim it is current. Outside this pipeline, use the
project's build tool with an isolated output directory and equivalent checks.

Read [failure-cases.md](references/failure-cases.md) for supported common causes.
Change only what the source or compiler evidence justifies. OCR-looking formula
symbols, equation tags, table cells and footnotes are content; compare them with
the source rather than deleting them to obtain a clean compilation.

## Acceptance

- Stop a pass containing `Missing character`; fix the font/math/mask cause.
- Let the build engine check aux/ToC stabilization with a bounded number of
  passes. Page count alone does not establish correct bookmark destinations.
- Inspect page count, bookmarks, figure presence and bounds, retained formulas,
  missing glyphs and unresolved references through the normal release checks.
- Figure placeholders can be useful in a draft. They do not substitute for
  required figures in a finished release.
- Distinguish small layout warnings from actual margin overflow. Inspect the
  affected pages when evidence indicates clipping; do not globally suppress logs.

Technical acceptance does not certify translation meaning or publication-level
typography. Optional deep visual/semantic editing should match the user's task.
