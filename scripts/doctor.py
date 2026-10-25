#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Доктор окружения: готова ли эта машина гонять конвейер.

Первое, что стоит запустить после распаковки. Каждая проверка либо говорит
«готово», либо называет ОДНУ команду, которая её чинит. Молчаливых «чего-то
не хватает» здесь нет намеренно: почти все дорогие поломки этого конвейера
начинались с того, что стадия падала глубоко внутри, а причина была в
неустановленной программе.

    python scripts/doctor.py            полная проверка (10-40 с)
    python scripts/doctor.py --quick    без пробной сборки LaTeX и запроса к модели

Коды возврата:
    0 — можно работать;
    1 — не хватает обязательного, не поедет вообще;
    2 — обязательное на месте, но часть маршрутов недоступна.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import (ROOT, lms, mineru, marker, pandoc, tesseract,  # noqa: E402
                   venv_python, xelatex)
from endpoint_policy import model_opener
from tex_logs import read_tex_log
OPENER = model_opener()

OK, WARN, FAIL = "OK  ", "ЧАСТЬ", "НЕТ "
rows = []           # (состояние, что проверяли, подробность, как чинить)


def add(state, what, detail="", fix=""):
    rows.append((state, what, detail, fix))
    return state


# --------------------------------------------------------------- python

def check_python():
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 10):
        return add(FAIL, "Python", f"{ver} — нужен 3.10+",
                   "поставьте Python 3.10+ с python.org (НЕ из Microsoft Store)")
    # Заглушка Microsoft Store живёт в WindowsApps и при запуске открывает
    # магазин вместо интерпретатора: падает с кодом 49 и ничего не печатает.
    if "WindowsApps" in sys.executable:
        return add(FAIL, "Python", f"{ver}, но это заглушка Microsoft Store",
                   "поставьте настоящий Python с python.org и пересоздайте venv")
    return add(OK, "Python", ver)


def check_venv():
    want = venv_python()
    here = Path(sys.executable).resolve()
    if not (ROOT / "venv").exists() and not (ROOT / ".venv").exists():
        return add(FAIL, "venv конвейера", "не создан",
                   f'python -m venv "{ROOT / "venv"}"  затем pip install -r requirements.txt')
    if Path(want).resolve() != here:
        return add(WARN, "venv конвейера", f"есть, но запущен другой python: {here}",
                   f'запускайте так: "{want}" scripts/doctor.py')
    return add(OK, "venv конвейера", str(Path(want).parent.parent))


# --------------------------------------------------------------- пакеты

REQUIRED = [("fitz", "pymupdf", "чтение PDF, метрики, превью — нужен везде"),
            ("psutil", "psutil", "диспетчер: идентичность и дерево процессов")]
OPTIONAL = [
    ("PIL", "pillow", "перекодировка и обрезка картинок"),
    ("cv2", "opencv-python", "вырезание чертежей (cut_figures.py)"),
    ("pymupdf4llm", "pymupdf4llm", "режим --extract digital"),
    ("pytest", "pytest", "прогон тестов"),
]


def check_packages():
    import importlib.util
    miss_req, miss_opt = [], []
    for mod, pkg, why in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            miss_req.append((pkg, why))
    for mod, pkg, why in OPTIONAL:
        if importlib.util.find_spec(mod) is None:
            miss_opt.append((pkg, why))
    if miss_req:
        add(FAIL, "пакеты python",
            "нет: " + ", ".join(p for p, _ in miss_req),
            "pip install -r requirements.txt")
    else:
        add(OK, "пакеты python", "обязательные на месте")
    for pkg, why in miss_opt:
        add(WARN, f"пакет {pkg}", why, f"pip install {pkg}")
    return FAIL if miss_req else OK


# --------------------------------------------------------- внешние программы

def _version(exe, args=("--version",), timeout=25):
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode != 0:
            return None
        line = (r.stdout or r.stderr or "").strip().splitlines()
        return line[0][:60] if line else None
    except (OSError, subprocess.SubprocessError):
        return None


def check_pandoc():
    exe = pandoc()
    v = _version(exe)
    if v is None:
        return add(FAIL, "pandoc", "не найден",
                   "поставьте с pandoc.org и перезапустите терминал")
    return add(OK, "pandoc", v)


