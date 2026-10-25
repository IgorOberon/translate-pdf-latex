# -*- coding: utf-8 -*-
"""Перевести все book.partN.md из папки и свести приёмку по отчётам.

Кэш ОДИН на книгу (<dir>/.tcache), а не по кэшу на часть. Ключ кэша — хэш
самого блока, поэтому общий кэш означает, что книгу можно переразбить на
части иначе (сменился порядок, вставились заголовки) и заново переводить
придётся только действительно новые блоки. Раньше каждая часть держала свой
каталог, и любое изменение нарезки обесценивало всё сразу.

Повтор безопасен: переведённое берётся из кэша, провалы не кэшируются.

Использование:
    python scripts/run_doc_translate.py <dir> [--glossary FILE] [--first N]
        [--last N] [--parallel 3] [--model hy-mt2-7b]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):      # не обёртка: она
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import default_glossary, lms, venv_python  # noqa: E402
from provenance import MANIFEST, create_manifest, load_manifest, inside, check_result, write_json
from request_plan import plan_fragments
from glossary_selection import load_profile, select_glossary
from translate import build_messages, split_blocks, is_translatable
from request_budget import estimate_messages
from editorial import source_review, term_guard_findings, review_ledger

PY = venv_python()
TRANS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "translate.py")
LMS = lms()

# Замерено на hy-mt2-7b (Q8_0) по usage.prompt_tokens его же ответов:
# немецкая проза 3,36 знака на токен, русская 2,23, строки глоссария
# «Term => Перевод» 2,58. Берём с запасом в свою сторону.
DE_CHARS_PER_TOK = 3.2
RU_CHARS_PER_TOK = 2.2
GLOSS_CHARS_PER_TOK = 2.5
SYSTEM_OVERHEAD_TOK = 120       # каркас промпта без глоссария
RU_TO_DE_CHARS = 1.0            # русский перевод примерно той же длины


def lms_state(model):
    """Что LM Studio говорит о загруженной модели, или None.

    Diagnostic only: slot/context allocation depends on the runtime.
    Never derive a per-request limit by dividing context by parallel here.
    """
    if not os.path.exists(LMS):
        return None
    try:
        r = subprocess.run([LMS, "ps", "--json"], capture_output=True,
                           timeout=40, text=True, encoding="utf-8",
                           errors="replace")
        rows = json.loads(r.stdout or "[]")
    except Exception:                                   # noqa: BLE001
        return None
    for row in rows:
        if model in (row.get("identifier"), row.get("modelKey")):
            return {"context": int(row.get("contextLength") or 0),
                    "parallel": int(row.get("parallel") or 1),
                    "max_context": int(row.get("maxContextLength") or 0)}
    return None


def longest_block(work_dir, split_chars):
    manifest = load_manifest(work_dir)
    return max((len(f["text"]) for row in manifest["parts"]
                for block in split_blocks(inside(work_dir, row["path"]).read_bytes().decode("utf-8"))
                if is_translatable(block) for f in plan_fragments(block, split_chars)), default=0)


def check_context(work_dir, glossary_path, model, parallel, split_chars,
                  context_per_request=0, source_lang="German", target_lang="Russian",
                  glossary_profile=None, mode="hymt"):
    """Conservative character estimate, not a tokenizer or runtime guarantee."""
    glossary = Path(glossary_path).read_text(encoding="utf-8").strip() if glossary_path else ""
    manifest = load_manifest(work_dir)
    need, longest = 0, 0
    for row in manifest["parts"]:
        for block in split_blocks(inside(work_dir, row["path"]).read_bytes().decode("utf-8")):
            if not is_translatable(block):
                continue
            for fragment in plan_fragments(block, split_chars):
                text = fragment["text"]
                longest = max(longest, len(text))
                messages = build_messages(text, mode, target_lang,
                                          select_glossary(text, glossary, glossary_profile),
                                          source_lang=source_lang)
                # 2 chars/token plus a 20% margin and full configured reply reserve.
                estimate = estimate_messages(messages)
                need = max(need, estimate)
    lines = [f"estimated maximum request+reply budget: ~{need} tokens; fragment {longest} chars"]
    if not context_per_request:
        lines.append("?? per-request runtime context unknown; estimate only, no capacity confirmation. "
                     "Set --context-per-request after checking server settings; context/parallel is not assumed.")
        return True, lines
    ok = need <= context_per_request
    lines.append(f"{'OK estimate' if ok else '!! budget exceeded'}: per-request context {context_per_request}")
    return ok, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir")
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--glossary-profile", default=None)
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=sys.maxsize)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--model", default="hy-mt2-7b")
    ap.add_argument("--mode", choices=["hymt", "generic"], default="hymt")
    ap.add_argument("--host", default="http://localhost:1234")
    ap.add_argument("--target-lang", default="Russian")
    ap.add_argument("--source-lang", default="German",
                    help="этим маршрутом идут немецкие книги XIX века, и "
                         "прежний хардкод «English» в системном промпте и в "
                         "шапке глоссария противоречил самому тексту")
    ap.add_argument("--split-chars", type=int, default=0,
                    help="передаётся в translate.py: делить блок длиннее N "
                         "знаков по границам предложений. Hy-MT2 на длинных "
                         "абзацах конспектирует; на томе 6 работало 900")
    ap.add_argument("--min-ratio", type=float, default=0.0,
                    help="передаётся в translate.py: брак ответа короче "
                         "исходника в эту долю")
    ap.add_argument("--skip-context-check", action="store_true",
                    help="обойти оценочный отказ по подтверждённому лимиту")
    ap.add_argument("--context-per-request", type=int, default=0,
                    help="confirmed runtime limit per request; 0 = unknown (warning)")
    a = ap.parse_args()
    if a.glossary is None and (Path(a.work_dir) / "glossary.md").is_file():
        a.glossary = str(Path(a.work_dir) / "glossary.md")
    if not (Path(a.work_dir) / MANIFEST).exists():
        create_manifest(a.work_dir)
    manifest = load_manifest(a.work_dir)
    ledger = review_ledger(a.work_dir, manifest, split_blocks((Path(a.work_dir) / "book.md").read_text(encoding="utf-8")))
    if ledger["status"] == "failed":
        print("!! unresolved editorial findings: inspect editorial.findings.json before MT")
        return 5
    profile_path = Path(a.work_dir) / "structure.json"
    cfg = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
    review = source_review(split_blocks((Path(a.work_dir) / "book.md").read_text(encoding="utf-8")), cfg)
    write_json(Path(a.work_dir) / "source.review.json", review)
    if review["status"] == "failed":
        print("!! OCR review required before MT: inspect source.review.json; fix by checked profile corrections")
        return 5

    ok, lines = check_context(a.work_dir, a.glossary, a.model, a.parallel,
                              a.split_chars, a.context_per_request,
                              a.source_lang, a.target_lang, load_profile(a.glossary_profile), a.mode)
    for ln in lines:
        print(("   " if not ln.startswith("!!") else "") + ln)
    if not ok and not a.skip_context_check:
        print("\nЗапуск остановлен. Переполнение контекста приходит как "
              "обычный HTTP 400 и выглядит как случайные провалы блоков — "
              "на томе 6 так падал каждый третий. Обойти: "
              "--skip-context-check")
        return 5

    cache = os.path.join(a.work_dir, ".tcache")
    os.makedirs(cache, exist_ok=True)

    rcs = {}
    for n, row in enumerate(manifest["parts"], 1):
        if not a.first <= n <= a.last:
            continue
        src, dst = inside(a.work_dir, row["path"]), inside(a.work_dir, row["output"])
        cmd = [PY, "-X", "utf8", TRANS, str(src), str(dst),
               "--mode", a.mode, "--model", a.model, "--host", a.host,
               "--target-lang", a.target_lang, "--source-lang", a.source_lang,
               "--cache-dir", cache, "--parallel", str(a.parallel),
               "--split-chars", str(a.split_chars), "--min-ratio", str(a.min_ratio),
               "--context-per-request", str(0 if a.skip_context_check else a.context_per_request),
               "--manifest-sha256", manifest["manifest_sha256"]]
        if a.glossary:
            cmd += ["--glossary", a.glossary]
        if a.glossary_profile:
            cmd += ["--glossary-profile", a.glossary_profile]
        print(f"\n=== PART {n} ===", flush=True)
        rcs[n] = subprocess.run(cmd).returncode
        if rcs[n] == 3:
            # Dead server: do not spend the same retry budget on every remaining part.
            break

    # сводная приёмка: непереведённые блоки раньше оставались только в
    # stderr отдельных частей, и английский текст молча уезжал в книгу
    failures, total, kept = [], 0, 0
    editorial = []
    for part in sorted(rcs):
        row = manifest["parts"][part - 1]
        try:
            result = check_result(inside(a.work_dir, row["path"]),
                                  inside(a.work_dir, row["output"]),
                                  manifest["manifest_sha256"])
            total += result["blocks_total"]
            kept += result["kept_source"]
            if rcs[part] or result["status"] != "passed" or result["failed_blocks"]:
                failures.append(f"part {part}: translation failed")
            src = inside(a.work_dir, row["path"])
            dst = inside(a.work_dir, row["output"])
            for source, target in zip(split_blocks(src.read_text(encoding="utf-8")), split_blocks(dst.read_text(encoding="utf-8"))):
                editorial.extend(term_guard_findings(source, target, cfg.get("term_guards", [])))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"part {part}: {exc}")
    if not rcs:
        failures.append("no selected parts")
    complete = len(rcs) == len(manifest["parts"])
    if editorial:
        failures.append("book-owned term guards failed; review/retranslate aligned contexts")
    write_json(Path(a.work_dir) / "terminology.review.json", {"schema": 1,
               "status": "failed" if editorial else "passed", "findings": editorial, "semantic_review": "not_run"})
    summary = {"schema": 1, "manifest_sha256": manifest["manifest_sha256"],
               "status": "failed" if failures else "passed" if complete else "warning",
               "scope": "all_parts" if complete else "selected_parts",
               "parts": rcs, "blocks": total, "kept_source": kept, "errors": failures,
               "semantic_review": "not_run"}
    write_json(Path(a.work_dir) / "translation.run.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 4 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
