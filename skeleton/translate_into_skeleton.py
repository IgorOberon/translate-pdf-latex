#!/usr/bin/env python3
"""translate_into_skeleton.py v1.0 — оркестратор перевода в ЧУЖОЙ LaTeX-каркас.

Те же стадии, что у translate_book.py (нарезка страниц -> извлечение -> санация ->
маскирование -> перевод -> размаскирование), но вместо сборки собственного
pandoc-документа переведённые разделы раскладываются по существующим файлам
глав с маркерами вставки, и книга собирается родным движком проекта
(LuaLaTeX/XeLaTeX определяется по main.tex).

Ожидаемое устройство проекта (образец: D:\\classical mechanic):
  <project>/book/main.tex              мастер-файл; главы подключены \\input{chapters/chNN}
  <project>/book/chapters/chNN.tex     заглушки: \\section{...} + \\origpages{a--b} +
                                       строка-маркер  % >>> ... раздела N.M ... <<<
  <project>/figures/chNN/              каталоги рисунков
  <project>/glossary.md                фид глоссария (строки `en => ru`)

Возобновляемость: раздел, чей маркер уже исчез из заглушки, не трогается;
нарезка/извлечение/перевод кэшируются в <project>/_skeleton_work/<глава>/.
Перевод внутри главы возобновляем через кэш translate.py.

Пример:
  python translate_into_skeleton.py "D:\\classical mechanic" --offset 14 \\
      --extract math --chapters ch01,ch02 --glossary "D:\\classical mechanic\\glossary.md"

Смещение: pdf_страница = книжная_страница + offset (у Тейлора offset=14).
"""
import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

print(">> translate_into_skeleton.py v1.0 loaded", flush=True)

SKELETON_DIR = Path(__file__).resolve().parent
SCRIPTS = SKELETON_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from procutils import (TIMEOUT_RC, PipelineLock, parse_unresolved,  # noqa: E402
                       reap_mineru, run_mineru, unload_llm, untranslated_ok)

MARKER_RE = re.compile(r"^\s*%\s*>>>.*<<<\s*$")
NUM_RE = re.compile(r"([A-Z0-9]+(?:\.[0-9]+)+)")
ORIGPAGES_RE = re.compile(r"\\origpages\{(\d+)(?:\s*--\s*(\d+))?\}")
INTRO_SENTINEL = "% [skeleton-orchestrator] intro"


from provenance import check_result, sha_file, write_json, canonical_hash
from build_release import compile_latex, inspect_pdf, publish_candidate
from prepare_mineru import prepare, assert_markdown_structure, assert_typeset_structure
from endpoint_policy import model_opener
import tempfile


def die(msg):
    print(f"!! {msg}", flush=True)
    sys.exit(1)


