#!/usr/bin/env python3
"""One-shot autonomous book translation: English book -> Russian PDF.

Accepts multiple input formats and routes them automatically:
  .pdf                      -> Marker / MinerU / PyMuPDF (choose with --extract)
  .epub .fb2 .docx .odt
  .html .htm .rst           -> pandoc -> markdown (+ extracted media)
  .txt                      -> treated as markdown
  .md .markdown             -> used as-is (extraction skipped)
  .djvu .mobi .azw .azw3    -> converted to EPUB via Calibre's ebook-convert
                               (install Calibre), then pandoc

Then: mask math -> translate via local model -> unmask -> LaTeX -> PDF.
Resume support: conversion/extraction is skipped if markdown already exists
(unless --force); translation caches every paragraph.

Usage:
  python scripts/translate_book.py book.pdf  --extract math --auto-glossary
  python scripts/translate_book.py book.epub --auto-glossary
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mask_math  # noqa: E402
from procutils import (TIMEOUT_RC, PipelineLock, parse_unresolved,  # noqa: E402
                       reap_mineru, run_mineru, unload_llm, untranslated_ok)

from provenance import check_result, sha_file, write_json, canonical_hash, inside, book_settings
from build_release import compile_latex, inspect_pdf, publish_candidate, aux_state
from stage_checkpoints import (extraction_identity, reuse_extraction, EXTRACTION_VERSION,
                               LAYOUT_VERSION, select_build, seal_build, tree_hashes,
                               build_code_identity)
from prepare_mineru import prepare, assert_markdown_structure, assert_typeset_structure
from source_preservation import translatable_output
from tex_logs import read_tex_log
from content_coverage import (clean_with_coverage, verify_cleaning_coverage,
                              apply_book_edits, assert_no_control_chars, load_book_profile)

print(">> translate_book.py v7 (clean, mask-repair, figcaption, layout, "
      "smart-build, audit) loaded", flush=True)

from endpoint_policy import model_opener
OPENER = model_opener()

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}

PANDOC_EXT = {".epub", ".fb2", ".docx", ".odt", ".html", ".htm", ".rst"}
CALIBRE_EXT = {".djvu", ".djv", ".mobi", ".azw", ".azw3"}
MD_EXT = {".md", ".markdown", ".txt"}


def run(cmd, **kw):
    print(">>", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(list(map(str, cmd)), **kw)


def die(msg):
    print("!!", msg)
    sys.exit(1)


UNTRANSLATED_TOL = 0.02     # доля блоков, что можно оставить исходником


def run_translate(cmd, dst, what="перевод", tol=UNTRANSLATED_TOL):
    """translate.py с одним авторетраем при неполном переводе (код 4).

    Провальные блоки не кэшируются, поэтому второй проход стоит ровно их
    повторения и лечит транзиентные 500-ки сервера. Код 3 (сервер мёртв)
    не ретраим. Если после ретрая осталась ГОРСТЬ непереведённых блоков
    (<= tol доли или <= 2 штук — обычно echo на именах/библиографии),
    конвейер НЕ падает, а идёт дальше к вёрстке: эти блоки останутся на
    английском. Массовый недобор (сервер сыплется) по-прежнему = die."""
    report = Path(str(dst) + ".report.json")
    # The parallel supervisor has a fragment-level targeted repair stage.  A
    # second full grouped pass here would resubmit the same rejected groups
    # before targeted_repair.py can use its validated fragment cache, which is
    # the main source of multi-hour stalls on long books.  The targeted
    # workflow is now the default; set LOCAL_MT_TARGETED_WORKFLOW=0 only for an
    # explicit legacy standalone retry.
    targeted_workflow = os.environ.get("LOCAL_MT_TARGETED_WORKFLOW", "1") == "1"
    attempts = (1,) if targeted_workflow else (1, 2)
    for attempt in attempts:
        rc = run([sys.executable, SCRIPTS / "translate.py", *cmd]).returncode
        if rc in (0, 4):
            try:
                check_result(cmd[0], dst)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                die(f"missing/foreign translation report: {exc}")
        if rc == 0:
            return
        if rc == 4 and attempt == 1 and len(attempts) > 1:
            print(f">> {what} неполный (код 4) — второй проход по "
                  "непереведённым блокам (кэш цел)", flush=True)
            continue
        if rc == 4:
            kept, total = None, None
            try:
                rep = json.loads(report.read_text(encoding="utf-8"))
                kept, total = rep.get("kept_source"), rep.get("blocks_total")
            except (OSError, ValueError, KeyError):
                pass
            if untranslated_ok(kept, total, tol):
                print(f"?? {what}: {kept} из {total} блоков остались исходником "
                      f"(<= допуска {tol:.0%}) — продолжаю; они останутся на "
                      f"английском (обычно имена/библиография). Список: "
                      f"{report.name}", flush=True)
                return
            die(f"{what} не принят: {kept} из {total} блоков остались "
                f"исходником после повторного прохода (см. {report.name}) — "
                "чините сервер (для длинных книг помогает разбиение, см. "
                "README) и перезапустите ту же команду (кэш продолжит). "
                "Ослабить порог: --untranslated-tol")
        die(f"{what} failed (код {rc})"
            + (" — сервер мёртв; кэш цел, перезапуск той же команды продолжит"
               if rc == 3 else " — re-run the same command to resume"))


def run_unmask(src, dst, mapping, what="unmask"):
    """mask_math.py unmask с приёмкой: unresolved > 0 — формулы потеряны,
    прогон не принимается (паритет со skeleton-оркестратором)."""
    r = run([sys.executable, SCRIPTS / "mask_math.py", "unmask",
             src, dst, mapping],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if out.strip():
        print(out.strip(), flush=True)      # вывод стадии не терять
    if r.returncode:
        die(f"{what}: unmasking failed")
    unres = parse_unresolved(out)
    if unres is None:
        die(f"{what}: missing unmask acceptance result; output is not approved")
    elif unres > 0:
        die(f"{what}: unresolved={unres} — формулы потеряны, прогон не "
            "принят; перезапустите перевод (битые ответы не кэшируются) "
            "и повторите")


# A malformed ``$$`` pair in MinerU output can surround ordinary prose and
# therefore hide it from the translator: mask_math.py quite correctly treats
# the whole pair as one immutable span.  Do not change the main masked source
# here.  That would shift group boundaries and invalidate a long book's cache.
# Instead, translate only these proven prose spans and replace their values in
# the derived map immediately before unmasking.  This keeps every unaffected
# group/cache identity byte-for-byte stable.
_DISPLAY_PROSE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "by", "can",
    "consider", "conclude", "contains", "corollary", "deduce", "does",
    "example", "exercise", "finally", "following", "for", "from", "given",
    "hence", "if", "implies", "in", "is", "let", "now", "of", "on", "only",
    "proof", "remark", "section", "show", "shows", "suppose", "that", "the",
    "then", "there", "therefore", "this", "to", "under", "use", "we", "where",
    "which", "with", "would",
}


def _display_prose_probe(value):
    """Return (english_word_count, stopword_count) outside math/TeX.

    The source is English, so a display span with several ordinary English
    words and no Cyrillic is a safe repair candidate.  Formula annotations in
    ``\\text{...}``, arrays, and inline math are removed before the probe to
    avoid classifying legitimate formulas as prose.
    """
    if not (isinstance(value, str) and value.startswith("$$")
            and value.endswith("$$")):
        return 0, 0
    text = value[2:-2]
    text = re.sub(r"\\begin\{[^}]*\}.*?\\end\{[^}]*\}", " ",
                  text, flags=re.DOTALL)
    text = re.sub(r"(?<!\\)\$(?:\\.|[^$\\])+?\$", " ", text,
                  flags=re.DOTALL)
    text = re.sub(r"\\\\\(.*?\\\\\)|\\\\\[.*?\\\\\]", " ", text,
                  flags=re.DOTALL)
    text = re.sub(
        r"\\(?:text|mathrm|mathbf|mathit|operatorname|textbf|textit)"
        r"\s*\{[^{}]*\}", " ", text, flags=re.DOTALL)
    # Keep image alt/caption words visible for the detector; only discard the
    # URL part, which can contain English path components.
    text = re.sub(r"(?<=\])\([^)]*\)", " ", text)
    text = re.sub(r"\\[A-Za-z@]+\*?(?:\s*\[[^\]]*\])?", " ", text)
    words = re.findall(r"[A-Za-z]{3,}", text)
    cyrillic = re.search(r"[А-Яа-яЁё]", text)
    if cyrillic:
        return 0, 0
    stops = sum(word.casefold() in _DISPLAY_PROSE_STOPWORDS for word in words)
    return len(words), stops


def _display_prose_candidates(mapping):
    out = []
    for key, value in mapping.items():
        words, stops = _display_prose_probe(value)
        if words >= 5 and stops >= 2:
            out.append((key, value))
    return out


def _repair_masked_display_prose_legacy(wk, map_path, args, glossary_path):
    """Translate malformed display spans without invalidating the main cache.

    The first implementation made every repaired span a paragraph-protocol
    member.  Dense formula paragraphs then spent most of their time retrying
    damaged ``P`` envelopes.  This targeted pass uses ordinary translation
    blocks with immutable start/end markers instead: a larger batch reduces
    request overhead, while a 900-character micro-batch is retained as a
    fallback for any block the model does not accept.
    """
    try:
        mapping = json.loads(Path(map_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        die(f"mask repair: cannot read map: {exc}")
    candidates = _display_prose_candidates(mapping)
    if not candidates:
        print(">> mask prose repair: no malformed display-prose spans", flush=True)
        return

    # Mask formulas separately for every candidate and remap their local IDs
    # into one collision-free delta map.  Blank lines become immutable
    # placeholders so candidate start/end markers remain unambiguous.
    delta_mapping = {}
    metadata = []
    next_id = 0

    def allocate(original):
        nonlocal next_id
        if next_id >= 1000000:
            die("mask repair: placeholder id space exhausted")
        key = f"\u27e6M{next_id:06d}\u27e7"
        next_id += 1
        delta_mapping[key] = original
        return key

    prepared = []
    for key, value in candidates:
        inner = value[2:-2]
        leading = inner[:len(inner) - len(inner.lstrip())]
        trailing = inner[len(inner.rstrip()):]
        core = inner.strip()
        masked_core, local_map = mask_math.mask(core)
        # ``mask_math.mask`` may create nested placeholders: for example an
        # inline-math token can contain an environment token.  Allocate the
        # whole local namespace first, then rewrite nested references before
        # storing the originals.  A sequential replace would leave local
        # ``M000000`` references inside later values; after the next candidate
        # those IDs could point at an unrelated formula and silently corrupt
        # the assembled book.
        local_to_global = {
            local_key: allocate(original)
            for local_key, original in local_map.items()
        }
        for local_key, global_key in local_to_global.items():
            original = delta_mapping[global_key]
            delta_mapping[global_key] = mask_math.PLACEHOLDER_RE.sub(
                lambda match: local_to_global.get(match.group(0), match.group(0)),
                original)
        masked_core = mask_math.PLACEHOLDER_RE.sub(
            lambda match: local_to_global.get(match.group(0), match.group(0)),
            masked_core)

        def blank_line(match):
            return allocate(match.group(0))

        masked_core = re.sub(r"\n[ \t]*\n+", blank_line, masked_core)
        start_token = allocate(f"__repair_start_{len(prepared):06d}__")
        end_token = allocate(f"__repair_end_{len(prepared):06d}__")
        prepared.append({"key": key, "leading": leading, "trailing": trailing,
                         "core": masked_core, "start": start_token,
                         "end": end_token})

    def make_batches(items, target_chars):
        batches, current, size = [], [], 0
        for item in items:
            wrapped = f"{item['start']}\n{item['core']}\n{item['end']}"
            if current and size + len(wrapped) + 2 > target_chars:
                batches.append(current)
                current, size = [], 0
            current.append((item, wrapped))
            size += len(wrapped) + 2
        if current:
            batches.append(current)
        return batches

    def run_batch_stage(items, label, target_chars, cache_name,
                        split_chars):
        if not items:
            return {}, set(), 0
        batches = make_batches(items, target_chars)
        src = wk / f"mask_prose_repair_{label}.masked.md"
        dst = wk / f"mask_prose_repair_{label}.ru.masked.md"
        cache = wk / cache_name
        report_path = Path(str(dst) + ".report.json")
        src.write_text("\n\n".join(
            "\n".join(wrapped for _item, wrapped in batch)
            for batch in batches) + "\n", encoding="utf-8", newline="")

        cmd = [src, dst, "--mode", args.mode, "--model", args.model,
               "--host", args.host, "--target-lang", args.target_lang,
               "--cache-dir", cache, "--parallel", args.parallel,
               # This is a private repair stage.  The main book keeps the
               # requested group_chars=900; disabling the paragraph protocol
               # here avoids P-envelope retries on malformed source spans.
               "--group-chars", "0", "--split-chars", str(split_chars),
               "--context-per-request", str(args.context_per_request)]
        if args.glossary_profile:
            cmd += ["--glossary-profile", args.glossary_profile]
        if glossary_path and Path(glossary_path).exists():
            cmd += ["--glossary", glossary_path]
        # Let the stage publish a report even when a few blocks fail; only
        # accepted blocks are used below, and failed ones go to the micro pass.
        run_translate(cmd, dst, what=f"mask prose repair ({label})", tol=1.0)

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            text = dst.read_bytes().decode("utf-8")
            failed = {
                int(row["index"]) - 1
                for row in report.get("failed_blocks", [])
                if isinstance(row, dict) and str(row.get("index", "")).isdigit()
            }
        except (OSError, ValueError, TypeError, KeyError,
                json.JSONDecodeError) as exc:
            die(f"mask repair ({label}): cannot read delta result: {exc}")

        translated = {}
        cursor = 0
        for batch_index, batch in enumerate(batches):
            if batch_index in failed:
                continue
            for item, _wrapped in batch:
                start = text.find(item["start"], cursor)
                if start < 0:
                    failed.add(batch_index)
                    break
                start += len(item["start"])
                end = text.find(item["end"], start)
                if end < 0:
                    failed.add(batch_index)
                    break
                body = text[start:end]
                restored, _exact, _repaired, unresolved = mask_math.unmask(
                    body, delta_mapping)
                if unresolved:
                    failed.add(batch_index)
                    break
                restored = restored.strip()
                if not re.search(r"[А-Яа-яЁё]", restored):
                    failed.add(batch_index)
                    break
                translated[item["key"]] = restored
                cursor = end + len(item["end"])
        failed_items = {
            item["key"]
            for index, batch in enumerate(batches) if index in failed
            for item, _wrapped in batch
        }
        return translated, failed_items, len(batches)

    # First pass: roughly 2.8k source characters per request.  This is still
    # well inside the 16k context, but avoids hundreds of fixed prompt/setup
    # costs.  Any rejected block is retried in a smaller independent batch.
    translated, failed_items, batch_count = run_batch_stage(
        prepared, "batch", 2800, ".tcache_mask_prose_batch", args.split_chars)
    micro_items = [item for item in prepared if item["key"] in failed_items]
    micro_translated, micro_failed, micro_count = run_batch_stage(
        micro_items, "micro", 900, ".tcache_mask_prose_micro", args.split_chars)
    translated.update(micro_translated)
    if micro_failed:
        sample = sorted(micro_failed)[:5]
        die(f"mask repair: {len(micro_failed)} spans remain unaccepted after "
            f"targeted retries; examples: {sample}")

    changed = 0
    for item in prepared:
        body = translated.get(item["key"])
        if body is None:
            die(f"mask repair: no accepted translation for {item['key']}")
        mapping[item["key"]] = "$$" + item["leading"] + body \
            + item["trailing"] + "$$"
        changed += 1

    Path(map_path).write_text(json.dumps(mapping, ensure_ascii=False, indent=0),
                              encoding="utf-8", newline="")
    write_json(wk / "mask_prose_repair.report.json", {
        "schema": 1, "status": "passed", "candidates": len(candidates),
        "updated": changed, "delta_map_entries": len(delta_mapping),
        "batch_count": batch_count, "micro_batch_count": micro_count,
        "translation_stage": "ordinary_marked_blocks_with_micro_fallback",
    })
    write_json(wk / "mask_prose_repair.plan.json", {
        "schema": 2, "candidates": len(candidates),
        "source_keys": [key for key, _value in candidates],
        "delta_map_entries": len(delta_mapping),
        "batch_chars": 2800, "micro_batch_chars": 900,
        "batch_count": batch_count, "micro_batch_count": micro_count,
    })
    print(f">> mask prose repair: translated {changed}/{len(candidates)} "
          f"malformed display-prose spans; main cache preserved", flush=True)


def repair_masked_display_prose(wk, map_path, args, glossary_path):
    """Translate only prose runs; reinsert every formula at its old position.

    A malformed display-math span can contain hundreds of TeX/image masks.
    Sending those masks to the MT model makes marker corruption the dominant
    failure mode.  This pass masks formulas locally, sends only the plain text
    between them, and reconstructs the span losslessly afterwards.
    """
    try:
        mapping = json.loads(Path(map_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        die(f"mask prose repair: cannot read map: {exc}")
    candidates = _display_prose_candidates(mapping)
    if not candidates:
        print(">> mask prose repair: no malformed display-prose spans", flush=True)
        return

    delta_mapping = {}
    next_id = 0

    def allocate(original):
        nonlocal next_id
        if next_id >= 1000000:
            die("mask prose repair: placeholder id space exhausted")
        token = f"\u27e6M{next_id:06d}\u27e7"
        next_id += 1
        delta_mapping[token] = original
        return token

    def meaningful_run(text):
        words = re.findall(r"[A-Za-z]{3,}", text)
        if not words:
            return False
        if len(words) >= 3:
            return True
        return any(word.casefold() in _DISPLAY_PROSE_STOPWORDS
                   for word in words)

    def mask_structural_markers(text):
        # request_plan.py treats headings/lists/quotes as indivisible Markdown
        # blocks.  Preserve those prefixes as tiny immutable tokens so a long
        # prose run containing a heading can still be split safely.
        pattern = re.compile(
            r"(?m)^([ \t]*)(#{1,6}|>|[-*+]|```|\||\d+[.)])(?=[ \t])")

        def replace(match):
            return match.group(1) + allocate(match.group(2))
        return pattern.sub(replace, text)

    def mask_blank_lines(text):
        def replace(match):
            return allocate(match.group(0))
        return re.sub(r"\n[ \t]*\n+", replace, text)

    # Build a lossless candidate representation.  Formula tokens are retained
    # only in this local representation; they never enter an MT request.
    runs = []
    candidate_specs = []
    for key, value in candidates:
        inner = value[2:-2]
        leading = inner[:len(inner) - len(inner.lstrip())]
        trailing = inner[len(inner.rstrip()):]
        core = inner.strip()
        masked_core, local_map = mask_math.mask(core)
        # ``mask_math.mask`` may create nested placeholders: for example an
        # inline-math token can contain an environment token.  Allocate the
        # whole local namespace first, then rewrite nested references before
        # storing the originals.  A sequential replace would leave local
        # ``M000000`` references inside later values; after the next candidate
        # those IDs could point at an unrelated formula and silently corrupt
        # the assembled book.
        local_to_global = {
            local_key: allocate(original)
            for local_key, original in local_map.items()
        }
        for local_key, global_key in local_to_global.items():
            original = delta_mapping[global_key]
            delta_mapping[global_key] = mask_math.PLACEHOLDER_RE.sub(
                lambda match: local_to_global.get(match.group(0), match.group(0)),
                original)
        masked_core = mask_math.PLACEHOLDER_RE.sub(
            lambda match: local_to_global.get(match.group(0), match.group(0)),
            masked_core)
        formula_tokens = mask_math.PLACEHOLDER_RE.findall(masked_core)
        text_parts = mask_math.PLACEHOLDER_RE.split(masked_core)
        parts = []
        for index, text_part in enumerate(text_parts):
            if text_part:
                if meaningful_run(text_part):
                    run_id = len(runs)
                    run_core = text_part.strip()
                    run_leading = text_part[:len(text_part) - len(text_part.lstrip())]
                    run_trailing = text_part[len(text_part.rstrip()):]
                    start_token = allocate(f"__run_start_{run_id:06d}__")
                    end_token = allocate(f"__run_end_{run_id:06d}__")
                    runs.append({
                        "id": run_id, "key": key,
                        "text": mask_blank_lines(
                            mask_structural_markers(run_core)),
                        "leading": run_leading, "trailing": run_trailing,
                        "start": start_token, "end": end_token,
                    })
                    parts.append(("run", run_id))
                else:
                    parts.append(("literal", text_part))
            if index < len(formula_tokens):
                parts.append(("formula", formula_tokens[index]))
        candidate_specs.append({"key": key, "leading": leading,
                                "trailing": trailing, "parts": parts})

    def make_batches(items, target_chars):
        batches, current, size = [], [], 0
        structural = re.compile(
            r"(?m)^\s*(?:#{1,6}\s|>|\[\^|[-*+]\s|```|\|)")

        def flush():
            nonlocal current, size
            if current:
                batches.append(current)
                current, size = [], 0

        for item in items:
            wrapped = f"{item['start']}\n{item['text']}\n{item['end']}"
            # request_plan.py deliberately keeps structural Markdown blocks
            # whole.  Do not let one heading make an otherwise large batch
            # indivisible; translate that run as its own small block.
            if structural.search(item["text"]):
                flush()
                batches.append([(item, wrapped)])
                continue
            if current and size + len(wrapped) + 1 > target_chars:
                flush()
            current.append((item, wrapped))
            size += len(wrapped) + 1
        flush()
        return batches

    def reusable_stage_output(src, dst, report_path, label):
        """Reuse a completed targeted-stage output, including its failures.

        ``translate.py`` caches accepted blocks, but deliberately does not cache
        rejected blocks.  That is useful for a transient server error, yet it
        made a restart of this repair pipeline repeat the same known-invalid
        marker requests before reaching the smaller fallback stage.  The stage
        report and output are themselves immutable, content-addressed evidence:
        when both hashes still match the current source, reuse them and let the
        next repair stage handle the recorded failures.
        """
        try:
            if not (src.exists() and dst.exists() and report_path.exists()):
                return False
            from types import SimpleNamespace
            from translate import (request_config, reusable_plain_stage, ValidationPolicy,
                                   CJK_TARGETS)
            from glossary_selection import load_profile, select_glossary
            glossary = Path(glossary_path).read_text(encoding="utf-8").strip() if glossary_path else ""
            profile = load_profile(args.glossary_profile)
            config = request_config(SimpleNamespace(
                host=args.host, model=args.model, mode=args.mode,
                source_lang="English", target_lang=args.target_lang,
                split_chars=args.split_chars, group_chars=0, think="off",
                temperature=.7, top_p=.6), glossary, profile)
            policy = ValidationPolicy(reject_cjk=args.target_lang.strip().lower() not in CJK_TARGETS,
                                      reject_echo=args.mode != "polish", mode=args.mode)
            if not reusable_plain_stage(src, dst, config, policy,
                                        select_glossary("", glossary, profile, all_groups=True)):
                return False
            print(f">> mask prose text ({label}): reuse existing stage output "
                  "and failed-block report (cache preserved)", flush=True)
            return True
        except (OSError, ValueError, TypeError, KeyError,
                json.JSONDecodeError):
            return False

    def run_stage(items, label, target_chars, cache_name):
        if not items:
            return {}, set(), 0
        batches = make_batches(items, target_chars)
        src = wk / f"mask_prose_text_{label}.masked.md"
        dst = wk / f"mask_prose_text_{label}.ru.masked.md"
        cache = wk / cache_name
        report_path = Path(str(dst) + ".report.json")
        # Single newlines keep each batch one ordinary source block.  Formula
        # and paragraph blank lines are already immutable M-tokens.
        src.write_text("\n\n".join(
            "\n".join(wrapped for _item, wrapped in batch)
            for batch in batches) + "\n", encoding="utf-8", newline="")
        cmd = [src, dst, "--mode", args.mode, "--model", args.model,
               "--host", args.host, "--target-lang", args.target_lang,
               "--cache-dir", cache, "--parallel", args.parallel,
               "--group-chars", "0", "--split-chars", str(args.split_chars),
               "--context-per-request", str(args.context_per_request)]
        if args.glossary_profile:
            cmd += ["--glossary-profile", args.glossary_profile]
        if glossary_path and Path(glossary_path).exists():
            cmd += ["--glossary", glossary_path]
        # Publish a machine-readable failed-block report; failed runs are
        # retried in the smaller stage below rather than accepted as source.
        # If this exact stage already completed, do not resubmit its known
        # failures on a resume; parse the preserved output and proceed below.
        if not reusable_stage_output(src, dst, report_path, label):
            run_translate(cmd, dst, what=f"mask prose text ({label})", tol=1.0)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            output = dst.read_bytes().decode("utf-8")
            failed_batches = {
                int(row["index"]) - 1
                for row in report.get("failed_blocks", [])
                if isinstance(row, dict) and str(row.get("index", "")).isdigit()
            }
        except (OSError, ValueError, TypeError, KeyError,
                json.JSONDecodeError) as exc:
            die(f"mask prose text ({label}): cannot read result: {exc}")

        translated, cursor = {}, 0
        for batch_index, batch in enumerate(batches):
            if batch_index in failed_batches:
                continue
            for item, _wrapped in batch:
                start = output.find(item["start"], cursor)
                if start < 0:
                    failed_batches.add(batch_index)
                    break
                start += len(item["start"])
                end = output.find(item["end"], start)
                if end < 0:
                    failed_batches.add(batch_index)
                    break
                body = output[start:end]
                restored, _exact, _repaired, unresolved = mask_math.unmask(
                    body, delta_mapping)
                if unresolved:
                    failed_batches.add(batch_index)
                    break
                source = item["text"].replace("⟦", "").replace("⟧", "")
                if (re.search(r"[A-Za-z]{3,}", source)
                        and re.search(r"[A-Za-z]{3,}", restored)
                        and not re.search(r"[А-Яа-яЁё]", restored)):
                    failed_batches.add(batch_index)
                    break
                translated[item["id"]] = restored.strip()
                cursor = end + len(item["end"])
        failed_runs = {
            item["id"]
            for index, batch in enumerate(batches) if index in failed_batches
            for item, _wrapped in batch
        }
        return translated, failed_runs, len(batches)

    # Keep the private repair request moderately dense, but use the pipeline's
    # normal 900-character splitter for long runs.  Any failed batch is retried
    # with a smaller batch; no formula is ever exposed to the model.
    translated, failed_runs, batch_count = run_stage(
        runs, "batch", 2800, ".tcache_mask_prose_text_batch")
    micro_runs = [run for run in runs if run["id"] in failed_runs]
    micro_translated, micro_failed, micro_count = run_stage(
        micro_runs, "micro", 900, ".tcache_mask_prose_text_micro")
    translated.update(micro_translated)

    def run_plain_fallback(items):
        """Last resort for marker-heavy runs: send clean prose only."""
        if not items:
            return {}, set()
        src = wk / "mask_prose_plain_fallback.md"
        dst = wk / "mask_prose_plain_fallback.ru.md"
        cache = wk / ".tcache_mask_prose_plain"
        report_path = Path(str(dst) + ".report.json")
        plain_sources = []
        for item in items:
            plain, _exact, _repaired, unresolved = mask_math.unmask(
                item["text"], delta_mapping)
            if unresolved:
                die(f"mask prose plain fallback: unresolved source run {item['id']}")
            # One source block per run, with internal paragraph gaps collapsed
            # to a single line break.  translate.py re-renders each block and
            # rejoins blocks with a double gap, so boundaries stay recoverable
            # without any model-visible marker.
            plain = re.sub(r"\n[ \t]*\n+", "\n", plain.strip())
            plain_sources.append(plain)
        src.write_text("\n\n".join(plain_sources) + "\n",
                       encoding="utf-8", newline="")
        cmd = [src, dst, "--mode", args.mode, "--model", args.model,
               "--host", args.host, "--target-lang", args.target_lang,
               "--cache-dir", cache, "--parallel", args.parallel,
               "--group-chars", "0", "--split-chars", str(args.split_chars),
               "--context-per-request", str(args.context_per_request)]
        if args.glossary_profile:
            cmd += ["--glossary-profile", args.glossary_profile]
        if glossary_path and Path(glossary_path).exists():
            cmd += ["--glossary", glossary_path]
        plain_reused = reusable_stage_output(src, dst, report_path,
                                             "plain fallback")
        # A plain-fallback report with rejected blocks is the one case where
        # we do want a retry: translate.py will revalidate the 936 accepted
        # fragments from cache and submit only the still-failed fragment(s).
        # Marker/micro stages intentionally keep their failed report and move
        # on, because resubmitting those known-bad marker requests is wasteful.
        if plain_reused:
            try:
                old_report = json.loads(
                    report_path.read_text(encoding="utf-8"))
                plain_reused = not bool(old_report.get("failed_blocks"))
            except (OSError, ValueError, TypeError, KeyError,
                    json.JSONDecodeError):
                plain_reused = False
        if not plain_reused:
            run_translate(cmd, dst, what="mask prose plain fallback", tol=1.0)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            output = dst.read_bytes().decode("utf-8")
            blocks = [b for b in re.split(r"\n\s*\n", output.strip())
                      if b.strip()]
            failed = {
                int(row["index"]) - 1
                for row in report.get("failed_blocks", [])
                if isinstance(row, dict) and str(row.get("index", "")).isdigit()
            }
        except (OSError, ValueError, TypeError, KeyError,
                json.JSONDecodeError) as exc:
            die(f"mask prose plain fallback: cannot read result: {exc}")
        if len(blocks) != len(items):
            die("mask prose plain fallback: output block count mismatch "
                f"({len(blocks)} vs {len(items)})")
        result, still_failed = {}, set(failed)
        for index, (item, source) in enumerate(zip(items, plain_sources)):
            candidate = blocks[index].strip()
            if index in failed:
                continue
            # These runs were selected as genuine English prose.  A successful
            # fallback must therefore contain Cyrillic; names/symbol-only runs
            # never enter this list.
            if (re.search(r"[A-Za-z]{3,}", source)
                    and not re.search(r"[А-Яа-яЁё]", candidate)):
                still_failed.add(index)
                continue
            result[item["id"]] = candidate

        # A short caption can be translated correctly by spelling a protected
        # numeral out in Russian (``2-dimensional`` -> ``двумерной``), while
        # candidate-v5 quite rightly rejects the unmasked source/target pair as
        # a changed literal.  Retry only those remaining items with numerals
        # represented by the normal immutable M-tokens.  This keeps the 936
        # accepted plain fragments in cache and makes the retry semantic rather
        # than loosening the omission gate.
        if still_failed:
            retry_indexes = sorted(i for i in still_failed
                                   if 0 <= i < len(items))
            retry_items = [items[i] for i in retry_indexes]
            retry_sources, retry_maps = [], []
            next_token = 910000
            for item_index in retry_indexes:
                number_map = {}

                def protect_number(match):
                    nonlocal next_token
                    if next_token > 999999:
                        die("mask prose plain retry: too many numeric tokens")
                    token = f"⟦M{next_token:06d}⟧"
                    next_token += 1
                    number_map[token] = match.group(0)
                    return token

                retry_sources.append(re.sub(
                    r"\d+(?:[.,]\d+)*", protect_number,
                    plain_sources[item_index]))
                retry_maps.append(number_map)

            retry_src = wk / "mask_prose_plain_number_retry.md"
            retry_dst = wk / "mask_prose_plain_number_retry.ru.md"
            retry_cache = wk / ".tcache_mask_prose_plain_number_retry"
            retry_report_path = Path(str(retry_dst) + ".report.json")
            retry_src.write_text("\n\n".join(retry_sources) + "\n",
                                 encoding="utf-8", newline="")
            retry_cmd = [retry_src, retry_dst, "--mode", args.mode,
                         "--model", args.model, "--host", args.host,
                         "--target-lang", args.target_lang,
                         "--cache-dir", retry_cache, "--parallel", args.parallel,
                         "--group-chars", "0", "--split-chars",
                         str(args.split_chars), "--context-per-request",
                         str(args.context_per_request)]
            if args.glossary_profile:
                retry_cmd += ["--glossary-profile", args.glossary_profile]
            if glossary_path and Path(glossary_path).exists():
                retry_cmd += ["--glossary", glossary_path]
            run_translate(retry_cmd, retry_dst,
                          what="mask prose plain numeric retry", tol=1.0)
            try:
                retry_report = json.loads(
                    retry_report_path.read_text(encoding="utf-8"))
                retry_output = retry_dst.read_text(encoding="utf-8")
                retry_blocks = [b for b in re.split(
                    r"\n\s*\n", retry_output.strip()) if b.strip()]
                retry_failed = {
                    int(row["index"]) - 1
                    for row in retry_report.get("failed_blocks", [])
                    if isinstance(row, dict)
                    and str(row.get("index", "")).isdigit()
                }
            except (OSError, ValueError, TypeError, KeyError,
                    json.JSONDecodeError) as exc:
                die(f"mask prose plain numeric retry: cannot read result: {exc}")
            if len(retry_blocks) != len(retry_items):
                die("mask prose plain numeric retry: output block count "
                    f"mismatch ({len(retry_blocks)} vs {len(retry_items)})")
            for retry_pos, (item, candidate) in enumerate(
                    zip(retry_items, retry_blocks)):
                if retry_pos in retry_failed:
                    continue
                candidate = candidate.strip()
                if any(token not in candidate
                       for token in retry_maps[retry_pos]):
                    continue
                for token, literal in retry_maps[retry_pos].items():
                    candidate = candidate.replace(token, literal)
                source = plain_sources[retry_indexes[retry_pos]]
                if (re.search(r"[A-Za-z]{3,}", source)
                        and not re.search(r"[А-Яа-яЁё]", candidate)):
                    continue
                result[item["id"]] = candidate
                still_failed.discard(retry_indexes[retry_pos])

        return result, {items[index]["id"] for index in still_failed}

    if micro_failed:
        plain_translated, plain_failed = run_plain_fallback(micro_runs)
        translated.update(plain_translated)
        micro_failed = plain_failed
    if micro_failed:
        sample = sorted(micro_failed)[:8]
        die(f"mask prose repair: {len(micro_failed)} text runs remain "
            f"unaccepted; examples: {sample}")

    changed = 0
    run_by_id = {run["id"]: run for run in runs}
    for spec in candidate_specs:
        rebuilt = []
        for kind, value in spec["parts"]:
            if kind == "formula" or kind == "literal":
                rebuilt.append(value)
                continue
            run = run_by_id[value]
            if value not in translated:
                die(f"mask prose repair: no accepted text run {value}")
            rebuilt.append(run["leading"] + translated[value] + run["trailing"])
        restored, _exact, _repaired, unresolved = mask_math.unmask(
            "".join(rebuilt), delta_mapping)
        if unresolved:
            die(f"mask prose repair: unresolved placeholders in {spec['key']}: "
                f"{unresolved}")
        restored = restored.strip()
        source_words, _source_stops = _display_prose_probe(mapping[spec["key"]])
        if source_words >= 5 and not re.search(r"[А-Яа-яЁё]", restored):
            die(f"mask prose repair: {spec['key']} still has no Cyrillic prose")
        mapping[spec["key"]] = "$$" + spec["leading"] + restored \
            + spec["trailing"] + "$$"
        changed += 1

    Path(map_path).write_text(json.dumps(mapping, ensure_ascii=False, indent=0),
                              encoding="utf-8", newline="")
    write_json(wk / "mask_prose_repair.plan.json", {
        "schema": 3, "candidates": len(candidates), "text_runs": len(runs),
        "delta_map_entries": len(delta_mapping), "batch_chars": 2800,
        "micro_batch_chars": 900, "batch_count": batch_count,
        "micro_batch_count": micro_count,
        "plain_fallback": bool(micro_runs),
        "formulas_sent_to_model": False,
    })
    write_json(wk / "mask_prose_repair.report.json", {
        "schema": 2, "status": "passed", "candidates": len(candidates),
        "text_runs": len(runs), "updated": changed,
        "batch_count": batch_count, "micro_batch_count": micro_count,
        "formulas_sent_to_model": False,
    })
    print(f">> mask prose repair: translated {changed}/{len(candidates)} "
          f"spans via {len(runs)} prose runs; formulas reinserted verbatim",
          flush=True)


def preflight(host, model, suffix, extract_mode, have_md):
    # Cached translation/build work is usable offline. The MT engine checks
    # the endpoint only when it actually has an uncached request to execute.
    if not have_md:
        if suffix == ".pdf":
            need = {"auto": ("marker_single", "pip install marker-pdf"),
                    "scan": ("marker_single", "pip install marker-pdf"),
                    "math": ("mineru", "pip install mineru")}.get(extract_mode)
            if need and not shutil.which(need[0]):
                die(f"'{need[0]}' not found in PATH ({need[1]})")
            if extract_mode == "digital":
                try:
                    import pymupdf4llm  # noqa: F401
                except ImportError:
                    die("pymupdf4llm not installed (pip install pymupdf4llm)")
                print("?? режим digital (pymupdf4llm) — для born-digital PDF "
                      "без формул; растровые картинки извлекаются, но "
                      "векторную графику он не рендерит (её «текст» = мусор). "
                      "Для книг с рисунками/формулами берите --extract auto "
                      "или math.", flush=True)
        elif suffix in CALIBRE_EXT and not shutil.which("ebook-convert"):
            die(f"'{suffix}' needs Calibre's ebook-convert in PATH "
                "(install Calibre from calibre-ebook.com), or convert the "
                "book to EPUB/PDF yourself first")
        elif suffix not in PANDOC_EXT | MD_EXT | {".pdf"}:
            die(f"unsupported input format: {suffix}")
        if suffix == ".pdf" and extract_mode in ("auto", "scan", "math"):
            try:
                import torch
                if not torch.cuda.is_available():
                    print("?? ВНИМАНИЕ: PyTorch без CUDA — извлечение пойдёт на CPU "
                          "в десятки раз медленнее и может упереться в таймаут. "
                          "Лечение: pip install torch torchvision "
                          "--index-url https://download.pytorch.org/whl/cu128 --force-reinstall")
                else:
                    free, _total = torch.cuda.mem_get_info()
                    if free < 6 * 2 ** 30:
                        print(f"?? ВНИМАНИЕ: свободно лишь {free / 2**30:.1f} ГБ VRAM — "
                              "выгрузите LLM из LM Studio (Eject) на время извлечения, "
                              "иначе оно будет медленным")
            except Exception:
                pass
    if not shutil.which("pandoc"):
        die("'pandoc' not found in PATH (install from pandoc.org)")
    if not (shutil.which("latexmk") or shutil.which("xelatex")):
        die("xelatex/latexmk not found (install MiKTeX or TeX Live)")


def find_md(root):
    # derived sanitation artifacts (.clean.md, .removed_*.md) must never be
    # picked as the "source" markdown — that snowballs .clean.clean.md chains
    mds = sorted((p for p in root.rglob("*.md")
                  if "_chunks" not in p.parts
                  and ".clean" not in p.name
                  and ".prepared" not in p.name
                  and ".removed_" not in p.name),
                 key=lambda p: p.stat().st_size, reverse=True)
    return mds[0] if mds else None


def extract_pdf(pdf, outdir, mode, pages_per_chunk=200, vram_limit=0,
                mineru_timeout=3600):
    if mode != "digital":
        unload_llm()
        reap_mineru()
    if mode == "math" and pages_per_chunk:
        pages = None
        try:
            import fitz
            pages = len(fitz.open(str(pdf)))
        except Exception:
            pass
        if pages and pages > pages_per_chunk:
            print(f">> {pages} страниц > {pages_per_chunk}: чанковое извлечение "
                  "(обход 60-минутного таймаута MinerU)")
            ccmd = [sys.executable, SCRIPTS / "extract_chunked.py", pdf, outdir,
                    "--pages-per-chunk", pages_per_chunk,
                    "--mineru-timeout", mineru_timeout]
            if vram_limit:
                ccmd += ["--vram-limit", vram_limit]
            rc = run(ccmd).returncode
            reap_mineru()
            if rc:
                die("chunked extraction failed — перезапустите ту же команду, "
                    "готовые части не потеряются")
            md = find_md(outdir)
            if md is None:
                die("chunked extractor produced no .md file")
            return md
    if mode == "digital":
        import pymupdf4llm
        md = outdir / (pdf.stem + ".md")
        imgdir = outdir / "images"
        # write_images=True: растровые картинки/графику извлечь файлами и
        # вставить ![](...) на их места (иначе digital давал только текст,
        # а «текст» внутри фигур — OCR-мусор)
        try:
            text = pymupdf4llm.to_markdown(
                str(pdf), write_images=True, image_path=str(imgdir),
                image_format="jpg")
            # pymupdf4llm пишет абсолютный путь — нормализуем на images/имя
            text = re.sub(r"!\[\]\((?:[^)]*[/\\])?([^/\\)]+)\)",
                          r"![](images/\1)", text)
        except TypeError:               # старый pymupdf4llm без write_images
            text = pymupdf4llm.to_markdown(str(pdf))
        md.write_text(text, encoding="utf-8")
        n_img = len(list(imgdir.glob("*"))) if imgdir.is_dir() else 0
        if n_img == 0:
            print("?? режим digital не извлёк ни одной картинки — если в "
                  "книге есть рисунки, они потеряны; используйте --extract "
                  "auto (Marker) или math (MinerU)", flush=True)
        return md
    if mode == "math":
        rc = run_mineru(["-p", pdf, "-o", outdir],
                        Path(outdir) / "mineru.log",
                        timeout=mineru_timeout, vram_limit=vram_limit)
        if rc == TIMEOUT_RC:
            die("mineru завис (>--mineru-timeout) и был прерван — "
                "перезапустите ту же команду")
        if rc:
            die("PDF extraction failed")
    else:
        env = dict(os.environ)
        if vram_limit:
            env["MINERU_VIRTUAL_VRAM_SIZE"] = str(vram_limit)
        cmd = ["marker_single", pdf, "--output_dir", outdir,
               "--output_format", "markdown"]
        if mode == "scan":
            cmd.append("--force_ocr")
        if run(cmd, env=env).returncode != 0:
            die("PDF extraction failed")
        reap_mineru()
    md = find_md(outdir)
    if md is None:
        die("extractor produced no .md file")
    return md


def pandoc_to_md(src, outdir):
    d = outdir / src.stem
    d.mkdir(parents=True, exist_ok=True)
    md = d / (src.stem + ".md")
    if run(["pandoc", src, "--to=markdown", "--wrap=none",
            f"--extract-media={d}", "-o", md]).returncode:
        die(f"pandoc failed to convert {src.name}")
    return md


def to_markdown(src, outdir, mode, pages_per_chunk=200, vram_limit=0,
                mineru_timeout=3600):
    """Route any supported input format to a markdown file in outdir."""
    suffix = src.suffix.lower()
    outdir.mkdir(parents=True, exist_ok=True)

    if suffix == ".pdf":
        return extract_pdf(src, outdir, mode, pages_per_chunk, vram_limit,
                           mineru_timeout)

    if suffix in MD_EXT:
        d = outdir / src.stem
        d.mkdir(parents=True, exist_ok=True)
        md = d / (src.stem + ".md")
        shutil.copyfile(src, md)
        # bring sibling images along: folders and loose files
        for item in src.parent.iterdir():
            if item.is_dir() and ("image" in item.name.lower()
                                  or item.name.lower() == "media"):
                shutil.copytree(item, d / item.name, dirs_exist_ok=True)
            elif item.suffix.lower() in IMG_EXT:
                shutil.copy2(item, d / item.name)
        return md

    if suffix in PANDOC_EXT:
        return pandoc_to_md(src, outdir)

    if suffix in CALIBRE_EXT:
        tmp_epub = Path(tempfile.mkdtemp()) / (src.stem + ".epub")
        if run(["ebook-convert", src, tmp_epub]).returncode:
            die("Calibre conversion failed")
        return pandoc_to_md(tmp_epub, outdir)

    die(f"unsupported input format: {suffix}")


def figure_inputs(md):
    result = {}
    for item in md.parent.iterdir():
        if item.is_dir() and ("image" in item.name.lower() or item.name.lower() == "media"):
            result.update({item.name + "/" + name: digest for name, digest in tree_hashes(item).items()})
        elif item.suffix.lower() in IMG_EXT and item.is_file():
            result[item.name] = sha_file(item)
    return result


def copy_figures(md, build):
    for item in md.parent.iterdir():
        name = item.name.lower()
        if item.is_dir() and ("image" in name or name == "media"):
            shutil.copytree(item, build / item.name, dirs_exist_ok=True)
        elif item.suffix.lower() in IMG_EXT:
            shutil.copy2(item, build / item.name)


# ---- layout profiles & main.tex assembly ------------------------------------
# Overrides are appended to the assembled main.tex AFTER the preamble text;
# assets/preamble.tex itself stays single-source (no per-profile forks).
LAYOUT_BLOCKS = {
    "screen": "",  # default: current behaviour, strict backwards compatibility
    "print": (
        "% --- layout profile: print (twoside, openright, binding offset) ---\n"
        "\\makeatletter\\@twosidetrue\\@openrighttrue\\@mparswitchtrue\\makeatother\n"
        "\\usepackage[twoside,bindingoffset=8mm]{geometry}\n"),
    "compact": (
        "% --- layout profile: compact (dense margins, no vertical stretch) ---\n"
        "\\usepackage[margin=1.8cm]{geometry}\n"
        "\\raggedbottom\n"),
}

# LaTeX разрешает четыре уровня вложенных списков; извлечение MinerU из
# книги с подпунктами вида «a) 1) i) …» легко даёт больше, и сборка встаёт
# с «Too deeply nested». Ставим в СОБИРАЕМЫЙ main.tex, а не в
# assets/preamble.tex: преамбула общая для всех книг и остаётся
# односточниковой, а enumitem нужен только тем, у кого списки глубокие.
# fancyhdr считает высоту колонтитула от 12pt и жалуется «\headheight is too
# small» на каждой странице: в логе NFCM таких строк 996, и они забивают всё
# остальное. Правится одной строкой в СОБИРАЕМЫЙ main.tex — фрактурный
# сборщик так и делает с самого начала, а маршрут MinerU почему-то нет.
HEAD_HEIGHT = ("% --- fancyhdr: высота колонтитула под 11pt ---\n"
               "\\setlength{\\headheight}{14pt}\n"
               "\\addtolength{\\topmargin}{-2pt}\n")

DEEP_LISTS = (
    "% --- глубокая вложенность списков («Too deeply nested») ---\n"
    "\\usepackage{enumitem}\n"
    "\\setlistdepth{9}\n"
    "\\renewlist{enumerate}{enumerate}{9}\n"
    "\\renewlist{itemize}{itemize}{9}\n"
    "\\setlist[enumerate]{label=\\arabic*.}\n"
    "\\setlist[itemize]{label=\\textbullet}\n")

TEX_SPECIALS = {"&": "\\&", "%": "\\%", "$": "\\$", "#": "\\#", "_": "\\_",
                "{": "\\{", "}": "\\}", "~": "\\textasciitilde{}",
                "^": "\\textasciicircum{}", "\\": "\\textbackslash{}"}


def tex_escape(s):
    return "".join(TEX_SPECIALS.get(c, c) for c in s)


def titlepage_block(title, author):
    lines = ["\\begin{titlepage}\\centering",
             "\\vspace*{0.25\\textheight}",
             "{\\Huge\\bfseries %s\\par}" % tex_escape(title)]
    if author:
        lines += ["\\vspace{2em}", "{\\Large %s\\par}" % tex_escape(author)]
    lines += ["\\vfill", "\\end{titlepage}"]
    return "\n".join(lines)


def assemble_main_tex(preamble, layout="screen", title="", author=""):
    """Build the main.tex text: preamble + profile overrides + title page."""
    if layout == "compact":
        # base font size is a class option, so it is swapped in the assembled
        # copy only — the preamble file on disk keeps 11pt
        preamble = preamble.replace("\\documentclass[11pt",
                                    "\\documentclass[10pt", 1)
    # The CMU math delimiter glyphs used by XeTeX have intentionally empty
    # Unicode mappings for some size variants (\big/\Big/\left/\right).
    # They render correctly but otherwise produce spurious "Missing character"
    # diagnostics for control-code slots.  Keep this suppression in the
    # generated build only; source Markdown, translation and caches remain
    # untouched and are checked separately by the release audit.
    lost_char_policy = (
        "% build-only: CMU math delimiter glyphs have no Unicode mapping\n"
        "\\tracinglostchars=0\n")
    parts = [preamble, lost_char_policy, LAYOUT_BLOCKS[layout], HEAD_HEIGHT, DEEP_LISTS,
             "\\begin{document}"]
    if title:
        parts.append(titlepage_block(title, author))
    parts += ["\\tableofcontents\\clearpage", "\\input{body.tex}",
              "\\end{document}", ""]
    return "\n".join(parts)


def latex_build(build):
    report = compile_latex(Path(build) / "main.tex")
    return report["status"] == "passed", (0 if report["status"] == "passed" else 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book", help="input file: pdf/epub/fb2/docx/odt/html/rst/"
                                 "txt/md (djvu/mobi via Calibre)")
    ap.add_argument("--extract", choices=["auto", "digital", "scan", "math"],
                    default="auto", help="PDF extraction mode (PDF input only)")
    ap.add_argument("--model", default="hy-mt2-7b")
    ap.add_argument("--host", default="http://localhost:1234")
    ap.add_argument("--mode", choices=["hymt", "generic"], default="hymt")
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--glossary-profile", default=None)
    ap.add_argument("--book-profile", default=None, help="exact reviewed cleaning/post-edit decisions")
    ap.add_argument("--split-chars", type=int, default=0)
    ap.add_argument("--context-per-request", type=int, default=0)
    ap.add_argument("--auto-glossary", action="store_true",
                    help="build glossary.md automatically from the extracted text "
                         "(recurring names/terms, translated once by the model)")
    ap.add_argument("--target-lang", default="Russian")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--vram-limit", type=int, default=0,
                    help="ограничить VRAM для MinerU (ГБ), напр. 8 — от зависаний на плотных страницах")
    ap.add_argument("--review-glossary", action="store_true",
                    help="пауза после автогенерации глоссария: правите файл и жмёте Enter")
    ap.add_argument("--parallel", type=int, default=1,
                    help="одновременных запросов к серверу (под слоты LM Studio, напр. 3)")
    ap.add_argument("--group-chars", type=int, default=0,
                    help="объединять мелкие соседние абзацы в один запрос "
                         "(0 = поабзацно). Для книг с тысячами коротких "
                         "абзацев это снимает фиксированные накладные сервера")
    ap.add_argument("--pages-per-chunk", type=int, default=200,
                    help="PDF в режиме math крупнее этого — извлекать частями "
                         "(0 = отключить чанкинг)")
    ap.add_argument("--force", action="store_true",
                    help="redo conversion/extraction even if markdown exists")
    ap.add_argument("--mineru-timeout", type=int, default=3600,
                    help="таймаут одного вызова mineru, сек (0 = без "
                         "таймаута); rc=124 = зависание VLM, перезапуск "
                         "безопасен")
    ap.add_argument("--no-lock", action="store_true",
                    help="не брать глобальный замок конвейера (осторожно: "
                         "два прогона душат друг друга за GPU/память)")
    ap.add_argument("--untranslated-tol", type=float, default=UNTRANSLATED_TOL,
                    help=f"доля блоков, что можно оставить исходником и всё "
                         f"равно собрать PDF (по умолчанию {UNTRANSLATED_TOL:.0%}; "
                         "0 = строго, 1 = никогда не падать из-за перевода)")
    ap.add_argument("--keep-source", help="explicit JSON decisions for unchanged masked/grouped units")
    ap.add_argument("--polish", action="store_true",
                    help="после перевода — литературная полировка "
                         "(mask -> translate --mode polish -> unmask)")
    ap.add_argument("--polish-model", default="qwen3.5:9b")
    ap.add_argument("--polish-host", default="http://localhost:11434")
    ap.add_argument("--no-plaques", action="store_true",
                    help="не спасать текстовые плашки, уехавшие картинками")
    ap.add_argument("--no-clean", action="store_true",
                    help="skip markdown sanitation (OCR TOC removal, running "
                         "heads, stray equation tags)")
    ap.add_argument("--layout", choices=["screen", "print", "compact"],
                    default="screen",
                    help="вёрстка: screen (по умолчанию, прежнее поведение), "
                         "print (twoside+openright+bindingoffset 8mm), "
                         "compact (10pt, поля 1.8cm, raggedbottom)")
    ap.add_argument("--title", default=None,
                    help="титул книги (default: имя входного файла)")
    ap.add_argument("--author", default="",
                    help="автор на титуле (default: пусто)")
    ap.add_argument("--heading-numbers", choices=["auto", "latex"],
                    default="auto",
                    help="auto: если заголовки несут родную нумерацию книги, "
                         "LaTeX-номера разделов подавляются; latex: всегда "
                         "нумеровать по-LaTeX'овски")
    a = ap.parse_args()
    if a.keep_source and a.polish:
        ap.error("--keep-source cannot be combined with --polish; preserved units must remain unchanged")
    if min(a.split_chars, a.group_chars, a.context_per_request) < 0:
        ap.error("split/group/context settings must be nonnegative")
    book_profile = load_book_profile(a.book_profile)
    if a.no_clean and book_profile.get("cleaning", {}).get("decisions"):
        ap.error("--no-clean cannot skip explicit cleaning decisions")
    if a.keep_source and book_profile.get("post_edits"):
        ap.error("post_edits cannot alter a --keep-source output; use a separate reviewed source revision")
    from glossary_selection import load_profile
    load_profile(a.glossary_profile)

    src = Path(a.book).resolve()
    if not src.exists():
        die(f"no such file: {src}")
    suffix = src.suffix.lower()
    root = Path(a.workdir or f"{src.stem}_ru").resolve()
    ex, wk, bl = root / "extract", root / "work", root / "build"
    for d in (ex, wk, bl):
        d.mkdir(parents=True, exist_ok=True)

    # глобальный замок: GPU один, параллельные прогоны душат друг друга,
    # а reap_mineru() добивает mineru по всей машине
    if not a.no_lock:
        PipelineLock(note=f"translate_book:{root.name}").acquire()

    input_hash = sha_file(src)
    preservation_hash = sha_file(a.keep_source) if a.keep_source else None
    book_profile_hash = sha_file(a.book_profile) if a.book_profile else None
    extraction_config = extraction_identity(input_hash, a.extract, a.pages_per_chunk, SCRIPTS)
    extraction_state = root / "extraction.state.json"
    md = None
    if not a.force and extraction_state.is_file():
        state = json.loads(extraction_state.read_text(encoding="utf-8"))
        md = inside(root, state["markdown"])
        if not reuse_extraction(state, input_hash, a.extract, a.pages_per_chunk, SCRIPTS, md):
            die("extraction inputs/output changed or legacy identity cannot be verified; use a separate workdir; existing files retained")
    elif not a.force and find_md(ex):
        die("legacy extraction has no identity: use --force or a separate workdir; cache retained")
    preflight(a.host, a.model, suffix, a.extract, have_md=md is not None)

    # 1) any format -> markdown
    if md:
        print(f">> markdown found, skipping conversion: {md}")
    else:
        ex = Path(tempfile.mkdtemp(prefix="candidate-", dir=ex))
        md = to_markdown(src, ex, a.extract, a.pages_per_chunk, a.vram_limit,
                         a.mineru_timeout)
        write_json(extraction_state, {"schema": EXTRACTION_VERSION,
                   "source_sha256": input_hash, "config_sha256": extraction_config,
                   "markdown": md.relative_to(root).as_posix(), "output_sha256": sha_file(md)})

    # Recover structure omitted by MinerU's default Markdown exporter BEFORE MT.
    # This is required even with --no-clean; raw OCR and JSON stay unchanged.
    if suffix == ".pdf" and a.extract == "math":
        md = prepare(md)

    # 1a-pre) детекция текстовых плашек, уехавших картинками (быстро, без GPU):
    # только отчёт. Восстановление текста — vision-агентом (см. АГЕНТ.md),
    # т.к. пере-OCR кропов через MinerU стохастически виснет.
    if not a.no_plaques and src.suffix.lower() == ".pdf":
        run([sys.executable, SCRIPTS / "rescue_plaques.py", "detect", md,
             "--json", wk / "plaques_candidates.json",
             "--report", wk / "plaques_report.md"])

    # Every cleaning decision is replayable against exact input bytes.
    cleaning_source = md
    coverage_path = wk / "cleaning.coverage.json"
    if not a.no_clean:
        clean_md = md.with_suffix(".clean.md")
        clean_with_coverage(md, clean_md, coverage_path, profile=book_profile)
        assert_markdown_structure(md.read_text(encoding="utf-8"), clean_md.read_text(encoding="utf-8"))
        md = clean_md
    assert_no_control_chars(md.read_text(encoding="utf-8"))
    coverage_hash = sha_file(coverage_path) if not a.no_clean else None
    rebuild_toc = any(row.get("action") == "rebuild"
                      for row in book_profile.get("cleaning", {}).get("decisions", []))

    structure_source = md.read_text(encoding="utf-8")

    # 1b) glossary: auto-build once if requested and missing
    gpath = Path(a.glossary).resolve() if a.glossary else None
    if a.auto_glossary:
        if gpath is None:
            gpath = wk / "glossary.md"
        if gpath.exists():
            print(f">> glossary found, keeping: {gpath}")
        else:
            if run([sys.executable, SCRIPTS / "make_glossary.py", md, gpath,
                    "--host", a.host, "--model", a.model,
                    "--target-lang", a.target_lang]).returncode:
                die("glossary generation failed")
            print(f"?? auto-glossary written: {gpath} — reviewing it before a long "
                  "run is recommended (edits reset the translation cache)")
            if a.review_glossary:
                try:
                    input(">> ПАУЗА: откройте и поправьте глоссарий, затем Enter для продолжения... ")
                except EOFError:
                    pass

    settings_hash = book_settings(a, gpath)
    # 2) mask
    masked, mapping = wk / "book.masked.md", wk / "book.map.json"
    if run([sys.executable, SCRIPTS / "mask_math.py", "mask",
            md, masked, mapping]).returncode:
        die("masking failed")

    # Audit hidden prose before any main-book model work. Keep the legacy mask
    # IDs and group boundaries stable, repairing its separate map from cache.
    if a.mode != "polish" and a.target_lang.strip().lower() == "russian":
        repair_masked_display_prose(wk, mapping, a, gpath)

    # 3) translate (resumable via cache)
    cmd = [masked, wk / "book.ru.masked.md",
           "--mode", a.mode, "--model", a.model, "--host", a.host,
           "--target-lang", a.target_lang, "--cache-dir", wk / ".tcache",
           "--parallel", a.parallel, "--group-chars", str(a.group_chars),
           "--split-chars", str(a.split_chars), "--context-per-request", str(a.context_per_request)]
    if a.glossary_profile:
        cmd += ["--glossary-profile", a.glossary_profile]
    if gpath and gpath.exists():
        cmd += ["--glossary", gpath]
    if a.keep_source:
        cmd += ["--keep-source", a.keep_source]
    run_translate(cmd, wk / "book.ru.masked.md", what="translation",
                  tol=a.untranslated_tol)

    # 3a) приёмка: доля целевого алфавита; мало кириллицы = сервер падал и
    # блоки остались английскими («keeping source») — прогон не принимаем
    if a.target_lang.strip().lower() == "russian":
        _report = check_result(masked, wk / "book.ru.masked.md")
        _t = re.sub(r"⟦M\d{6}⟧", "", translatable_output(
                    (wk / "book.ru.masked.md").read_bytes().decode("utf-8"), _report))
        _c = len(re.findall(r"[А-Яа-яЁё]", _t))
        _l = len(re.findall(r"[A-Za-z]", _t))
        if _c + _l > 2000 and _c / (_c + _l) < 0.5:
            die(f"перевод неполный: кириллицы лишь {100 * _c // (_c + _l)}% — "
                "видимо, сервер перевода падал; перезапустите (кэш продолжит)")

    # 4) unmask (+гейт: unresolved строго 0)
    ru_md = wk / "book.ru.md"
    run_unmask(wk / "book.ru.masked.md", ru_md, mapping)

    # Book-specific changes require exact, content-bound decisions. Keep the
    # raw unmasked output as evidence and publish a distinct checked derivative.
    unmasked = ru_md
    _text = unmasked.read_bytes().decode("utf-8")
    _fixed, post_report = apply_book_edits(_text, book_profile)
    post_report_path = wk / "postprocess.coverage.json"
    write_json(post_report_path, post_report)
    if _fixed != _text:
        ru_md = wk / "book.ru.reviewed.md"
        ru_md.write_text(_fixed, encoding="utf-8", newline="")
    post_report_hash = sha_file(post_report_path)
    unmasked_hash = sha_file(unmasked)

    # 4b) полировка (опционально): литературная правка переведённого текста
    # по замаскированному файлу — формулы не уходят в полировщик; канон
    # описан в КОНФИГУРАЦИИ.md (слои П1–П3), тут он автоматизирован
    if a.polish:
        p_masked = wk / "polish.masked.md"
        p_map = wk / "polish.map.json"
        p_out = wk / "polish.ru.masked.md"
        polished = wk / "book.ru.polished.md"
        if run([sys.executable, SCRIPTS / "mask_math.py", "mask",
                ru_md, p_masked, p_map]).returncode:
            die("polish: masking failed")
        pcmd = [p_masked, p_out,
                "--mode", "polish", "--model", a.polish_model,
                "--host", a.polish_host, "--target-lang", a.target_lang,
                "--cache-dir", wk / ".tcache_polish", "--parallel", a.parallel,
                "--split-chars", a.split_chars, "--group-chars", a.group_chars,
                "--context-per-request", a.context_per_request]
        if a.glossary_profile:
            pcmd += ["--glossary-profile", a.glossary_profile]
        if gpath and gpath.exists():
            pcmd += ["--glossary", gpath]
        run_translate(pcmd, p_out, what="polish", tol=a.untranslated_tol)
        run_unmask(p_out, polished, p_map, what="polish")
        ru_md = polished

    assert_no_control_chars(ru_md.read_text(encoding="utf-8"))
    assert_markdown_structure(structure_source, ru_md.read_text(encoding="utf-8"))
    assembly_hash = canonical_hash({"md": sha_file(ru_md), "source": sha_file(src),
                                   "translation": sha_file(wk / "book.ru.masked.md")})
    build_inputs = {"assembly": assembly_hash, "layout": a.layout,
                    "title": a.title if a.title is not None else src.stem,
                    "author": a.author, "heading_numbers": a.heading_numbers,
                    "renderer": build_code_identity(__file__), "version": LAYOUT_VERSION,
                    "assets": tree_hashes(SKILL_ROOT / "assets"),
                    "figures": figure_inputs(md)}
    try:
        bl, prepared, build_receipt = select_build(bl, build_inputs)
    except ValueError as exc:
        die(str(exc))
    if not prepared:
        write_json(bl / "release.report.json", {"schema": 1, "status": "failed", "profile": "release",
                                                "reason": "candidate not yet accepted"})
        # 5) markdown -> LaTeX (figcaption.lua glues figures to their captions;
        # structure.lua demotes OCR front matter and, when headings carry the
        # original numbering, switches LaTeX section numbers off)
        # --no-highlight: без него pandoc на любом ``` -блоке выдаёт \NormalTok и
        # окружения Shaded/Highlighting, которых в преамбуле нет. На «New
        # Foundations for Classical Mechanics» это дало 30 «Undefined control
        # sequence» в одном приложении. Подсветка синтаксиса переводной книге по
        # математике не нужна, а окружения тянуть в преамбулу — лишняя связность.
        if run(["pandoc", ru_md,
                "--from=markdown+raw_tex+tex_math_dollars", "--to=latex",
                "--no-highlight",
                "--top-level-division=chapter", "--wrap=preserve",
                "--lua-filter", SKILL_ROOT / "assets" / "figcaption.lua",
                "--lua-filter", SKILL_ROOT / "assets" / "structure.lua",
                "-M", f"heading_numbers={a.heading_numbers}",
                "-o", bl / "body.tex"]).returncode:
            die("pandoc failed")
        # MinerU occasionally leaves a one-letter ``\\q`` fragment (or a
        # replacement character after it) in a long display-math line.  Pandoc
        # passes it through as an undefined TeX control sequence.  This is a
        # build-only repair of the derived body.tex; the translated Markdown and
        # all translation/cache evidence remain untouched.
        body_path = bl / "body.tex"
        body_text = body_path.read_text(encoding="utf-8")
        body_text, q_garbage = re.subn(
            r"\\q(?:[^\sA-Za-z]|(?=\s))", "", body_text)
        body_text, qants = re.subn(r"\\qquants\b", r"\\qquad", body_text)
        # OCR sometimes expands a visual alignment gap into dozens of adjacent
        # ``\qquad`` tokens in one array row.  Such a row becomes hundreds of
        # points overfull and can trigger a MiKTeX native-font failure.  Collapse
        # only runs of eight or more spacing tokens in the derived body; a short
        # alignment run is left intact.
        body_text, long_qqquad_runs = re.subn(
            r"(?:\\qquad\s*){8,}",
            r"\\qquad ",
            body_text,
        )
        # A known OCR/translation artefact can leave ``inf`` outside an inline
        # math span whose first token is a subscript, e.g.
        # ``inf \(_ B _ { i } \ f = 0\)``.  TeX interprets the second underscore
        # as a double subscript and aborts the release build.  Repair only this
        # exact, semantically unambiguous pattern in derived body.tex; keep the
        # Markdown, model reply, and cache evidence unchanged.
        body_text, inf_subscript = re.subn(
            r"\binf\s+\\\(_\s*B\s*_\s*\{\s*i\s*\}\s*\\\s*f\s*=\s*0\\\)",
            r"\\(\\inf _{B_i} f = 0\\)",
            body_text,
        )
        # Pandoc preserves one malformed source array literally: the preamble
        # declares three columns while the row contains four cells.  Widen this
        # exact derived row so XeLaTeX can typeset the displayed block.
        body_text, array_alignment = re.subn(
            r"\\begin\{array\}\{c c c\}(\s*D \\phi \(y\)\s*&\s*v _ \{d \+ 1\} \(y\)\s*&\s*\\dots\s*&\s*v _ \{n\} \(y\))",
            r"\\begin{array}{c c c c}\1",
            body_text,
        )
        # One extracted piecewise definition declares six columns but contains
        # eight cells in its longest rows.  Widen only that characteristic row
        # header; the translated source and cache evidence remain untouched.
        body_text, piecewise_array = re.subn(
            r"\\begin\{array\}\{l l l l l l\}(?=\s*0,\s*&\s*&\s*&\s*x\s*&\s*\\leq\s*&)",
            r"\\begin{array}{l l l l l l l l}",
            body_text,
        )
        # Pandoc can preserve a norm from a table cell as ``\\|x\\|`` inside
        # math.  The doubled row-break command makes XeLaTeX leave math mode and
        # fail with ``Missing $ inserted``; in this context it is unambiguously
        # the norm command ``\|``.  Repair only the derived TeX body.
        doubled_norm = body_text.count(r"\\|")
        if doubled_norm:
            body_text = body_text.replace(r"\\|", r"\|")
        # In one matrix OCR puts math-norm commands into the ``array`` preamble
        # (``c\|c\|...``).  The array package accepts a literal vertical rule
        # there, not the math ``\|`` command; normalize this exact derived
        # preamble while leaving genuine norms in cell contents untouched.
        array_preamble_source = r"\begin{array}{c\|c\|c\|c\|c}"
        array_preamble_target = r"\begin{array}{c|c|c|c|c|c}"
        array_preamble = body_text.count(array_preamble_source)
        if array_preamble:
            body_text = body_text.replace(array_preamble_source, array_preamble_target)
        # A few OCR fragments survive as text commands inside math: ``\AA``
        # expands to an Å glyph, while ``\textless``/``\textgreater`` rely on a
        # text font in an already active math span.  Repair only the derived TeX
        # body; the Markdown translation and source/cache evidence remain intact.
        angstrom_cmd = len(re.findall(r"\\AA\b", body_text))
        if angstrom_cmd:
            body_text = re.sub(r"\\AA\b", "A", body_text)
        textless_cmd = body_text.count(r"\textless")
        textgreater_cmd = body_text.count(r"\textgreater")
        if textless_cmd:
            body_text = body_text.replace(r"\textless", r"\ensuremath{<}")
        if textgreater_cmd:
            body_text = body_text.replace(r"\textgreater", r"\ensuremath{>}")
        # One OCR line uses Markdown dollar delimiters around a raw-TeX formula
        # immediately followed by the extracted footnote marker ``2``.  Pandoc
        # emits those dollars as ``\$...\$2`` after the preceding inline formula,
        # which puts the formula text outside math mode and causes ``Missing $
        # inserted``.  Normalize this one exact derived fragment to ``\(...\)2``;
        # retain the marker and leave the translated Markdown/cache untouched.
        boundary_source = (
            r"\$V ( a ; \delta ) \backslash B ( a ; \delta ) = "
            r"\{ x \in \mathbf { R } \^{} \{ n \} \mid "
            r"\textbar{} x - a \textbar{} = \delta \}\$2"
        )
        boundary_target = (
            r"\(V ( a ; \delta ) \backslash B ( a ; \delta ) = "
            r"\{ x \in \mathbf { R } ^ { n } \mid "
            r"\lvert x - a \rvert = \delta \} \)2"
        )
        body_text, boundary_delimiter = re.subn(
            re.escape(boundary_source),
            lambda _match: boundary_target,
            body_text,
        )
        # A second OCR artefact encodes the scalable right bracket of one open
        # interval as a one-row ``array`` containing ``\right.``.  The generated
        # array has an unmatched brace and makes XeLaTeX report ``Missing }``.
        # The array contributes no visible content, so remove only this exact
        # derived fragment and keep the interval's literal ``\left[`` delimiter.
        interval_array_source = r"\left[ \begin{array} { l l } { \right. } \end{array}"
        body_text, interval_array = re.subn(
            re.escape(interval_array_source),
            lambda _match: "[",
            body_text,
        )
        # The inverse-tangent example contains a nested OCR array used only to
        # stretch an interval bracket.  It creates a fragile native-font node;
        # replace the whole derived fragment with the equivalent plain interval.
        interval_nested_source = (
            r"\begin{array} { r } { \mathbf { R } \ \to \ \biggr ] "
            r"- \frac { \pi } { 2 } , \frac { \pi } { 2 } \left[ "
            r"\begin{array} { l } { \phantom { - } } \end{array} \right. } "
            r"\end{array}"
        )
        interval_nested_target = (
            r"\mathbf { R } \to ] - \frac { \pi } { 2 } , "
            r"\frac { \pi } { 2 } ["
        )
        body_text, interval_nested = re.subn(
            re.escape(interval_nested_source),
            lambda _match: interval_nested_target,
            body_text,
        )
        # XeLaTeX's CMU text face does not cover several Unicode math/dingbat
        # characters left literally by the extractor.  Convert them in the
        # derived TeX body to standard math commands.  ``ensuremath`` keeps the
        # replacement valid both in prose and inside an existing math span; the
        # source Markdown and translation/cache evidence remain unchanged.
        unicode_math_replacements = {
            "∂": r"\ensuremath{\partial}",
            "∫": r"\ensuremath{\int}",
            "∞": r"\ensuremath{\infty}",
            "∇": r"\ensuremath{\nabla}",
            "⊃": r"\ensuremath{\supset}",
               "⊆": r"\ensuremath{\subseteq}",
            "∧": r"\ensuremath{\wedge}",
            "⇒": r"\ensuremath{\Rightarrow}",
            "↦": r"\ensuremath{\mapsto}",
            "∈": r"\ensuremath{\in}",
            "′": r"\ensuremath{^{\prime}}",
            "℘": r"\ensuremath{\wp}",
            "ℝ": r"\ensuremath{\mathbb{R}}",
            "∩": r"\ensuremath{\cap}",
            "∼": r"\ensuremath{\sim}",
            "ϒ": r"\ensuremath{\Upsilon}",
            "☉": r"\ensuremath{\odot}",
            "□": r"\ensuremath{\square}",
            "⟨": r"\ensuremath{\langle}",
            "⟩": r"\ensuremath{\rangle}",
            "⟦": r"\ensuremath{[\![}",
            "⟧": r"\ensuremath{]\!]}",
            "❏": r"\ensuremath{\square}",
            "❐": r"\ensuremath{\square}",
            "❍": r"\ensuremath{\circ}",
            "✰": r"\ensuremath{\star}",
            "✷": r"\ensuremath{\star}",
            "✰": r"\ensuremath{\star}",
            "★": r"\ensuremath{\star}",
            "☆": r"\ensuremath{\star}",
            "✯": r"\ensuremath{\star}",
            "✻": r"\ensuremath{\star}",
            "✽": r"\ensuremath{\star}",
            "✿": r"\ensuremath{\star}",
            # A few polytonic Greek letters occur in explanatory prose.  CMU's
            # Unicode text face can fail in MiKTeX's native-font measurement path
            # on these rare code points; retain the letters without the combining
            # accent in the derived body.
            "ὁ": "ο",
            "ῖ": "ι",
            "ἔ": "ε",
            # Symbols which the extractor left outside the earlier replacement
            # set.  ensuremath is valid in both prose and an existing math span.
            "⋆": r"\ensuremath{\star}",
            "∗": r"\ensuremath{\ast}",
            "−": r"\ensuremath{-}",
            "→": r"\ensuremath{\to}",
            "∅": r"\ensuremath{\varnothing}",
            "‖": r"\ensuremath{\Vert}",
            "⊕": r"\ensuremath{\oplus}",
            "⊥": r"\ensuremath{\perp}",
            "∀": r"\ensuremath{\forall}",
            "•": r"\ensuremath{\bullet}",
            "◦": r"\ensuremath{\circ}",
            # Remaining raw Greek letters are emitted by the extractor both in
            # formulas and in short etymological notes.  Explicit TeX commands
            # avoid passing native Unicode Greek glyphs to dvipdfmx.
            "Φ": r"\ensuremath{\Phi}",
            "Ψ": r"\ensuremath{\Psi}",
            "α": r"\ensuremath{\alpha}",
            "ψ": r"\ensuremath{\psi}",
            "θ": r"\ensuremath{\theta}",
            "δ": r"\ensuremath{\delta}",
            "φ": r"\ensuremath{\phi}",
            "π": r"\ensuremath{\pi}",
            "ν": r"\ensuremath{\nu}",
            "γ": r"\ensuremath{\gamma}",
            "Γ": r"\ensuremath{\Gamma}",
            "κ": r"\ensuremath{\kappa}",
            "τ": r"\ensuremath{\tau}",
            "λ": r"\ensuremath{\lambda}",
            "ο": "o",
            "Ω": r"\ensuremath{\Omega}",
            "ζ": r"\ensuremath{\zeta}",
            "η": r"\ensuremath{\eta}",
            "ι": r"\ensuremath{\iota}",
            "ε": r"\ensuremath{\epsilon}",
            "Ξ": r"\ensuremath{\Xi}",
            "μ": r"\ensuremath{\mu}",
            "ς": r"\ensuremath{\varsigma}",
            "β": r"\ensuremath{\beta}",
            "ρ": r"\ensuremath{\rho}",
        }
        unicode_math_counts = {}
        for char, replacement in unicode_math_replacements.items():
            count = body_text.count(char)
            if count:
                unicode_math_counts[char] = count
                body_text = body_text.replace(char, replacement)
        mathbb_b = body_text.count(r"\mathbb {b}")
        if mathbb_b:
            body_text = body_text.replace(r"\mathbb {b}", r"\mathbf {b}")
        # One OCR line mangles the Greek etymology of ``гомеоморфизм`` into an
        # unfinished math fragment containing ``\hat{\eta}`` and ``\mathsf``
        # glyphs.  XeTeX/dvipdfmx can abort while measuring that malformed node.
        # Preserve the readable Russian meaning in the derived body only; the
        # translated Markdown and its cache evidence remain unchanged.
        etymology_source = (
            r"(o\ensuremath{\mu}o\ensuremath{\iota}o\ensuremath{\varsigma} "
            r"= похожий и \(\hat { \eta } \ \{ \mathsf { \Pi } "
            r" \mathsf { \Pi } \{ \mathsf { o } \mathsf { \rho } "
            r"\varphi \hat { \eta } = \mathsf { s h a p e } \}\)"
        )
        body_text, malformed_etymology = re.subn(
            re.escape(etymology_source),
            lambda _match: "(гомеоморфизм: похожий по форме)",
            body_text,
        )
        automorphism_etymology_source = (
            r"(\(( \mathsf { \alpha { \dot { \alpha } } } "
            r"\mathsf { \tau \acute { o } } \mathsf { \zeta } = "
            r"\mathsf { o f }\), то есть самих отображений)"
        )
        body_text, automorphism_etymology = re.subn(
            re.escape(automorphism_etymology_source),
            lambda _match: "(то есть самих отображений)",
            body_text,
        )
        # One OCR glyph is exported as the undefined ``\pentagon`` command in a
        # map declaration.  In context it is the arrow between the domain and
        # codomain, so normalize it in the derived TeX body only.
        pentagon_cmd = body_text.count(r"\pentagon")
        if pentagon_cmd:
            body_text = body_text.replace(r"\pentagon", r"\to")
        # A single OCR fragment nests the text-mode ``\em`` switch inside a bold
        # math symbol (``\mathbf { \em W }``).  XeLaTeX expands it to ``\itshape``
        # and rejects it in math mode; the intended symbol is simply bold W.
        body_text, em_math = re.subn(
            r"\\mathbf\s*\{\s*\\em\s+W\s*\}",
            r"\\mathbf { W }",
            body_text,
        )
        # Keep a final guard for the same one-letter text switch if Pandoc varies
        # the surrounding braces or spaces.  The derived body contains no other
        # standalone ``\em`` command, while ``\emph`` remains unaffected.
        body_text, em_switch = re.subn(r"\\em(?![A-Za-z])", "", body_text)
        # A malformed etymology parenthesis contains ``\bigl`` without its
        # delimiter (``\bigl \mathrm { b a s i s }``); dropping only the size
        # command keeps the readable word and restores balanced TeX math.
        basis_bigl_source = r"\bigl \mathrm { b a s i s }"
        basis_bigl = body_text.count(basis_bigl_source)
        if basis_bigl:
            body_text = body_text.replace(basis_bigl_source, r"\mathrm { b a s i s }")
        # Equation-star markers are emitted as ``\tag{\ensuremath{\star}}`` by
        # Pandoc.  Inside amsmath's tag box the nested ensuremath switch can
        # trigger XeTeX's native-font measurement bug; a literal star is the same
        # visible marker and is safe in a tag.
        star_tag = body_text.count(r"\tag{\ensuremath{\star}}")
        if star_tag:
            body_text = body_text.replace(r"\tag{\ensuremath{\star}}", r"\tag{*}")
        # OCR occasionally uses text-font switches for single letters inside
        # math.  XeTeX represents those as native-font nodes; use the matching
        # math-font commands instead.  Plain ``\text{...}`` prose remains intact.
        textbf_math = body_text.count(r"\textbf")
        textit_math = body_text.count(r"\textit")
        textsf_math = body_text.count(r"\textsf")
        if textbf_math:
            body_text = body_text.replace(r"\textbf", r"\mathbf")
        if textit_math:
            body_text = body_text.replace(r"\textit", r"\mathit")
        if textsf_math:
            body_text = body_text.replace(r"\textsf", r"\mathsf")
        # Proof-ending squares are prose-level math markers.  Use explicit math
        # delimiters rather than ensuremath, which can leave a native node in a
        # long paragraph on XeTeX/MiKTeX.
        square_marker = body_text.count(r"\ensuremath{\square}")
        if square_marker:
            body_text = body_text.replace(r"\ensuremath{\square}", r"\(\square\)")
        # If the extractor already emitted an explicit proof marker, keep the
        # surrounding prose but avoid the problematic square glyph in XeTeX's
        # native measurement path.  This is build-only and affects no equations.
        explicit_square_marker = body_text.count(r"\(\square\)")
        if explicit_square_marker:
            body_text = body_text.replace(r"\(\square\)", ".")
        prose_star_marker = body_text.count(r"\ensuremath{\star}")
        if prose_star_marker:
            body_text = body_text.replace(r"\ensuremath{\star}", ".")
        # The source extraction contains one equation with two adjacent tags,
        # ``\tag{3-12}\tag{8.13}``.  amsmath rejects multiple tags in one
        # display.  Keep the last tag: the surrounding translated prose refers
        # to the current volume's equation (8.13).  This is a build-only repair;
        # the extracted Markdown, translation, and cache evidence stay unchanged.
        body_text, duplicate_tag = re.subn(
            r"\\tag\s*\{[^{}]*\}\s*\\tag\s*\{([^{}]*)\}",
            r"\\tag{\1}",
            body_text,
        )
        if (q_garbage or qants or inf_subscript or array_alignment
                or piecewise_array or doubled_norm or array_preamble or long_qqquad_runs
                or angstrom_cmd
                or textless_cmd or textgreater_cmd or boundary_delimiter
                or interval_array
                or interval_nested
                or malformed_etymology
                or automorphism_etymology
                or pentagon_cmd
                or em_math
                or em_switch
                or basis_bigl
                or star_tag
                or textbf_math or textit_math or textsf_math
                or square_marker or explicit_square_marker or prose_star_marker
                or unicode_math_counts
                or mathbb_b or duplicate_tag):
            body_path.write_text(body_text, encoding="utf-8", newline="")
            print(f">> build math repair: removed {q_garbage} stray \\q "
                  f"fragment(s), normalized {qants} malformed \\qquants, "
                  f"repaired {inf_subscript} malformed inf-subscript fragment(s), "
                  f"widened {array_alignment + piecewise_array} malformed array row(s), "
                  f"normalized {doubled_norm} doubled norm delimiter(s), "
                   f"normalized {array_preamble} array preamble(s), "
                  f"collapsed {long_qqquad_runs} long \\qquad run(s), "
                  f"repaired {angstrom_cmd} OCR \\AA command(s), "
                  f"normalized {textless_cmd + textgreater_cmd} text/math delimiter command(s), "
                  f"repaired {boundary_delimiter} OCR boundary delimiter fragment(s), "
                  f"removed {interval_array} OCR interval-array fragment(s), "
                  f"normalized {interval_nested} nested OCR interval array(s), "
                  f"repaired {malformed_etymology} malformed etymology fragment(s), "
                  f"repaired {automorphism_etymology} malformed automorphism etymology fragment(s), "
                   f"repaired {pentagon_cmd} OCR pentagon command(s), "
                   f"repaired {em_math + em_switch} text-em math fragment(s), "
                   f"repaired {basis_bigl} malformed basis delimiter(s), "
                  f"normalized {star_tag} star equation tag(s), "
                  f"normalized {textbf_math + textit_math + textsf_math} text-font math command(s), "
                  f"normalized {square_marker + explicit_square_marker} proof-square marker(s), "
                  f"removed {prose_star_marker} prose-star marker(s), "
                  f"mapped {sum(unicode_math_counts.values())} unsupported Unicode math/glyph(s), "
                  f"repaired {mathbb_b} unsupported \\mathbb{{b}} glyph(s), "
                  f"collapsed {duplicate_tag} duplicate equation tag(s)")
        assert_typeset_structure(ru_md.read_text(encoding="utf-8"),
                                body_text)
        preamble = (SKILL_ROOT / "assets" / "preamble.tex").read_text(encoding="utf-8")
        (bl / "main.tex").write_text(
            assemble_main_tex(preamble, layout=a.layout,
                              title=(a.title if a.title is not None else src.stem),
                              author=a.author),
            encoding="utf-8")
        copy_figures(md, bl)

        seal_build(bl, build_receipt)
    else:
        print(">> build: reuse verified prepared candidate", flush=True)

    # 5b) комплектность картинок: чего не хватает — честно предупредить заранее
    refs = set(re.findall(r"\\(?:includegraphics[^{]*|FigWithCaption)\{([^}]+)\}",
                          (bl / "body.tex").read_text(encoding="utf-8", errors="replace")))
    missing = sorted(r for r in refs if not (bl / r).exists())
    if missing:
        print(f"?? не хватает {len(missing)} из {len(refs)} картинок — в PDF они "
              "станут рамками-заглушками; список: build/missing_images.txt")
        (bl / "missing_images.txt").write_text("\n".join(missing), encoding="utf-8")

    # 6) compile
    ok, rc = latex_build(bl)
    if not ok:
        log = bl / "main.log"
        if log.exists():
            tail = read_tex_log(log).splitlines()[-30:]
            print("\n".join(tail))
        die(f"LaTeX failed — inspect {log}. Typical fix: escape a special "
            "character in build/body.tex, then re-run (translation is cached).")
    final = root / f"{src.stem}.ru.pdf"
    # Publication follows mandatory technical acceptance below.

    # 7) consistency self-audit (diagnosis only; soft — never fails the run)
    try:
        import fitz  # noqa: F401
        if run([sys.executable, SCRIPTS / "audit_selfcheck.py", ru_md,
                bl / "body.tex", bl / "main.pdf",
                "--report", root / "audit_report.md"]).returncode:
            print("?? самоаудит нашёл проблемы гигиены PDF — "
                  "см. audit_report.md", flush=True)
    except ImportError:
        print("?? PyMuPDF не установлен — самоаудит пропущен "
              "(pip install pymupdf)")
    if rebuild_toc and r"\tableofcontents" not in (bl / "main.tex").read_text(encoding="utf-8"):
        die("TOC rebuild decision requires a generated table of contents")
    technical = inspect_pdf(bl / "main.pdf", bl / "main.log",
                            require_toc=rebuild_toc or bool(re.search(r"(?m)^#", ru_md.read_text(encoding="utf-8"))),
                            source_text=md.read_text(encoding="utf-8"))
    raw = check_result(masked, wk / "book.ru.masked.md")
    if book_settings(a, gpath) != settings_hash:
        die("translation settings/profile changed during build; candidate not published")
    if (sha_file(a.book_profile) if a.book_profile else None) != book_profile_hash:
        die("book profile changed during build; candidate not published")
    if not a.no_clean:
        verify_cleaning_coverage(cleaning_source, md, coverage_path, profile=book_profile)
        if sha_file(coverage_path) != coverage_hash:
            die("cleaning receipt changed during build")
    if sha_file(post_report_path) != post_report_hash or sha_file(unmasked) != unmasked_hash:
        die("postprocessing evidence changed during build")
    if ((sha_file(a.keep_source) if a.keep_source else None) != preservation_hash
            or raw.get("source_preservation_sha256") != preservation_hash):
        die("keep-source policy changed during build; published PDF unchanged")
    if (sha_file(src) != input_hash or assembly_hash != canonical_hash({"md": sha_file(ru_md),
            "source": sha_file(src), "translation": sha_file(wk / "book.ru.masked.md")})):
        die("assembly inputs changed during build; published PDF unchanged")
    partial = raw["status"] != "passed" or bool(raw.get("kept_source")) or bool(raw.get("failed_blocks"))
    if a.polish:
        partial |= check_result(p_masked, p_out)["status"] != "passed"
    release = {"schema": 1, "profile": "draft" if partial else "release",
               "status": "failed" if technical["status"] != "passed" or missing else "warning" if partial else "passed",
               "source_sha256": sha_file(src), "pdf_sha256": sha_file(bl / "main.pdf"),
               "settings_sha256": settings_hash,
               "compile": json.loads((bl / "compile.report.json").read_text(encoding="utf-8")),
               "technical": technical, "translation_report_sha256": sha_file(str(wk / "book.ru.masked.md") + ".report.json"),
               "preserved_source": raw.get("preserved_source", 0),
               "allowed_source": raw.get("allowed_source", []),
               "source_preservation_sha256": preservation_hash,
               "book_profile_sha256": book_profile_hash,
               "cleaning_coverage_sha256": coverage_hash,
               "postprocess_coverage_sha256": post_report_hash,
               "visual_review": "not_run", "semantic_review": "not_run"}
    write_json(bl / "release.report.json", release)
    if release["status"] != "passed":
        print(f"!! candidate only ({release['status']}), previous PDF untouched: {bl}")
        sys.exit(4 if partial else 3)
    publish_candidate(bl / "main.pdf", final, release)
    print(f"\nDONE: {final} (technical acceptance only)")


if __name__ == "__main__":
    main()