def check_xelatex(quick):
    exe = xelatex()
    v = _version(exe)
    if v is None:
        return add(FAIL, "xelatex", "не найден",
                   "поставьте MiKTeX (miktex.org) или TeX Live и перезапустите терминал")
    if quick:
        return add(OK, "xelatex", v + "  (пробная сборка пропущена, --quick)")
    return check_latex_build(exe, v)


def check_latex_build(exe, ver):
    """Собрать крошечный документ на боевой преамбуле.

    Именно это ловит самую частую беду свежей установки MiKTeX: xelatex есть,
    но кириллических шрифтов нет, и книга собирается с пустыми местами вместо
    русского текста. В логе это «Missing character», в PDF — ничего.
    """
    pre = ROOT / "assets" / "preamble.tex"
    if not pre.is_file():
        return add(WARN, "xelatex", ver + f"  (нет {pre}, сборка не проверена)")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "main.tex").write_text(
            pre.read_text(encoding="utf-8")
            + "\n\\begin{document}\nРусский текст и формула "
              "$\\alpha^2 + \\beta^2 = \\gamma^2$.\n\\end{document}\n",
            encoding="utf-8")
        try:
            result = subprocess.run([exe, "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                           cwd=d, capture_output=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            return add(FAIL, "xelatex собирает", str(e)[:60],
                       "проверьте установку TeX")
        log = (d / "main.log")
        text = read_tex_log(log) if log.exists() else ""
        missing = text.count("Missing character")
        if result.returncode != 0 or re.search(r"^!", text, re.M) or not (d / "main.pdf").exists():
            head = [ln for ln in text.splitlines() if ln.startswith("!")][:1]
            return add(FAIL, "xelatex собирает", f"сборка не принята (exit {result.returncode}). "
                       + (head[0][:70] if head else "PDF отсутствует или сборка завершилась с ошибкой"),
                       "в MiKTeX разрешите доустановку пакетов (Always install "
                       "missing packages on-the-fly) и повторите")
        if missing:
            return add(FAIL, "xelatex собирает",
                       f"PDF есть, но {missing} пропавших глифов — нет "
                       "кириллических шрифтов",
                       "поставьте пакет cm-unicode (MiKTeX Console -> Packages "
                       "-> cm-unicode) или смените \\setmainfont в assets/preamble.tex")
        try:
            import fitz
            with fitz.open(d / "main.pdf") as pdf:
                if not len(pdf) or "Русский текст" not in " ".join(p.get_text() for p in pdf):
                    return add(FAIL, "xelatex собирает", "нет читаемого русского текста в пробном PDF")
        except Exception as exc:
            return add(FAIL, "xelatex собирает", "PDF не читается: " + str(exc)[:80])
        return add(OK, "xelatex собирает", "русский текст и формулы печатаются")


def check_tesseract():
    exe = tesseract()
    v = _version(exe)
    if v is None:
        return add(WARN, "tesseract", "не найден — маршрут сканов фрактуры недоступен",
                   "github.com/UB-Mannheim/tesseract/wiki, затем модель deu_latf")
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        langs = {ln.strip() for ln in (r.stdout or "").splitlines()}
    except (OSError, subprocess.SubprocessError):
        langs = set()
    if "deu_latf" not in langs:
        have = "deu_frak" in langs
        return add(WARN, "tesseract", v + " — НЕТ модели deu_latf"
                   + (" (есть только устаревшая deu_frak)" if have else ""),
                   "скачайте deu_latf.traineddata из tesseract-ocr/tessdata_best "
                   "в папку tessdata: на фрактуре она даёт 400 терминов книги "
                   "против 316 у deu_frak")
    return add(OK, "tesseract", v + ", модель deu_latf на месте")


def check_mineru():
    exe = mineru()
    if not (Path(exe).is_file() or shutil.which(exe)):
        return add(WARN, "mineru", "не найден — born-digital книги с формулами "
                   "придётся извлекать иначе",
                   'pip install "mineru[core]"  (тянет torch, несколько ГБ)')
    return add(OK, "mineru", exe)


# ------------------------------------------------------------- LM Studio

def check_extractor(mode):
    """Selected PDF route is required; do not require OCR for a Markdown test."""
    if mode == "none":
        return add(OK, "извлечение", "не требуется для выбранной проверки (none)")
    if mode == "digital":
        import importlib.util
        found = importlib.util.find_spec("pymupdf4llm") is not None
        return add(OK if found else FAIL, "извлечение digital", "pymupdf4llm" if found else "не установлен",
                   "" if found else "venv\\Scripts\\python.exe -m pip install pymupdf4llm")
    exe = mineru() if mode == "math" else marker()
    found = Path(exe).is_file() or shutil.which(exe)
    return add(OK if found else FAIL, "извлечение " + mode, exe if found else "движок не найден",
               "" if found else ('pip install "mineru[core]"' if mode == "math" else "pip install marker-pdf"))

def check_lm_studio(quick, host="http://localhost:1234", model=None):
    base = host.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")]
    url = base + ("/models" if re.search(r"/(v\d+[a-z]*|openai)$", base) else "/v1/models")
    try:
        with OPENER.open(url, timeout=10) as r:
            data = json.load(r)
        ids = [m["id"] for m in data["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return add(FAIL, "сервер модели", f"сервер не отвечает корректно на {host}",
                   "проверьте URL и включите локальный OpenAI-совместимый сервер")
    if not ids or (model and model not in ids):
        return add(FAIL, "сервер модели", "нужная модель отсутствует: " + (model or "список пуст"),
                   "скачайте/подключите модель и укажите её точный ID из /v1/models")
    return add(OK, "сервер модели", "API доступен; ID: " + (model or ", ".join(ids[:3]))
               + ". Генерация и вместимость контекста ещё не проверены.")


def check_context_budget(quick):
    """Optional LM Studio inspection; never infer context/parallel capacity."""
    exe = lms()
    if not (Path(exe).is_file() or shutil.which(exe)):
        return add(WARN, "контекст модели", "нет lms CLI — бюджет не проверить",
                   "поставьте CLI LM Studio (в приложении: Developer -> CLI)")
    try:
        r = subprocess.run([exe, "ps", "--json"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        loaded = json.loads(r.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return add(WARN, "контекст модели", "lms ps не ответил")
    if not loaded:
        return add(WARN, "контекст модели", "модель не загружена (JIT загрузит "
                   "её при первом запросе — это до 6 минут молчания)",
                   "lms load hy-mt2-7b -c 16384 --parallel 3 -y")
    m = loaded[0]
    ctx = int(m.get("contextLength") or 0)
    par = int(m.get("parallel") or 1)
    return add(WARN, "контекст модели", f"runtime сообщает context={ctx}, parallel={par}; "
               "вместимость конкретного запроса не проверена")


def check_gpu():
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return add(WARN, "видеокарта", "nvidia-smi не найден; GPU-режим не проверен")
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return add(WARN, "видеокарта", "не определена")
    return add(OK, "видеокарта", line[0].strip())


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="без пробной сборки LaTeX и без опроса модели")
    ap.add_argument("--host", default="http://localhost:1234")
    ap.add_argument("--model", help="точный ID модели из настроек задания")
    ap.add_argument("--extract", choices=("none", "auto", "scan", "math", "digital"), default="none",
                    help="выбранный PDF-движок обязателен; none для Markdown/EPUB и базовой установки")
    a = ap.parse_args()
    rows.clear()

    print(f"== доктор конвейера ==  корень: {ROOT}\n")
    check_python()
    check_venv()
    check_packages()
    check_pandoc()
    check_xelatex(a.quick)
    check_tesseract()
    check_mineru()
    check_extractor(a.extract)
    if a.quick:
        add(WARN, "LM Studio", "не опрошен (--quick)")
    else:
        check_lm_studio(a.quick, a.host, a.model)
    check_gpu()

    width = max(len(w) for _, w, _, _ in rows)
    for state, what, detail, fix in rows:
        print(f"  {state}  {what:{width}s}  {detail}")
        if fix:
            print(f"        └─ {fix}")

    n_fail = sum(1 for s, *_ in rows if s == FAIL)
    n_warn = sum(1 for s, *_ in rows if s == WARN)
    print()
    if n_fail:
        print(f"ВЕРДИКТ: не поедет — не хватает обязательного ({n_fail}). "
              "Почините строки «НЕТ» сверху вниз.")
        return 1
    if n_warn:
        print(f"ВЕРДИКТ: обязательные проверки пройдены; предупреждений/пропусков: {n_warn}. "
              "Смотрите строки «ЧАСТЬ»; готовность непроверенных стадий не подтверждена.")
        return 2
    print("ВЕРДИКТ: выбранные проверки пройдены; выполните пробный перевод.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