def run(cmd, **kw):
    print(">>", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(list(map(str, cmd)), **kw)


# ----------------------------------------------------------------- discovery
def find_main_tex(project):
    cands = [project / "book" / "main.tex", project / "main.tex"]
    cands += sorted(project.rglob("main.tex"))
    for c in cands:
        if c.is_file() and "\\documentclass" in c.read_text(encoding="utf-8",
                                                            errors="replace"):
            return c.resolve()
    die("main.tex с \\documentclass не найден в проекте")


def find_source_pdf(project, explicit):
    if explicit:
        p = Path(explicit)
        p = p if p.is_absolute() else project / p
        if not p.is_file():
            die(f"--source: файла нет: {p}")
        return p.resolve()
    pdfs = sorted(project.glob("*.pdf"), key=lambda p: p.stat().st_size,
                  reverse=True)
    if not pdfs:
        die("в корне проекта нет PDF-исходника (укажите --source)")
    print(f">> исходник (крупнейший PDF): {pdfs[0].name} "
          f"({pdfs[0].stat().st_size / 2**20:.0f} МБ); не он — задайте --source")
    return pdfs[0].resolve()


def chapter_files(main_tex):
    """Файлы глав в порядке \\input из main.tex."""
    body = main_tex.read_text(encoding="utf-8", errors="replace")
    files = []
    for m in re.finditer(r"\\input\{([^}]+)\}", body):
        rel = m.group(1)
        if not rel.endswith(".tex"):
            rel += ".tex"
        p = (main_tex.parent / rel).resolve()
        if p.is_file() and p != main_tex and "preamble" not in p.stem.lower():
            files.append(p)
    if not files:
        die("в main.tex не найдено \\input{...} глав")
    return files


def parse_stub(path):
    """Разбор заглушки: маркеры вставки + диапазоны страниц оригинала."""
    lines = path.read_text(encoding="utf-8").splitlines()
    sections, pages, last_pages = [], [], None
    for i, ln in enumerate(lines):
        m = ORIGPAGES_RE.search(ln)
        if m:
            a = int(m.group(1))
            b = int(m.group(2) or m.group(1))
            last_pages = (a, b)
            pages.append((a, b))
        elif MARKER_RE.match(ln):
            nm = NUM_RE.search(ln)
            sections.append({"num": nm.group(1) if nm else None,
                             "line": i, "pages": last_pages})
    rng = (min(a for a, _ in pages), max(b for _, b in pages)) if pages else None
    return {"path": path, "lines": lines, "sections": sections, "pages": rng}


def detect_engine(main_tex, override):
    if override != "auto":
        return override
    head = "\n".join(main_tex.read_text(encoding="utf-8",
                                        errors="replace").splitlines()[:40])
    pre = main_tex.parent / "preamble.tex"
    if pre.is_file():
        head += pre.read_text(encoding="utf-8", errors="replace")[:3000]
    if re.search(r"lualatex|\\directlua|luacode", head, re.I):
        return "lualatex"
    return "xelatex"


# ----------------------------------------------------------------- preflight
def preflight(a, need_translate, need_extract, engine):
    if not shutil.which("pandoc"):
        die("pandoc не найден в PATH")
    if not (shutil.which("latexmk") or shutil.which(engine)):
        die(f"не найден ни latexmk, ни {engine}")
    if need_extract:
        try:
            import pymupdf  # noqa: F401
        except ImportError:
            die("pymupdf не установлен — запускайте питоном из venv конвейера "
                "(venv\\Scripts\\python.exe в корне скилла; см. README)")
        tool = "mineru" if a.extract == "math" else "marker_single"
        if not shutil.which(tool):
            die(f"'{tool}' не найден в PATH — активируйте venv конвейера")
        if a.offset is None and a.folio != "auto":
            die("нужно смещение страниц: --offset N (pdf = книжная + N). "
                "Подсказка обычно есть в PROGRESS.md проекта")
    if need_translate:
        try:
            with model_opener().open(a.host.rstrip("/") + "/v1/models",
                                        timeout=5) as r:
                ids = [m.get("id") for m in json.load(r).get("data", [])]
            if a.model not in ids:
                print(f"?? модели '{a.model}' нет в списке {ids}; "
                      "LM Studio может подгрузить её JIT — продолжаю")
        except Exception as e:
            die(f"сервер перевода недоступен: {a.host} ({e}). "
                "LM Studio: Developer -> Start Server")


# ----------------------------------------------- folio: печатные номера страниц
# Урок Тейлора: единое --offset на книгу дрейфует (нумерованные, но не
# считаемые страницы-разделители между частями) и молча режет хвост КАЖДОЙ
# главы. Поэтому положение главы определяется НЕЗАВИСИМО — по печатным
# номерам страниц (folio) из текстового слоя; --offset остаётся запасным
# входом для сканов без текстового слоя.

FOLIO_HEAD_LINES = 4        # где искать folio в get_text() страницы
MINERU_WINDOW = 64          # окно гибридного бэкенда MinerU (дубли на стыке)


def page_folio(page):
    """Печатный номер страницы из текстового слоя, или None.
    1) одиночное число отдельной строкой в первых строках;
    2) fallback: 2-4 цифры между табами в начале («RunningHead \\t NNN»)."""
    txt = page.get_text() or ""
    for ln in txt.splitlines()[:FOLIO_HEAD_LINES]:
        m = re.fullmatch(r"(\d{1,4})", ln.strip())
        if m:
            return int(m.group(1))
    m = re.search(r"(?:^|\t)[ \t]*(\d{2,4})[ \t]*(?:\t|$)", txt[:120], re.M)
    return int(m.group(1)) if m else None


def build_folio_map(doc):
    """{folio -> pdf_index(0-based)}. Достоверность: номер принимается, только
    если согласован с соседним номером (g - f == j - i в пределах 3 страниц) —
    одинокое «8» со шмуцтитула главы 8 согласованных соседей не имеет."""
    raw = [(i, page_folio(doc[i])) for i in range(doc.page_count)]
    raw = [(i, f) for i, f in raw if f is not None]
    fmap = {}
    for k, (i, f) in enumerate(raw):
        for j, g in raw[max(0, k - 2):k + 3]:
            if j != i and abs(j - i) <= 3 and g - f == j - i:
                fmap.setdefault(f, i)
                break
    return fmap


def load_folio_map(src, workroot):
    """build_folio_map с кэшем в _skeleton_work/folio_map.json
    (ключ валидности включает SHA-256 PDF)."""
    import pymupdf
    doc = pymupdf.open(src)
    cache = workroot / "folio_map.json"
    key = {"pdf": Path(src).name, "pages": doc.page_count, "sha256": sha_file(src)}
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if {k: data.get(k) for k in key} == key:
                return {int(f): i for f, i in data["map"].items()}
        except (ValueError, KeyError):
            pass
    fmap = build_folio_map(doc)
    workroot.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({**key, "map": fmap}), encoding="utf-8")
    return fmap


def locate_chapter(fmap, book_a, book_b, ch=""):
    """(p0, p1, eff_offset) по folio-карте, или None, если в диапазоне главы
    нет ни одного достоверного folio. Начало тянется от МЛАДШЕГО известного
    folio внутри главы, конец — от СТАРШЕГО (разделители сидят МЕЖДУ главами,
    внутри главы нумерация линейна)."""
    known = sorted(f for f in fmap if book_a <= f <= book_b)
    if not known:
        return None
    fa, fb = known[0], known[-1]
    p0 = fmap[fa] - (fa - book_a)
    p1 = fmap[fb] + (book_b - fb)
    if p1 - p0 != book_b - book_a:
        raise RuntimeError(
            f"{ch}: pdf-диапазон {p0 + 1}–{p1 + 1} не совпадает по длине с "
            f"книжными {book_a}–{book_b} — похоже, внутри диапазона главы "
            "вставные страницы; проверьте \\origpages")
    return p0, p1, p0 + 1 - book_a


def probe_slice(pdf_path, book_a, book_b):
    """Контрольный зонд нарезанного pages.pdf: folio первых/последних страниц
    против ожидаемых книжных. -> (ok: bool|None, деталь) — None = не проверить."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    n = doc.page_count
    checked = 0
    for i in list(range(min(3, n))) + list(range(max(n - 3, 0), n)):
        f = page_folio(doc[i])
        if f is None:
            continue
        want = book_a + i
        if abs(f - want) > 500:     # мусорное число, не folio
            continue
        checked += 1
        if f != want:
            return False, f"pdf-стр. {i + 1} среза несёт folio {f}, ожидалась {want}"
    return (True, f"{checked} зондов") if checked else (None, "folio не извлекаются")


# ------------------------------------------------------------------- stages
def slice_pdf(src, book_a, book_b, p0, p1, out, probe=True):
    """Нарезка pdf-диапазона [p0, p1] (0-based) в out; folio-зонд среза."""
    import pymupdf
    if out.is_file():
        if probe:
            ok, detail = probe_slice(out, book_a, book_b)
            if ok is False:
                die(f"кэшированная нарезка {out.name} не соответствует книжным "
                    f"страницам {book_a}–{book_b}: {detail} — удалите "
                    f"{out.parent.name}/pages.pdf и {out.parent.name}/extract/, "
                    "затем перезапустите")
        print(f">> нарезка есть: {out.name}")
        return
    if p0 < 0:
        die(f"книжная стр. {book_a} даёт pdf-стр. {p0 + 1} < 1 — offset неверен")
    doc = pymupdf.open(src)
    if p1 >= doc.page_count:
        die(f"страница {p1 + 1} за пределами PDF ({doc.page_count} стр.) — "
            "проверьте --offset/\\origpages")
    part = pymupdf.open()
    part.insert_pdf(doc, from_page=p0, to_page=p1)
    out.parent.mkdir(parents=True, exist_ok=True)
    part.save(out)
    note = ""
    if probe:
        ok, detail = probe_slice(out, book_a, book_b)
        if ok is False:
            die(f"нарезка не совпадает с книжными страницами: {detail} — "
                "проверьте \\origpages главы и --offset")
        note = ("; folio подтверждены (" + detail + ")" if ok
                else "; ?? folio не извлекаются — сверка невозможна")
    print(f">> нарезано: книжные {book_a}–{book_b} = pdf {p0 + 1}–{p1 + 1} "
          f"-> {out.name}{note}")


def find_md(root):
    mds = sorted((p for p in root.rglob("*.md")
                  if ".clean" not in p.name and ".removed_" not in p.name
                  and ".prepared" not in p.name),
                 key=lambda p: p.stat().st_size, reverse=True)
    return mds[0] if mds else None


def extract(chpdf, outdir, mode, vram_limit, mineru_timeout=1800):
    md = find_md(outdir) if outdir.is_dir() else None
    if md:
        print(f">> извлечение есть: {md.relative_to(outdir)}")
        return md
    unload_llm()
    outdir.mkdir(parents=True, exist_ok=True)
    if mode == "math":
        rc = run_mineru(["-p", chpdf, "-o", outdir], outdir / "mineru.log",
                        timeout=mineru_timeout, vram_limit=vram_limit)
        if rc == TIMEOUT_RC:
            die("mineru завис (>--mineru-timeout) и был прерван — "
                "перезапустите ту же команду")
        if rc:
            die("извлечение упало")
    else:
        import os
        env = dict(os.environ)
        if vram_limit:
            env["MINERU_VIRTUAL_VRAM_SIZE"] = str(vram_limit)
        cmd = ["marker_single", chpdf, "--output_dir", outdir,
               "--output_format", "markdown"]
        if mode == "scan":
            cmd.append("--force_ocr")
        if run(cmd, env=env).returncode:
            die("извлечение упало")
        reap_mineru()
    md = find_md(outdir)
    if not md:
        die("извлечение не дало .md")
    return md


def stage_script(script, *args, capture=False):
    kw = dict(capture_output=True, text=True, encoding="utf-8") if capture else {}
    r = run([sys.executable, SCRIPTS / script, *args], **kw)
    out = ((r.stdout or "") + "\n" + (r.stderr or "")) if capture else ""
    if capture and out.strip():
        print(out.strip(), flush=True)
    if r.returncode:
        die(f"{script} завершился с ошибкой (код {r.returncode})")
    return out


def run_translate(cmd, dst):
    """translate.py с одним авторетраем при неполном переводе (код 4).

    Провальные блоки не кэшируются, поэтому второй проход стоит ровно их
    повторения и лечит транзиентные 500-ки сервера. Код 3 (сервер мёртв)
    не ретраим."""
    report = Path(str(dst) + ".report.json")
    for attempt in (1, 2):
        rc = run([sys.executable, SCRIPTS / "translate.py", *cmd]).returncode
        if rc in (0, 4):
            try:
                check_result(cmd[0], dst)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                die(f"missing/foreign translation report: {exc}")
        if rc == 0:
            return
        if rc == 4 and attempt == 1:
            print(">> перевод неполный (код 4) — второй проход по "
                  "непереведённым блокам (кэш цел)", flush=True)
            continue
        if rc == 4:
            kept, total = None, None
            try:
                rep = json.loads(report.read_text(encoding="utf-8"))
                kept, total = rep.get("kept_source"), rep.get("blocks_total")
            except (OSError, ValueError, KeyError):
                pass
            if untranslated_ok(kept, total):
                print(f"?? перевод: {kept} из {total} блоков остались "
                      f"исходником (<= допуска) — продолжаю (обычно имена/"
                      f"библиография); список: {report.name}", flush=True)
                return
            die(f"перевод не принят: {kept} из {total} блоков остались "
                f"исходником после повторного прохода (см. {report.name}) — "
                "чините сервер перевода и перезапустите ту же команду "
                "(кэш продолжит)")
        die(f"translate.py завершился с ошибкой (код {rc})"
            + (" — сервер перевода мёртв; кэш цел, перезапуск той же команды "
               "продолжит" if rc == 3 else ""))


def translate_chapter(work, md, a):
    """clean -> mask -> translate -> unmask; возвращает путь к ru.md."""
    from content_coverage import verify_cleaning_coverage, assert_no_control_chars
    ru = work / "chapter.ru.md"
    state_path = work / "chapter.state.json"
    def current_config():
        return canonical_hash({"mode": a.mode, "model": a.model, "host": a.host,
            "glossary": sha_file(a.glossary) if a.glossary else None,
            "glossary_profile": sha_file(a.glossary_profile) if getattr(a, "glossary_profile", None) else None,
            "split_chars": getattr(a, "split_chars", 0), "group_chars": getattr(a, "group_chars", 0),
            "context_per_request": getattr(a, "context_per_request", 0),
            "code": {name: sha_file(SCRIPTS / name) for name in (
                "translate.py", "mask_math.py", "clean_markdown.py", "content_coverage.py",
                "paragraph_units.py", "request_plan.py", "request_budget.py", "glossary_selection.py")}})
    config_hash = current_config()
    if ru.is_file() and state_path.is_file() and not a.force:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (state.get("source_sha256") == sha_file(md)
                and state.get("output_sha256") == sha_file(ru)
                and state.get("config_sha256") == config_hash):
            check_result(work / "chapter.masked.md", work / "chapter.ru.masked.md")
            verify_cleaning_coverage(md, work / "chapter.clean.md")
            if state.get("coverage_sha256") != sha_file(work / "chapter.clean.coverage.json"):
                die("chapter cleaning receipt changed; existing output retained")
            assert_no_control_chars(ru.read_text(encoding="utf-8"))
            if current_config() != config_hash:
                die("chapter translation settings/profile changed; existing output retained")
            return ru
    clean = work / "chapter.clean.md"
    masked = work / "chapter.masked.md"
    rumasked = work / "chapter.ru.masked.md"
    mp = work / "chapter.map.json"
    stage_script("clean_markdown.py", md, clean)
    verify_cleaning_coverage(md, clean)
    assert_markdown_structure(md.read_text(encoding="utf-8"), clean.read_text(encoding="utf-8"))
    stage_script("mask_math.py", "mask", clean, masked, mp)
    cmd = [masked, rumasked, "--mode", a.mode,
           "--model", a.model, "--host", a.host,
           "--parallel", str(a.parallel), "--cache-dir", work / ".tcache",
           "--split-chars", str(getattr(a, "split_chars", 0)),
           "--group-chars", str(getattr(a, "group_chars", 0)),
           "--context-per-request", str(getattr(a, "context_per_request", 0))]
    if getattr(a, "glossary_profile", None):
        cmd += ["--glossary-profile", a.glossary_profile]
    if a.glossary:
        cmd += ["--glossary", a.glossary]
    run_translate(cmd, rumasked)
    # приёмка: доля кириллицы в прозе; низкая = сервер падал и блоки остались
    # английскими («keeping source») — главу не принимаем, кэш сохранён
    txt = re.sub(r"⟦M\d{6}⟧", "", rumasked.read_text(encoding="utf-8"))
    cyr = len(re.findall(r"[А-Яа-яЁё]", txt))
    lat = len(re.findall(r"[A-Za-z]", txt))
    if cyr + lat > 500 and cyr / (cyr + lat) < 0.5:
        die(f"перевод неполный: кириллицы лишь {100 * cyr // (cyr + lat)}% — "
            "видимо, сервер перевода падал; перезапустите главу (кэш продолжит)")
    out = stage_script("mask_math.py", "unmask", rumasked, ru, mp, capture=True)
    unres = parse_unresolved(out)
    if unres is None:
        die("missing unmask acceptance result; chapter is not approved")
    elif unres > 0:
        die(f"unmask: unresolved={unres} — формулы потеряны, глава не принята")
    report = check_result(masked, rumasked)
    if report["status"] != "passed":
        die("chapter is partial: candidate retained; skeleton not updated")
    assert_markdown_structure(clean.read_text(encoding="utf-8"), ru.read_text(encoding="utf-8"))
    verify_cleaning_coverage(md, clean)
    assert_no_control_chars(ru.read_text(encoding="utf-8"))
    if current_config() != config_hash:
        die("chapter translation settings/profile changed; skeleton not updated")
    write_json(state_path, {"source_sha256": sha_file(md), "output_sha256": sha_file(ru),
                            "config_sha256": config_hash,
                            "coverage_sha256": sha_file(clean.with_suffix(".coverage.json"))})
    return ru


# ------------------------------------------------------- разрезка перевода
def split_ru_md(text):
    """Тела разделов по родным номерам в заголовках; intro — всё до первого.
    Границей считается ЛЮБОЙ нумерованный заголовок: раздел без пары в
    каркасе уйдёт в leftover.md, а не приклеится молча к предыдущему."""
    bounds = []
    for m in re.finditer(r"^\s{0,3}#{1,6}\s*[*_ ]*([A-Z0-9]+(?:\.[0-9]+)+)\)?\.?[ \t]",
                         text, re.M):
        num = m.group(1).rstrip(".")
        bounds.append((m.start(), m.end() - 1, num))
    parts, intro, dup_nums = {}, text[: bounds[0][0]] if bounds else text, []
    for i, (s, e, num) in enumerate(bounds):
        nxt = bounds[i + 1][0] if i + 1 < len(bounds) else len(text)
        body = text[text.find("\n", e) + 1: nxt].strip()
        if num not in parts:                       # первое вхождение главнее
            parts[num] = body
        else:
            dup_nums.append(num)                   # но молчать об этом нельзя
    # из intro выбрасываем заголовок главы, если он там один
    intro = re.sub(r"^\s{0,3}#{1,6}[^\n]*\n", "", intro.strip(), count=1).strip()
    return intro, parts, dup_nums


def find_repeated_subheads(body, min_len=8, min_count=2):
    """Повторы одинаковых подзаголовков внутри ОДНОГО тела раздела: признак
    дубля извлечения, который не поймала санация (после перевода копии
    расходятся). Боевой случай: 15.2 Тейлора — вторая копия начинается с
    НЕнумерованных подзаголовков, поэтому дедуп по номерам её не видит.
    Короткие заголовки («Пример», «Итоги») законно повторяются — не судим."""
    seen = {}
    for m in re.finditer(r"^\s{0,3}#{1,6}\s*(.+?)\s*$", body, re.M):
        h = " ".join(m.group(1).split()).casefold()
        if len(h) >= min_len:
            seen[h] = seen.get(h, 0) + 1
    return [(h, n) for h, n in seen.items() if n >= min_count]


def carry_images(body, md_dir, figdir, relpfx, prefix):
    """Копирует картинки раздела в figures/ и переписывает пути в md."""
    missing = []

    def repl(m):
        alt, src = m.group(1), m.group(2)
        p = (md_dir / src).resolve()
        if not p.is_file():
            missing.append(src)
            return ""                              # битую ссылку не тащим в LaTeX
        figdir.mkdir(parents=True, exist_ok=True)
        dst = figdir / f"{prefix}_{p.name}"
        if not dst.is_file():
            shutil.copy2(p, dst)
        return f"![{alt}]({relpfx}/{dst.name})"

    body = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", repl, body)
    return body, missing


def md_to_fragment(body, work, tag):
    src = work / f"frag_{tag}.md"
    dst = work / f"frag_{tag}.tex"
    src.write_text(body + "\n", encoding="utf-8")
    r = run(["pandoc", src, "-f", "markdown+raw_tex", "-t", "latex",
             "--shift-heading-level-by=1", "--top-level-division=section",
             "-o", dst])
    if r.returncode:
        die(f"pandoc упал на фрагменте {tag}")
    tex = dst.read_text(encoding="utf-8")
    assert_typeset_structure(body, tex)
    # alt={...} понимает не всякий graphicx; \pandocbounded есть только в
    # шаблоне pandoc — заменяем ограничителем ширины на чистом graphicx
    tex = re.sub(r",?\s*alt=\{[^{}]*\}", "", tex)
    tex = re.sub(
        r"\\pandocbounded\{((?:[^{}]|\{[^{}]*\})*)\}",
        lambda m: (r"\resizebox{\ifdim\width>\linewidth\linewidth"
                   r"\else\width\fi}{!}{" + m.group(1) + "}"),
        tex)
    tex = tex.replace("\\tightlist\n", "").replace("\\tightlist", "")
    # маркеры rescue_plaques -> рамка на чистом ядре LaTeX (преамбула чужая,
    # никаких определений не требуется)
    tex = tex.replace("\\plaquebegin",
                      "\\par\\medskip\\noindent\\hrule\\smallskip"
                      "\\begin{quote}\\itshape")
    tex = tex.replace("\\plaqueend",
                      "\\end{quote}\\smallskip\\hrule\\medskip\\par")
    dst.write_text(tex, encoding="utf-8")     # на диске — то же, что вставляем
    return tex.strip()


def insert_fragments(stub, frags, intro_tex, stamp):
    """Заменяет маркеры телами разделов; intro — после \\pending/\\chapter."""
    lines = stub["lines"][:]
    filled, left = [], []
    for sec in stub["sections"]:
        num = sec["num"]
        if num and num in frags:
            lines[sec["line"]] = (f"% [skeleton-orchestrator {stamp}] раздел {num}\n"
                                  + frags[num])
            filled.append(num)
        else:
            left.append(num or "(без номера)")
    body = "\n".join(lines)
    if intro_tex and INTRO_SENTINEL not in body:
        anchor = re.search(r"^\\pending\s*$", body, re.M) \
            or re.search(r"^\\chapter\{[^}]*\}.*$", body, re.M)
        if anchor:
            pos = body.find("\n", anchor.end())
            body = (body[:pos + 1] + f"\n{INTRO_SENTINEL} {stamp}\n"
                    + intro_tex + "\n" + body[pos + 1:])
    if not any(MARKER_RE.match(ln) for ln in body.splitlines()):
        body = re.sub(r"^\\pending\s*$\n?", "", body, flags=re.M)
    bak = stub["path"].with_suffix(".tex.orig")
    if not bak.exists():
        shutil.copy2(stub["path"], bak)
    stub["path"].write_text(body + ("\n" if not body.endswith("\n") else ""),
                            encoding="utf-8")
    return filled, left


# --------------------------------------------------------------------- build
def build(main_tex, engine):
    main_tex = Path(main_tex).resolve()
    base = main_tex.parent / ".build-candidates"
    base.mkdir(exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix="candidate-", dir=base))
    result = compile_latex(main_tex, candidate, engine=engine)
    pdf = candidate / (main_tex.stem + ".pdf")
    report = {"schema": 1, "profile": "release", "status": "failed",
              "compile": result, "visual_review": "not_run", "semantic_review": "not_run",
              "scope": "compiled skeleton; existing chapter content not retranslated"}
    if result["status"] == "passed":
        technical = inspect_pdf(pdf, candidate / (main_tex.stem + ".log"),
                                require_toc=r"\tableofcontents" in main_tex.read_text(encoding="utf-8"),
                                source_text=main_tex.read_text(encoding="utf-8"))
        report.update(technical=technical, status=technical["status"], pdf_sha256=sha_file(pdf))
    write_json(candidate / "release.report.json", report)
    if report["status"] == "passed":
        publish_candidate(pdf, main_tex.with_suffix(".pdf"), report)
        return True, main_tex.with_suffix(".pdf")
    print(f"!! candidate failed; previous PDF untouched: {candidate}")
    return False, pdf


def main():
    ap = argparse.ArgumentParser(
        description="перевод книги в существующий LaTeX-каркас")
    ap.add_argument("project", help="папка проекта с каркасом")
    ap.add_argument("--source", default=None, help="исходный PDF (по умолчанию "
                    "крупнейший в корне проекта)")
    ap.add_argument("--offset", type=int, default=None,
                    help="pdf_страница = книжная + offset (запасной вход: при "
                         "--folio auto главы находятся по печатным номерам "
                         "страниц и offset не обязателен)")
    ap.add_argument("--folio", choices=["auto", "offset", "off"],
                    default="auto",
                    help="локация глав по печатным номерам страниц (folio): "
                         "auto — искать каждую главу независимо, --offset как "
                         "fallback; offset — только --offset, но с контрольным "
                         "зондом среза; off — без folio-логики вовсе")
    ap.add_argument("--chapters", default=None,
                    help="какие главы: ch01,ch03 (по умолчанию все с маркерами)")
    ap.add_argument("--extract", choices=["auto", "digital", "scan", "math"],
                    default="math")
    ap.add_argument("--model", default="hy-mt2-7b")
    ap.add_argument("--host", default="http://localhost:1234")
    ap.add_argument("--mode", choices=["hymt", "generic"], default="hymt")
    ap.add_argument("--glossary", default=None,
                    help="фид en => ru (по умолчанию <project>/glossary.md, если есть)")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--split-chars", type=int, default=0)
    ap.add_argument("--group-chars", type=int, default=0)
    ap.add_argument("--context-per-request", type=int, default=0)
    ap.add_argument("--glossary-profile", default=None)
    ap.add_argument("--vram-limit", type=int, default=0)
    ap.add_argument("--engine", choices=["auto", "lualatex", "xelatex"],
                    default="auto")
    ap.add_argument("--figures-rel", default=None, help="путь к рисункам главы "
                    "относительно папки main.tex (по умолчанию ../figures/<глава>)")
    ap.add_argument("--mineru-timeout", type=int, default=1800,
                    help="таймаут одного вызова mineru, сек (0 = без "
                         "таймаута); rc=124 = зависание VLM")
    ap.add_argument("--no-lock", action="store_true",
                    help="не брать глобальный замок конвейера")
    ap.add_argument("--no-intro", action="store_true",
                    help="не вставлять текст главы до первого раздела")
    ap.add_argument("--no-plaques", action="store_true",
                    help="не спасать текстовые плашки, уехавшие картинками")
    ap.add_argument("--no-dup-check", action="store_true",
                    help="не браковать главу при повторе подзаголовков "
                         "(книги с законно повторяющейся структурой)")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="переделать перевод главы заново (кэш остаётся)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать план и ничего не менять")
    a = ap.parse_args()

    project = Path(a.project).resolve()
    if not project.is_dir():
        die(f"нет такой папки: {project}")
    main_tex = find_main_tex(project)
    engine = detect_engine(main_tex, a.engine)
    stubs = [parse_stub(p) for p in chapter_files(main_tex)]
    if a.chapters:
        keys = [k.strip().lower() for k in a.chapters.split(",") if k.strip()]
        stubs = [s for s in stubs if any(k in s["path"].stem.lower() for k in keys)]
    todo = [s for s in stubs if s["sections"]]
    done = [s for s in stubs if not s["sections"]]

    if a.glossary is None and (project / "glossary.md").is_file():
        a.glossary = str(project / "glossary.md")
        print(f">> глоссарий проекта: {a.glossary}")

    print(f">> каркас: {main_tex} (движок: {engine})")
    print(f">> глав с маркерами: {len(todo)}; уже заполнено: "
          f"{', '.join(s['path'].stem for s in done) or '—'}")
    if not todo:
        print(">> вставлять нечего — все маркеры уже заполнены")
        if not a.no_build and not a.dry_run:
            ok, pdf = build(main_tex, engine)
            print((">> собрано: " + str(pdf)) if ok else "!! сборка не прошла")
        return

    if a.dry_run:
        fmap = None
        if a.folio == "auto":
            try:
                src_dry = find_source_pdf(project, a.source)
                fmap = load_folio_map(src_dry, project / "_skeleton_work")
                print(f">> folio-карта: {len(fmap)} достоверных печатных номеров")
            except (SystemExit, Exception):   # noqa: B014 — dry-run мягкий
                pass
        for s in todo:
            nums = [x["num"] or "?" for x in s["sections"]]
            pg = (f"книжные стр. {s['pages'][0]}–{s['pages'][1]}"
                  + (f" = pdf {s['pages'][0] + a.offset}–{s['pages'][1] + a.offset}"
                     if a.offset is not None else "")) if s["pages"] else "нет \\origpages!"
            if fmap and s["pages"]:
                try:
                    loc = locate_chapter(fmap, *s["pages"], s["path"].stem)
                    pg += (f" | folio: pdf {loc[0] + 1}–{loc[1] + 1}, "
                           f"eff.offset {loc[2]}" if loc
                           else " | ?? достоверных folio в диапазоне нет")
                except RuntimeError as e:
                    pg += f" | ?? {e}"
            print(f"   {s['path'].stem}: {len(nums)} разделов "
                  f"({', '.join(nums[:8])}{'…' if len(nums) > 8 else ''}), {pg}")
        print(">> dry-run: ничего не изменено")
        return

    # глобальный замок: GPU один, параллельные прогоны душат друг друга,
    # а reap_mineru() добивает mineru по всей машине
    if not a.no_lock:
        PipelineLock(note=f"skeleton:{project.name}").acquire()

    workroot = project / "_skeleton_work"
    # Revalidate cached chapters too; existence alone is not stage completion.
    need_translate = bool(todo)
    need_extract = a.force or any(not (workroot / s["path"].stem / "extraction.state.json").is_file()
                                 for s in todo)
    src = find_source_pdf(project, a.source)
    source_hash = sha_file(src)
    preflight(a, need_translate, need_extract, engine)

    # локация глав по печатным номерам страниц (см. блок folio выше)
    fmap = None
    if need_extract and a.folio == "auto":
        fmap = load_folio_map(src, workroot)
        if fmap:
            print(f">> folio-карта: {len(fmap)} достоверных печатных номеров")
        else:
            print("?? folio не извлекаются из PDF (нет текстового слоя или "
                  "страницы без номеров) — локация глав по печатным номерам "
                  "невозможна", flush=True)
            if a.offset is None:
                die("задайте --offset N (pdf = книжная + N) или --folio off")

    stamp = datetime.date.today().isoformat()
    report, any_fail = [], False
    for s in todo:
        ch = s["path"].stem
        work = workroot / ch
        work.mkdir(parents=True, exist_ok=True)
        print(f"\n===== {ch}: {len(s['sections'])} разделов =====", flush=True)
        try:
            if not s["pages"]:
                raise RuntimeError("в заглушке нет \\origpages — нечего нарезать")
            exdir = work / "extract"
            src_md = find_md(exdir) if exdir.is_dir() else None
            extract_state = work / "extraction.state.json"
            extraction_config = canonical_hash({"source": source_hash, "pages": s["pages"],
                "mode": a.extract, "offset": a.offset, "folio": a.folio})
            if not a.force and extract_state.exists():
                state = json.loads(extract_state.read_text(encoding="utf-8"))
                from provenance import inside
                src_md = inside(work, state["markdown"])
                exdir = src_md.parent
                if (state.get("config_sha256") != extraction_config
                        or sha_file(src_md) != state.get("output_sha256")):
                    raise RuntimeError("extraction identity changed; use --force (old extraction retained)")
            elif not a.force and src_md:
                raise RuntimeError("legacy extraction without identity; use --force or a separate project")
            if a.force:
                exdir = Path(tempfile.mkdtemp(prefix="extract-", dir=work))
                src_md = None
            ru = work / "chapter.ru.md"
            if need_translate:
                if not src_md:
                    exdir.mkdir(parents=True, exist_ok=True)
                    chpdf = exdir / "pages.pdf"
                    book_a, book_b = s["pages"]
                    npages = book_b - book_a + 1
                    if npages > MINERU_WINDOW:
                        print(f"?? {ch}: {npages} книжных стр. > "
                              f"{MINERU_WINDOW} (окно гибридного бэкенда "
                              f"MinerU) — извлечение пойдёт в "
                              f"{-(-npages // MINERU_WINDOW)} окна; на стыке "
                              "возможен дубль контента (точный снимет "
                              "санация — проверьте .removed_dup.md)",
                              flush=True)
                    p0 = p1 = None
                    if fmap:
                        loc = locate_chapter(fmap, book_a, book_b, ch)
                        if loc:
                            p0, p1, eff = loc
                            if a.offset is not None and eff != a.offset:
                                print(f">> {ch}: фактический офсет {eff} "
                                      f"(задан {a.offset})", flush=True)
                        else:
                            print(f"?? {ch}: достоверных folio в диапазоне "
                                  f"{book_a}–{book_b} нет — беру --offset",
                                  flush=True)
                    if p0 is None:
                        if a.offset is None:
                            raise RuntimeError(
                                "нет folio и нет --offset — главу не разместить")
                        p0 = book_a + a.offset - 1
                        p1 = book_b + a.offset - 1
                    slice_pdf(src, book_a, book_b, p0, p1, chpdf,
                              probe=(a.folio != "off"))
                    src_md = extract(chpdf, exdir, a.extract, a.vram_limit,
                                     a.mineru_timeout)
                    write_json(extract_state, {"config_sha256": extraction_config,
                        "markdown": src_md.relative_to(work).as_posix(), "output_sha256": sha_file(src_md)})
                else:
                    print(f">> извлечение есть: {src_md.relative_to(exdir)}")
                if a.extract == "math":
                    src_md = prepare(src_md)
                if not a.no_plaques:
                    # детекция текстовых плашек, уехавших картинками (только
                    # отчёт; восстановление — vision-агентом, см. АГЕНТ.md)
                    run([sys.executable, SCRIPTS / "rescue_plaques.py", "detect",
                         src_md, "--json", work / "plaques_candidates.json",
                         "--report", work / "plaques_report.md"])
                ru = translate_chapter(work, src_md, a)

            # пути картинок в md относительны каталогу ИЗВЛЕЧЁННОГО md
            img_base = src_md.parent if src_md else ru.parent
            intro, parts, dup_nums = split_ru_md(ru.read_text(encoding="utf-8"))
            if dup_nums:
                print(f"?? {ch}: разделы {', '.join(sorted(set(dup_nums)))} "
                      "встречаются в переводе дважды — вторая копия отброшена "
                      "(первое вхождение главнее); вероятен дубль извлечения "
                      "на стыке окон MinerU", flush=True)
            if not a.no_dup_check:
                for num, body in parts.items():
                    rep = find_repeated_subheads(body)
                    if rep:
                        h, cnt = rep[0]
                        raise RuntimeError(
                            f"повтор подзаголовка «{h}» x{cnt} в теле раздела "
                            f"{num} — похоже на дубль извлечения (стык "
                            f"{MINERU_WINDOW}-страничных окон MinerU); точный "
                            "дубль снимает санация (strip-dup-runs), неточный "
                            f"правится в {ru.name}; обход: --no-dup-check")
            relpfx = a.figures_rel or f"../figures/{ch}"
            figdir = (main_tex.parent / relpfx).resolve()
            frags, lost_imgs = {}, []
            for num, body in parts.items():
                body, miss = carry_images(body, img_base, figdir, relpfx,
                                          ch)
                lost_imgs += miss
                frags[num] = md_to_fragment(body, work, num.replace(".", "_"))
            intro_tex = ""
            if intro and not a.no_intro:
                intro, miss = carry_images(intro, img_base, figdir, relpfx, ch)
                lost_imgs += miss
                intro_tex = md_to_fragment(intro, work, "intro")

            filled, left = insert_fragments(s, frags, intro_tex, stamp)
            unmatched = sorted(set(parts) - set(filled))
            if unmatched:
                lf = work / "leftover.md"
                lf.write_text("\n\n".join(f"## {n}\n\n{parts[n]}"
                                          for n in unmatched), encoding="utf-8")
            report.append((ch, "заполнена",
                           f"{len(filled)}/{len(s['sections'])} разделов"
                           + (f"; без пары: {', '.join(left)}" if left else "")
                           + (f"; лишние в leftover.md: {len(unmatched)}"
                              if unmatched else "")
                           + (f"; картинок не найдено: {len(lost_imgs)}"
                              if lost_imgs else "")))
        except SystemExit:
            raise
        except Exception as e:
            any_fail = True
            report.append((ch, "ОШИБКА", str(e)))
            print(f"!! {ch}: {e} — продолжаю со следующей главой", flush=True)

    built = None
    if not a.no_build:
        ok, pdf = build(main_tex, engine)
        built = pdf if ok else None
        any_fail = any_fail or not ok

    print("\n" + "=" * 60)
    for ch, st, note in report:
        print(f"{ch:12s} {st:10s} {note}")
    print(f"сборка: {'OK -> ' + str(built) if built else ('пропущена' if a.no_build else 'НЕ ПРОШЛА — чините по ' + str(main_tex.with_suffix('.log')))}")
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
