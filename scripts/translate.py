#!/usr/bin/env python3
"""Translate a masked markdown file EN->RU via any OpenAI-compatible endpoint
(LM Studio, Ollama, llama.cpp server, vLLM ...).

Prompt modes:
  --mode hymt     Fixed Hunyuan/Hy-MT2 translation prompt ("Translate the following
                  segment into <lang>, without additional explanation.") for the
                  Tencent translation specialists. Optional glossary and the
                  placeholder rule are appended as extra translation instructions.
  --mode generic  Full rule set as a system prompt -- for general LLMs (Qwen3.5,
                  Gemma, abliterated variants) used as translators or for a
                  literary polish pass.

Shared behaviour: paragraph-block chunking, pass-through of placeholder-only
blocks, per-block cache (safe to re-run / resumes after interruption), retries.

Examples:
  # LM Studio + Hy-MT2-7B  (Developer tab -> Start Server, default port 1234)
  python translate.py in.masked.md out.ru.masked.md \
      --mode hymt --model hy-mt2-7b --host http://localhost:1234 --glossary glossary.md

  # Ollama + Qwen3.5 (OpenAI-compatible endpoint on port 11434)
  python translate.py in.masked.md out.ru.masked.md \
      --mode generic --model qwen3.5:9b --host http://localhost:11434

Notes:
  * --model must match the id the server reports (check GET <host>/v1/models).
  * top_k / repeat-penalty are not standard OpenAI fields: set them in the
    LM Studio model-load sidebar (Hy-MT2 recommended: top_k 20, repeat 1.05).
"""
import argparse
import concurrent.futures
import difflib
import hashlib
import http.client
import threading
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from collections import Counter
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit, urlunsplit

from glossary_selection import load_profile, select_glossary
from request_plan import PLANNER_VERSION, plan_fragments
from request_budget import check_messages
from managed_model_guard import check_managed_model, ManagedModelError
from attempt_journal import AttemptJournal, current_attempt
import paragraph_units as paragraphs
from provenance import atomic_text, canonical_hash, sha_file, sha_text, write_json

print(">> translate.py v4 (unified validator, v5 cache, provenance, shared request plan)",
      flush=True)

from endpoint_policy import model_opener
OPENER = model_opener()

PLACEHOLDER_RE = re.compile(r"\u27e6M\d{6}\u27e7")


def has_damaged_tokens(reply):
    """True when any M###### appears outside an intact \u27e6M######\u27e7 token.

    Models occasionally corrupt the placeholder brackets (\u27e8M001705\u27e9,
    \u2039M003223\u203a, \u25a6M009806\u25e6 ...): the exact unmask would then silently skip
    them and the formula would vanish from the book.
    """
    return bool(re.search(r"M\d{6}", PLACEHOLDER_RE.sub("", reply)))


# Ровно то, что чинит восстановительный проход mask_math.unmask: M###### с
# одним небуквенным соседом с каждой стороны (⟨M001705⟩, ‹M003223›, ▦M009806◦).
RECOVERABLE_RE = re.compile(r"[^\w\s]?M\d{6}[^\w\s]?")


def damage_is_recoverable(reply):
    """True, если ВСЕ битые маркеры ответа починит unmask.

    Три попытки на битом маркере кончались тем, что блок оставался
    английским (на NFCM так потерялись 3 блока из 10013). Но unmask умеет
    восстанавливать маркер, у которого модель испортила только скобки —
    цифры-то целы. Значит терять блок не за чем: если после исчерпания
    попыток порча ИМЕННО такая, ответ можно принять.
    """
    left = PLACEHOLDER_RE.sub("", reply)
    if not re.search(r"M\d{6}", left):
        return True
    return not re.search(r"M\d{6}", RECOVERABLE_RE.sub("", left))


CJK_RE = re.compile(
    "[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\u1100-\u11ff\uac00-\ud7af]")   # +хангыль: «시작» доехало до PDF тома 6
CJK_TARGETS = {"chinese", "japanese", "korean", "mandarin", "cantonese",
               "zh", "ja", "ko"}


def has_cjk(reply):
    """Hy-MT2's native script occasionally leaks into non-CJK targets
    (证明, 与 ...) — such replies must be resampled, never cached."""
    return bool(CJK_RE.search(reply))


ECHO_MIN_CHARS = 200        # короче — не судим (заголовки, имена, подписи)
ECHO_RATIO = 0.97


def _norm_for_echo(s):
    return re.sub(r"\s+", " ", PLACEHOLDER_RE.sub("", s)).strip().lower()


INDEX_LINE_RE = re.compile(r"^\s*\S.{0,90}?\.{2,}\s*\d[\d,\s\u2013\u2014-]*\s*$")


def is_index_block(text):
    """A back-of-book index page: term, dot leaders, page number.

    Nothing in it needs translating (point names stay in pinyin, page numbers
    stay), so an identical reply is correct, not an echo.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    hits = sum(1 for ln in lines if INDEX_LINE_RE.match(ln))
    return hits >= max(1, int(len(lines) * 0.6))


def looks_like_echo(block, reply):
    """Ответ почти дословно повторяет исходник — модель вернула эхо вместо
    перевода. Такой ответ надо пересэмплировать и никогда не кэшировать:
    иначе английский абзац молча уезжает в «переведённую» книгу.
    Не судим короткие блоки, блоки без слов исходного письма и индексные
    страницы, где повтор и есть правильный ответ."""
    b = _norm_for_echo(block)
    if len(b) < ECHO_MIN_CHARS or not re.search(r"[A-Za-z]{2,}", b):
        return False
    if is_index_block(block):
        return False
    r = _norm_for_echo(reply)
    if b == r:
        return True
    return difflib.SequenceMatcher(None, b, r).ratio() >= ECHO_RATIO


# Модель иногда возвращает не перевод текста, а сам промпт — причём в режиме
# hymt она его ещё и ПЕРЕВОДИТ, так что в книгу попадает «/но_тхинк/ Перевести
# следующий отрывок на русский язык». Эхо-детектор такое пропускает: ответ не
# похож ни на исходный блок, ни на глоссарий.
INSTRUCTION_LEAK_RE = re.compile(
    # Служебный токен узнаётся по слэшу или подчёркиванию: без этого
    # «что можно думать об обвинении» ловилось как утечка «/no_think».
    r"(/no[_ ]?think|no_think|/но[_ ]?тхинк|но_тхинк"
    r"|/без[_ ]?(?:рассужден|размышлен|расчет|расчёт)"
    r"|без_(?:рассужден|размышлен|расчет|расчёт)"
    r"|/(?:но|не)[_ ]?думать|(?:но|не)_думать"
    r"|translate the following segment|"
    r"перевести следующий отрывок|переведите следующий (?:отрывок|сегмент)|"
    r"without additional explanation|без дополнительных объяснений|"
    r"placeholder tokens|copy any .{0,20}placeholder|"
    r"strictly use these exact term|скопируйте любые)", re.IGNORECASE)


def leaks_instructions(block, reply):
    """True, когда в ответе видна инструкция промпта, а не перевод."""
    src = Counter(m.group().casefold() for m in INSTRUCTION_LEAK_RE.finditer(block))
    out = Counter(m.group().casefold() for m in INSTRUCTION_LEAK_RE.finditer(reply))
    return bool(out - src)


GLOSSARY_LEAK_MIN_LINES = 3


def glossary_lines(glossary):
    """Содержательные строки глоссария: 'Term => Перевод' и его заголовки.

    Возвращается множество нормализованных строк — по ним ловится ответ,
    в который модель вывалила собственную инструкцию вместо перевода.
    """
    out = set()
    for ln in (glossary or "").splitlines():
        s = ln.strip()
        if len(s) >= 6 and ("=>" in s or s.startswith("#")):
            out.add(re.sub(r"\s+", " ", s).lower())
    return out


def leaks_glossary(block, reply, gloss_set):
    """True, когда модель напечатала строки глоссария вместо перевода.

    Hy-MT2 на части блоков вываливает системный промпт целиком: в готовую
    книгу уезжает весь глоссарий, а его '#'-заголовки становятся главами и
    портят оглавление. Эхо-детектор такое не ловит — ответ не похож на
    исходник, он похож на инструкцию. Строки, которые есть и в самом
    исходнике, не в счёт: там утечки нет.
    """
    if not gloss_set:
        return False
    src = {re.sub(r"\s+", " ", ln.strip()).lower() for ln in block.splitlines()}
    hits = 0
    for ln in reply.splitlines():
        s = re.sub(r"\s+", " ", ln.strip()).lower()
        if s and s in gloss_set and s not in src:
            hits += 1
            if hits >= GLOSSARY_LEAK_MIN_LINES:
                return True
    return False
# Сокращения, после которых точка не кончает предложение. Немецкий научный
# набор XIX века полон их: «z. B.», «d. h.», «u. s. w.», «S. 33.», «§. 160.»,
# плюс инициалы и порядковые номера.
ABBREV_END = re.compile(
    r"(?:\b(?:z|d|u|s|f|i|c|vgl|Vgl|Bd|Th|Nr|Anm|Abschn|Aufl|Hrn|Hr|St|Kap|Cap"
    r"|dgl|ff|cf|resp|bzw|usw|etc|S|Th|vol)\.|\b[A-ZÄÖÜ]\.|\b\d{1,4}\.|§\.)"
    r"\s*$")

THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)

GENERIC_SYSTEM = """You are a professional literary translator. Translate the user's text from {src} into fluent, natural, literary {lang}.

Hard rules:
- Output ONLY the translation. No notes, no explanations, no preamble.
- Copy every placeholder token of the form \u27e6M######\u27e7 through EXACTLY as-is. They stand for math, code and references -- never translate, reorder, alter, add or drop them.
- Preserve all Markdown structure: keep heading markers (#, ##, ...), list markers, blockquotes (>), table pipes, and emphasis markers (*italic*, **bold**). Translate only the words, not the syntax.
- Do not translate numbers, units, code identifiers, file paths or URLs.
- Translate figure/table captions and footnotes.
- Keep the meaning faithful; write idiomatic {lang}, not word-for-word."""


POLISH_SYSTEM = """You are a meticulous literary editor. The user gives you a text in {lang}. Improve it: fix grammar, agreement and calques, replace clumsy or unnatural phrasing with fluent, idiomatic {lang}. Do NOT change the meaning, do NOT add or drop information.

Hard rules:
- Output ONLY the edited text. No notes, no explanations, no preamble.
- Copy every placeholder token of the form ⟦M######⟧ through EXACTLY as-is. They stand for math, code and references -- never reorder, alter, add or drop them.
- Preserve all Markdown structure: keep heading markers (#, ##, ...), list markers, blockquotes (>), table pipes, and emphasis markers (*italic*, **bold**).
- Do not change numbers, units, code identifiers, file paths or URLs.
- If a segment is already good, return it unchanged."""


# Подсказка на повтор после испорченного маркера. Обычная просьба «copy
# placeholders unchanged» стоит в промпте всегда, и если модель её уже
# нарушила, повторять то же самое бессмысленно — нужен более резкий приказ
# именно про этот отказ.
TOKEN_RETRY_HINT = (
    "\n\nCRITICAL: your previous attempt CORRUPTED a placeholder token. "
    "Every token looks exactly like ⟦M000123⟧ — copy it CHARACTER BY "
    "CHARACTER, including the ⟦ and ⟧ brackets and all six digits. "
    "Never translate, re-bracket, renumber, split or drop a token.")

# Numbers, point codes and measurements are the part of a medical atlas the
# reader trusts most. A reply that rounds, re-formats or drops one is worse
# than a slower one, so the retry says it explicitly.
NUMBERS_RETRY_HINT = (
    "\n\nCRITICAL: your previous attempt changed a number. Copy every numeral, "
    "point code, dose and measurement exactly as written in the source: "
    "0.5 stays 0.5 (not 0,5 and not 1), 3-5 a.m. stays 3-5, ST-36 stays ST-36. "
    "Keep numerals as digits and never spell them out (write 3, not 'три'). "
    "Do not round, re-format, merge, split or drop any number.")

# A translation model may run on past the end of the segment and invent more
# text; the reply is then cut and rejected. The retry pins the scope.
SCOPE_RETRY_HINT = (
    "\n\nCRITICAL: translate ONLY the segment given below, nothing else. "
    "Do not continue it, do not add further paragraphs, bullets, notes or "
    "commentary, and stop as soon as the segment ends.")


def build_messages(block, mode, target_lang, glossary, think="off",
                   source_lang="English", retry_hint=""):
    if paragraphs.MARKER.search(block):
        retry_hint = paragraphs.RULE + retry_hint
    if mode == "polish":
        sys_prompt = POLISH_SYSTEM.format(lang=target_lang)
        if glossary:
            sys_prompt += (
                f"\n\nGlossary ({source_lang} => {target_lang}) -- these "
                "renderings are canonical, never replace them with "
                "synonyms:\n" + glossary
            )
        return [{"role": "system", "content": sys_prompt + retry_hint},
                {"role": "user", "content": block}]
    if mode == "hymt":
        sys_prompt = ("Copy any \u27e6M######\u27e7 placeholder tokens through "
                      "unchanged. Never translate these rules or the glossary; "
                      "they are instructions, not text. Preserve every clause and example; "
                      "do not replace unclear words or segment boundaries with ellipses. "
                      "Preserve Markdown syntax, table pipes and row breaks; "
                      "translate cell text without moving cells.")
        if glossary:
            sys_prompt += (
                f"\nStrictly use these exact term translations "
                f"({source_lang} => {target_lang}):\n" + glossary
            )
        if think != "on":
            sys_prompt = "/no_think\n" + sys_prompt
        user = (f"Translate the following segment into {target_lang}, "
                "without additional explanation.\n\n" + block)
        return [{"role": "system", "content": sys_prompt + retry_hint},
                {"role": "user", "content": user}]
    sys_prompt = GENERIC_SYSTEM.format(lang=target_lang, src=source_lang)
    if think != "on":
        sys_prompt = "/no_think\n" + sys_prompt
    if glossary:
        sys_prompt += (
            f"\n\nGlossary ({source_lang} => {target_lang}) -- use these "
            "exact renderings consistently:\n" + glossary
        )
    return [
        {"role": "system", "content": sys_prompt + retry_hint},
        {"role": "user", "content": block},
    ]


def api_url(host):
    """localhost:1234 -> +/v1/chat/completions; хосты, уже несущие версию
    в пути (…/v1beta/openai, …/v1) -> +/chat/completions."""
    h = host.rstrip("/")
    if h.endswith("/chat/completions"):
        return h
    if re.search(r"/(v\d+[a-z]*|openai)$", h):
        return h + "/chat/completions"
    return h + "/v1/chat/completions"


def call_api(host, model, messages, temperature, top_p):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": 4096,
    }
    # облачные OpenAI-совместимые эндпоинты требуют настоящий ключ
    token = os.environ.get("LLM_API_KEY") or "local"
    req = urllib.request.Request(
        api_url(host),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
    )
    trace = current_attempt()
    if trace:
        trace.request(api_url(host), payload)
    started = time.monotonic()
    try:
        with OPENER.open(req, timeout=600) as r:
            raw = r.read()
            if trace:
                trace.response(raw, status=getattr(r, "status", None),
                               elapsed_seconds=time.monotonic() - started)
    except urllib.error.HTTPError as exc:
        if trace:
            trace.response(exc.read(), status=exc.code,
                           elapsed_seconds=time.monotonic() - started)
        raise
    except http.client.IncompleteRead as exc:
        if trace:
            trace.response(exc.partial, partial=True,
                           elapsed_seconds=time.monotonic() - started)
        raise
    data = json.loads(raw)
    if trace:
        trace.model_result(data)
    if (not isinstance(data, dict) or not isinstance(data.get("choices"), list)
            or not data["choices"] or not isinstance(data["choices"][0], dict)):
        raise ValueError("API response has invalid choices")
    choice = data["choices"][0]
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("API response has invalid message/content")
    if message.get("refusal") or choice.get("finish_reason") not in (None, "stop"):
        raise ValueError("API response did not finish normally (truncation/refusal)")
    text = message["content"]
    return THINK_RE.sub("", text).strip()


IMAGE_ONLY_RE = re.compile(r"^\s*(!\[[^\]]*\]\([^)]*\)\s*)+$")


def is_translatable(block):
    """Блок из одних картинок переводить нечего: имя файла — латиница, и без
    этой проверки модель «переводит» путь, а рисунок теряется."""
    if IMAGE_ONLY_RE.match(block):
        return False
    return _has_letters(block)


def _has_letters(block):
    """False for blocks that are only placeholders / symbols / whitespace.
    \u041A\u0438\u0440\u0438\u043B\u043B\u0438\u0446\u0430 \u0438 \u0433\u0440\u0435\u0447\u0435\u0441\u043A\u0438\u0439 \u0443\u0447\u0442\u0435\u043D\u044B: \u0432 \u0440\u0435\u0436\u0438\u043C\u0435 polish \u0432\u0445\u043E\u0434\u043D\u043E\u0439 \u0442\u0435\u043A\u0441\u0442 \u0443\u0436\u0435 \u0440\u0443\u0441\u0441\u043A\u0438\u0439."""
    stripped = PLACEHOLDER_RE.sub("", block)
    return bool(re.search(r"[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF]",
                          stripped))


def split_blocks(text):
    return [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]


def group_blocks(blocks, limit):
    """Join short neighbouring blocks into one request unit.

    The server pays a fixed price per request, so a book of thousands of
    one-line paragraphs costs the same overhead again and again. A table
    stays alone: the model must not move its cells. A picture may join the
    group: its path is masked as a placeholder and copied through.
    """
    if not limit or limit <= 0:
        return list(blocks)
    out, group = [], []
    size = 0
    for block in blocks:
        if block.lstrip().startswith("|"):
            if group:
                out.append("\n\n".join(group))
                group, size = [], 0
            out.append(block)
            continue
        if group and size + len(block) + 2 > limit:
            out.append("\n\n".join(group))
            group, size = [], 0
        group.append(block)
        size += len(block) + 2
    if group:
        out.append("\n\n".join(group))
    return out


VALIDATOR_VERSION = "candidate-v5"
REQUEST_VERSION = "request-v5"
REPAIR_TOKEN = re.compile(r"[⟨‹▦⟦\[<](M\d{6})[⟩›◦⟧\]>]")
# "1. ", "2. " — an enumeration marker of a list, not a number of the text.
# A number that is part of a dotted number ("Fig. 1.1", "1.4") is content:
# the dot before it and the digit after it keep the marker out of the way.
LIST_MARKER = re.compile(r"(?m)^[ \t]*\d{1,2}\.(?=[ \t])")
# Russian number words a translator may use instead of a small digit:
# "1 finger-width" -> "одной ширины пальца", "L4/L5" -> "четвёртого и пятого".
# Cardinal and ordinal forms are both accepted, otherwise a correct translation
# that spells the level out would be rejected as a lost number.
_RU_CARD = {
    1: "один|одна|одно|одной|одного|одному|одним|одном|одну",
    2: "два|две|двух|двум|двумя|пара|пары|пару",
    3: "три|трёх|трех|трём|трем|тремя|треть|трети|третью",
    4: "четыре|четырёх|четырех|четырём|четырем|четырьмя",
    5: "пять|пяти|пятью",
    6: "шесть|шести|шестью",
    7: "семь|семи|семью",
    8: "восемь|восьми|восемью",
    9: "девять|девяти|девятью",
    10: "десять|десяти|десятью",
}
_RU_ORD = {
    1: "перв(?:ый|ого|ому|ым|ом|ая|ое|ые|ых|ую|ой)",
    2: "втор(?:ой|ого|ому|ым|ом|ая|ое|ые|ых|ую)",
    3: "трет(?:ий|ьего|ьему|ьим|ьем|ья|ье|ьи|ьих|ью|ьей|ьими)",
    4: "(?:четверт|четвёрт)(?:ый|ого|ому|ым|ом|ая|ое|ые|ых|ую|ой)",
    5: "пят(?:ый|ого|ому|ым|ом|ая|ое|ые|ых|ую|ой)",
    6: "шест(?:ой|ого|ому|ым|ом|ая|ое|ые|ых|ую)",
    7: "седьм(?:ой|ого|ому|ым|ом|ая|ое|ые|ых|ую)",
    8: "восьм(?:ой|ого|ому|ым|ом|ая|ое|ые|ых|ую)",
    9: "девят(?:ый|ого|ому|ым|ом|ая|ое|ые|ых|ую)",
    10: "десят(?:ый|ого|ому|ым|ом|ая|ое|ые|ых|ую)",
}
RU_NUMBER_WORDS = {
    n: re.compile(r"\b(?:%s|%s)\b" % (_RU_CARD[n], _RU_ORD[n]), re.I)
    for n in _RU_CARD
}
# "1st"/"2nd" and "1-й"/"2-го": an ordinal is a word in Russian, its digit may go.
ORDINAL = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)\b|"
    r"\b\d{1,2}-(?:й|я|е|го|му|м|х|ю|ой|ый|ая|ое|ые|ых|ым|ыми|ого|ому)\b",
    re.I)

# Only explicit equivalent number words can account for a changed numeral.
# Source words are deducted too: an existing "one" -> "один" must not hide a
# separate lost measurement. Codes, decimals and section references stay digits.
EN_NUMBER_WORDS = {
    n: re.compile(r"\b(?:%s)\b" % words, re.I)
    for n, words in enumerate(("", "one|first", "two|second|pair|pairs", "three|third",
                              "four|fourth", "five|fifth", "six|sixth",
                              "seven|seventh", "eight|eighth", "nine|ninth",
                              "ten|tenth")) if n
}
NUMERAL = re.compile(r"\d+(?:[.,]\d+)*")


def numeric_evidence(text):
    """Word alternatives and literals that cannot be replaced by number words."""
    text = PLACEHOLDER_RE.sub("", text)
    text = LIST_MARKER.sub("", text)
    protected = Counter()
    ordinal_spans = {m.span() for m in ORDINAL.finditer(text)}
    for m in NUMERAL.finditer(text):
        # Suffix-bearing ordinals may be written as digits or Russian words.
        ordinal = any(a == m.start() and b >= m.end() for a, b in ordinal_spans)
        prefix, suffix = text[:m.start()], text[m.end():]
        code = (re.search(r"[\w.][-–—]?$", prefix)
                or re.match(r"[A-Za-zА-Яа-я]", suffix)
                or re.search(r"[§#]\s*$", prefix))
        if not ordinal and (code or "." in m[0] or "," in m[0]):
            protected[m[0].replace(",", ".")] += 1
    words = Counter({str(n): len(RU_NUMBER_WORDS[n].findall(text))
                     + len(EN_NUMBER_WORDS[n].findall(text))
                     for n in RU_NUMBER_WORDS})
    return protected, words


def numbers_changed(source, target, source_numbers, target_numbers):
    src_protected, src_words = numeric_evidence(source)
    tgt_protected, tgt_words = numeric_evidence(target)
    if src_protected != tgt_protected:
        return True
    for num in source_numbers.keys() | target_numbers.keys():
        delta = source_numbers[num] - target_numbers[num]
        # Every missing or extra occurrence needs its own word equivalent;
        # there is no blanket allowance for a repeated number to disappear.
        if delta > 0 and tgt_words[num] - src_words[num] < delta:
            return True
        if delta < 0 and src_words[num] - tgt_words[num] < -delta:
            return True
    return False


@dataclass(frozen=True)
class ValidationPolicy:
    min_ratio: float = 0.0
    length_min_chars: int = 300
    reject_cjk: bool = True
    reject_echo: bool = True
    mode: str = "hymt"


@dataclass(frozen=True)
class Candidate:
    text: str
    reasons: tuple
    repaired: bool = False

    @property
    def ok(self):
        return not self.reasons


def added_cjk(source, reply):
    # A single quoted name must not whitelist an arbitrary hallucinated paragraph.
    return bool(Counter(CJK_RE.findall(reply)) - Counter(CJK_RE.findall(source)))


def added_ellipsis(source, reply):
    src = len(re.findall(r"…|\.{3,}", source))
    if is_index_block(source):
        return False
    return src == 0 and len(re.findall(r"…|\.{3,}", reply)) > 0


def structural_literals(text):
    """Cheap omission checks, not semantic review. Mask IDs are checked separately.

    Digit groups allow decimal punctuation to localize but do not allow numbers
    to disappear. Greek enumeration labels and Markdown note IDs stay verbatim.
    A number that is an enumeration marker ("1. ", "2. ") is structure, not
    content: a translation that folds a list into a sentence loses the marker
    without losing anything, and such a loss must not reject the whole block.
    Ordinal suffixes are normalized, but their values remain checked. Otherwise
    "2nd/3rd" -> "2/3" falsely looks like added numbers, and lost ordinals pass.
    """
    text = PLACEHOLDER_RE.sub("", text)
    content = LIST_MARKER.sub(" ", text)
    content = ORDINAL.sub(lambda m: re.match(r"\d+", m[0])[0], content)
    return {"numbers": Counter(re.findall(r"\d+", content)),
            "enumeration labels": Counter(re.findall(r"(?<!\w)([Ͱ-Ͽ])[).]", text)),
            "footnote IDs": Counter(re.findall(r"\[\^([^\]]+)\]", text))}



def validate_candidate(source, reply, policy=None, glossary=""):
    """One gate for API, cache, repair and assembled fragments. No bad fallback."""
    policy = policy or ValidationPolicy()
    reasons = []
    if not isinstance(reply, str) or not reply.strip():
        return Candidate("", ("empty",))
    ids = PLACEHOLDER_RE.findall(source)
    allowed = {token[1:-1] for token in ids}
    repaired = REPAIR_TOKEN.sub(lambda m: "⟦" + m[1] + "⟧" if m[1] in allowed else m[0], reply)
    if has_damaged_tokens(repaired):
        reasons.append("damaged token")
    if PLACEHOLDER_RE.findall(repaired) != ids:
        reasons.append("mask IDs/count/order")
    if leaks_instructions(source, repaired):
        reasons.append("instruction leak")
    if leaks_glossary(source, repaired, glossary_lines(glossary)):
        reasons.append("glossary leak")
    if policy.reject_cjk and added_cjk(source, repaired):
        reasons.append("CJK addition")
    if policy.reject_echo and looks_like_echo(source, repaired):
        reasons.append("echo")
    prose = PLACEHOLDER_RE.sub("", source).strip()
    translated = PLACEHOLDER_RE.sub("", repaired).strip()
    source_literals, target_literals = structural_literals(source), structural_literals(repaired)
    from prepare_mineru import table_signature
    if table_signature(source) != table_signature(repaired):
        reasons.append("table rows/columns/numeric cells changed")
    for kind in source_literals:
        if kind == "numbers":
            if numbers_changed(source, repaired, source_literals[kind],
                               target_literals[kind]):
                reasons.append(kind + " changed (possible omission/addition)")
        elif source_literals[kind] != target_literals[kind]:
            reasons.append(kind + " changed (possible omission/addition)")
    # Never silently turn an unread word or a request boundary into an omission.
    if added_ellipsis(prose, translated):
        reasons.append("added ellipsis (possible omission)")
    # Short names, headings, captions and formula-only blocks have no length
    # heuristic. Explicit --min-ratio also applies to long polish prose.
    if (policy.min_ratio and len(prose) >= policy.length_min_chars
            and not re.fullmatch(r"\s*#{1,6}[ \t]+[^\n]*\s*", source)
            and len(translated) < policy.min_ratio * len(prose)):
        reasons.append("too short")
    return Candidate(repaired.strip(), tuple(reasons), repaired != reply)


def request_config(a, glossary, glossary_profile=None):
    endpoint = urlsplit(api_url(a.host))
    # No authentication/query strings in on-disk metadata.
    safe_endpoint = urlunsplit((endpoint.scheme, endpoint.hostname or "",
                               endpoint.path, "", ""))
    result = {"schema": REQUEST_VERSION, "source_lang": a.source_lang,
            "target_lang": a.target_lang, "model": a.model, "mode": a.mode,
            "endpoint": safe_endpoint, "port": endpoint.port,
            "glossary_profile_sha256": canonical_hash(glossary_profile or {}),
            "glossary_sha256": sha_text(glossary), "think": a.think,
            "templates_sha256": canonical_hash([GENERIC_SYSTEM, POLISH_SYSTEM,
                build_messages("", a.mode, a.target_lang, glossary, a.think, a.source_lang),
                TOKEN_RETRY_HINT]),
            "planner": PLANNER_VERSION, "split_chars": a.split_chars,
            "group_chars": getattr(a, "group_chars", 0),
            "generation": {"temperature": a.temperature, "top_p": a.top_p, "max_tokens": 4096}}
    if getattr(a, "group_chars", 0) > 0:
        result["paragraph_protocol"] = paragraphs.PROTOCOL
        result["paragraph_template_sha256"] = sha_text(paragraphs.RULE)
    return result


def request_identity(block, config, fragments, messages):
    return canonical_hash({"config": config, "source": block, "plan": fragments,
                           "messages": messages})


def plan_translation_fragments(block, split_chars=0, paragraph_members=None):
    """Shared plan for MT, context estimation and explicit request export."""
    if paragraph_members is not None:
        return paragraphs.plan_group(paragraph_members, split_chars)
    return plan_fragments(block, split_chars)


def validate_fragment(fragment, reply, policy=None, glossary=""):
    if "paragraphs" not in fragment:
        return validate_candidate(fragment["text"], reply, policy, glossary)
    parts = fragment["paragraphs"]
    try:
        bodies = paragraphs.parse_response(reply, parts)
    except ValueError as exc:
        return Candidate("", (str(exc),))
    checked = [validate_candidate(part["text"], body, policy, glossary)
               for part, body in zip(parts, bodies)]
    reasons = tuple(f"paragraph {part['id']}: {reason}"
                    for part, result in zip(parts, checked) for reason in result.reasons)
    return Candidate(paragraphs.encode(parts, [c.text for c in checked]), reasons,
                     any(c.repaired for c in checked))


def assemble_translation(block, fragments, pieces, policy, glossary, members=None):
    """Recheck paragraph ownership after fragment assembly, including cache hits."""
    if members is None:
        return validate_candidate(block, " ".join(pieces), policy, glossary), None
    by_id = {p["id"]: [] for p in members}
    for fragment, piece in zip(fragments, pieces):
        for part, body in zip(fragment["paragraphs"],
                              paragraphs.parse_response(piece, fragment["paragraphs"])):
            by_id[part["id"]].append(body)
    checked = [validate_candidate(row["text"], " ".join(by_id[row["id"]]), policy, glossary)
               for row in members]
    reasons = tuple(f"paragraph {row['id']}: {reason}"
                    for row, candidate in zip(members, checked) for reason in candidate.reasons)
    bodies = [paragraphs.render_paragraph(c.text) for c in checked]
    return Candidate("\n\n".join(bodies), reasons, any(c.repaired for c in checked)), bodies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mode", choices=["hymt", "generic", "polish"], default="hymt",
                    help="hymt: перевод Hy-MT2; generic: перевод общей моделью; "
                         "polish: литературная правка уже переведённого текста")
    ap.add_argument("--model", default="hy-mt2-7b")
    ap.add_argument("--host", default="http://localhost:1234")
    ap.add_argument("--target-lang", default="Russian")
    ap.add_argument("--source-lang", default="English",
                    help="язык исходника. Он подставляется в системный промпт "
                         "и в шапку глоссария; при немецком источнике "
                         "хардкод «English» и промпт «from English» "
                         "противоречили самому тексту")
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--glossary-profile", help="optional critical entries and relevant term groups JSON")
    ap.add_argument("--parallel", type=int, default=1,
                    help="concurrent requests (match LM Studio's Parallel slots, e.g. 3)")
    ap.add_argument("--think", choices=["on", "off"], default="off",
                    help="Hunyuan hybrid thinking mode (off = fast, default)")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top-p", type=float, default=0.6)
    ap.add_argument("--max-chars", type=int, default=4000,
                    help="ПОРОГ ПРЕДУПРЕЖДЕНИЯ, а не нарезки: блок длиннее "
                         "этого числа знаков заметен в stderr. Режет блоки "
                         "--split-chars, и без него длинный блок уезжает в "
                         "модель целиком — а Hy-MT2 такие КОНСПЕКТИРУЕТ. "
                         "Разводить эти два ключа приходится потому, что "
                         "нарезка входит в ключ кэша: сменить порог значит "
                         "перевести заново все блоки длиннее него")
    ap.add_argument("--split-chars", type=int, default=0,
                    help="делить блок длиннее N знаков по границам предложений "
                         "и переводить частями (0 = не делить). Hy-MT2 на "
                         "длинных абзацах КОНСПЕКТИРУЕТ: на томе 6 отношение "
                         "длин падало с 0.99 при 700 знаках до 0.79 при 3500. "
                         "Разумное значение — 1200")
    ap.add_argument("--group-chars", type=int, default=0,
                    help="объединять мелкие соседние абзацы в один запрос, "
                         "пока группа не станет длиннее N знаков (0 = не "
                         "объединять). Нужен для книг с тысячами коротких "
                         "абзацев: сервер тратит фиксированные ~2 с на запрос, "
                         "и поабзацная нарезка превращает эти накладные в "
                         "десятки часов. Таблицы и картинки в группы не "
                         "объединяются")
    ap.add_argument("--min-ratio", type=float, default=0.0,
                    help="брак ответа короче исходника в эту долю (0 = не "
                         "проверять). Эвристика сокращений, не проверка смысла")
    ap.add_argument("--cache-dir", default=".tcache")
    ap.add_argument("--context-per-request", type=int, default=0,
                    help="explicit request token budget (0 = no additional budget check)")
    ap.add_argument("--keep-source", type=Path,
                    help="explicit hash-bound policy for deliberate unchanged blocks")
    ap.add_argument("--reject-cjk", choices=["auto", "on", "off"], default="auto",
                    help="брак ответов с CJK-иероглифами (auto = включено, "
                         "если целевой язык не китайский/японский/корейский)")
    ap.add_argument("--echo-check", choices=["auto", "on", "off"], default="auto",
                    help="брак ответов-эхо: модель вернула исходник вместо "
                         "перевода (auto = включено везде, кроме --mode polish, "
                         "где «вернуть как есть» — штатный ответ)")
    ap.add_argument("--manifest-sha256", default=None, help="identity supplied by parts orchestrator")
    a = ap.parse_args()
    if (a.split_chars < 0 or a.group_chars < 0 or a.context_per_request < 0
            or not 0 <= a.min_ratio <= 2):
        ap.error("invalid split-chars/group-chars/context-per-request/min-ratio")

    reject_cjk = (a.reject_cjk == "on"
                  or (a.reject_cjk == "auto"
                      and a.target_lang.strip().lower() not in CJK_TARGETS))
    reject_echo = (a.echo_check == "on"
                   or (a.echo_check == "auto" and a.mode != "polish"))

    # generic-режим держит инструкцию только у instruction-tuned моделей;
    # base/чат-модели без RLHF (напр. gemma-*-it-obliterated) галлюцинируют
    if a.mode == "generic":
        good = ("qwen", "deepseek", "gpt", "claude", "gemini", "llama",
                "mistral", "gemma-3", "gemma3")
        if not any(g in a.model.lower() for g in good):
            sys.stderr.write(
                f"?? generic-режим требует instruction-tuned модель; "
                f"'{a.model}' в белом списке не найдена — возможны "
                "галлюцинации. Проверьте на нескольких блоках или возьмите "
                "qwen3.5/deepseek.\n")

    glossary = ""
    if a.glossary and not Path(a.glossary).is_file():
        ap.error("requested glossary does not exist")
    if a.glossary and os.path.exists(a.glossary):
        glossary = Path(a.glossary).read_text(encoding="utf-8").strip()
    glossary_profile = load_profile(a.glossary_profile)
    validation_glossary = select_glossary("", glossary, glossary_profile, all_groups=True)
    os.makedirs(a.cache_dir, exist_ok=True)
    text = Path(a.src).read_bytes().decode("utf-8")
    source_hash = sha_text(text)
    blocks = split_blocks(text)
    paragraph_groups = None
    if a.group_chars:
        try:
            source_paragraphs = paragraphs.source_paragraphs(blocks)
        except ValueError as exc:
            ap.error(str(exc))
        blocks = group_blocks(blocks, a.group_chars)
        paragraph_groups = paragraphs.assign_groups(blocks, source_paragraphs)
    from source_preservation import load_policy, select_preserved, render_preserved
    try:
        preservation_hash = sha_file(a.keep_source) if a.keep_source else None
        preserved = select_preserved(blocks, load_policy(a.keep_source))
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    n = len(blocks)
    out = [None] * n
    out_paragraphs = [None] * n
    jobs, records, attempts = [], {}, {}
    n_cached = 0
    policy = ValidationPolicy(a.min_ratio, reject_cjk=reject_cjk,
                              reject_echo=reject_echo, mode=a.mode)
    config = request_config(a, glossary, glossary_profile)
    config_hash = canonical_hash(config)
    if not a.context_per_request:
        sys.stderr.write("?? per-request runtime context unknown; no capacity confirmation\n")
    journal = AttemptJournal(Path(str(a.dst) + ".attempts"), metadata={
        "source_sha256": source_hash, "request_config_sha256": config_hash,
        "context_per_request": a.context_per_request, "model": a.model})
    for i, block in enumerate(blocks):
        members = paragraph_groups[i] if paragraph_groups is not None else None
        if members is not None:
            out_paragraphs[i] = [p["text"] for p in members]
        if i + 1 in preserved:
            out[i] = block
            continue
        if not block.strip() or not is_translatable(block):
            out[i] = block
            continue
        fragments = plan_translation_fragments(block, a.split_chars, members)
        if len(block) > a.max_chars:
            sys.stderr.write(f"[{i + 1}/{n}] source block {len(block)} chars; planned {len(fragments)} request(s)\n")
        messages = [build_messages(f["text"], a.mode, a.target_lang, select_glossary(f["text"], glossary, glossary_profile),
                                   a.think, a.source_lang) for f in fragments]
        key = request_identity(block, config, fragments, messages)
        cache = Path(a.cache_dir) / (key + ".json")
        records[i] = {"request_sha256": key, "fragments": fragments}
        if cache.exists():
            try:
                entry = json.loads(cache.read_text(encoding="utf-8"))
                pieces = entry["pieces"]
                valid = (entry.get("schema") == REQUEST_VERSION
                         and entry.get("request_sha256") == key
                         and entry.get("response_sha256") == sha_text(entry["response"])
                         and len(pieces) == len(fragments))
                if valid:
                    checked = [validate_fragment(f, p, policy, validation_glossary)
                               for f, p in zip(fragments, pieces)]
                    result, bodies = assemble_translation(block, fragments,
                        [v.text for v in checked], policy, validation_glossary, members)
                    valid = (all(v.ok for v in checked) and result.ok
                             and result.text == entry["response"])
                if valid:
                    out[i] = result.text
                    out_paragraphs[i] = bodies
                    n_cached += 1
                    sys.stderr.write(f"[{i + 1}/{n}] cache (revalidated)\n")
                    continue
            except (ValueError, KeyError, TypeError, OSError):
                pass
            # Old/invalid entries remain as evidence, never delete the cache.
            sys.stderr.write(f"[{i + 1}/{n}] cache miss (policy/identity)\n")
        jobs.append((i, block, cache))

    lock = threading.Lock()
    done = [0]
    consec_fail = [0]
    failed = []                 # машиночитаемый след «keeping source»
    abort = threading.Event()   # сервер мёртв: стоп, а не сотни «keeping source»

    def translate_chunk(i, block, fragment=None):
        selected_glossary = select_glossary(block, glossary, glossary_profile)
        messages = build_messages(block, a.mode, a.target_lang, selected_glossary,
                                  a.think, a.source_lang)
        why = "unknown"
        for attempt in range(3):
            try:
                with journal.attempt(context={"block_index": i + 1, "retry": attempt + 1,
                        "fragment_sha256": sha_text(block),
                        "request_sha256": records[i]["request_sha256"]}) as trace:
                    estimate = check_messages(messages, a.context_per_request)
                    trace.event("budget", estimated_tokens=estimate,
                                context_per_request=a.context_per_request)
                    model_before = check_managed_model(a.host, a.model, messages, a.context_per_request,
                                                       phase="before")
                    reply = call_api(a.host, a.model, messages, a.temperature, a.top_p)
                    check_managed_model(a.host, a.model, messages, a.context_per_request,
                                        phase="after", previous=model_before)
                    candidate = validate_fragment(fragment or {"text": block}, reply, policy, validation_glossary)
                    trace.validation(candidate.ok, reasons=candidate.reasons,
                                     candidate_text=candidate.text, repaired=candidate.repaired)
                if candidate.ok:
                    return candidate.text, "repaired" if candidate.repaired else "ok"
                why = "; ".join(candidate.reasons)
                if any("token" in r or "mask" in r for r in candidate.reasons):
                    messages = build_messages(block, a.mode, a.target_lang, selected_glossary,
                                              a.think, a.source_lang, TOKEN_RETRY_HINT)
                elif any("numbers changed" in r or "table rows" in r for r in candidate.reasons):
                    messages = build_messages(block, a.mode, a.target_lang, selected_glossary,
                                              a.think, a.source_lang, NUMBERS_RETRY_HINT)
                elif any("paragraph" in r for r in candidate.reasons):
                    messages = build_messages(block, a.mode, a.target_lang, selected_glossary,
                                              a.think, a.source_lang, SCOPE_RETRY_HINT)
            except ManagedModelError as exc:
                why = "managed model refused: " + str(exc)
                with lock:
                    attempts.setdefault(i + 1, []).append({"attempt": attempt + 1, "reason": why})
                    sys.stderr.write(f"[{i + 1}/{n}] {why}; no automatic retry\n")
                abort.set()
                return None, why
            except (urllib.error.URLError, OSError, KeyError, IndexError, TypeError,
                    TimeoutError, ValueError, http.client.HTTPException) as e:
                why = f"request failed: {type(e).__name__}"
                if "ValueError" in why:
                    messages = build_messages(block, a.mode, a.target_lang, selected_glossary,
                                              a.think, a.source_lang, SCOPE_RETRY_HINT)
                time.sleep(2)
            with lock:
                attempts.setdefault(i + 1, []).append({"attempt": attempt + 1, "reason": why})
                sys.stderr.write(f"[{i + 1}/{n}] retry {attempt + 1}: {why}\n")
        # Preserve the user's short-but-kept fix: never return a rejected reply.
        return None, why

    def work(job):
        i, block, cache = job
        if abort.is_set():
            return i, block
        fragments = records[i]["fragments"]
        pieces, ru, why = [], None, "not attempted"
        for fragment in fragments:
            if fragment["oversized"]:
                sys.stderr.write(f"[{i + 1}/{n}] indivisible request: {len(fragment['text'])} chars\n")
            piece, why = translate_chunk(i, fragment["text"], fragment)
            if piece is None:
                break
            pieces.append(piece)
        else:
            members = paragraph_groups[i] if paragraph_groups is not None else None
            assembled, bodies = assemble_translation(block, fragments, pieces, policy, validation_glossary, members)
            with journal.attempt(context={"block_index": i + 1, "stage": "assembly",
                    "request_sha256": records[i]["request_sha256"]}) as trace:
                trace.validation(assembled.ok, reasons=assembled.reasons,
                                 candidate_text=assembled.text, stage="assembly")
            if assembled.ok:
                ru = assembled.text
                out_paragraphs[i] = bodies
            else:
                why = "; ".join(assembled.reasons)
        with lock:
            done[0] += 1
            if ru is None:
                failed.append({"index": i + 1, "reason": why})
                sys.stderr.write(f"[{done[0]}/{n}] FAILED block {i + 1} ({why}), keeping source\n")
                # The abort is a dead-server signal: a rejected answer means the
                # server talked to us. Only transport failures count, otherwise
                # a resumed run (the hard blocks are retried first) stops itself.
                if why.startswith("request failed"):
                    consec_fail[0] += 1
                    if consec_fail[0] >= 10:
                        abort.set()
                else:
                    consec_fail[0] = 0
            else:
                consec_fail[0] = 0
                sys.stderr.write(f"[{done[0]}/{n}] ok\n")
        if ru is not None:
            write_json(cache, {"schema": REQUEST_VERSION,
                              "request_sha256": records[i]["request_sha256"],
                              "config_sha256": config_hash, "pieces": pieces,
                              "response": ru, "response_sha256": sha_text(ru),
                              "validator": VALIDATOR_VERSION, "policy": asdict(policy)})
        return i, (ru if ru is not None else block)

    workers = max(1, a.parallel)
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for i, res in ex.map(work, jobs):
                out[i] = res

    # машиночитаемый итог стадии: раньше единственным следом «keeping source»
    # была строка в stderr, которую никто не читал, — и английские абзацы
    # молча уезжали в готовую книгу
    failed.sort(key=lambda f: f["index"])
    report_path = Path(a.dst + ".report.json")
    if sha_file(a.src) != source_hash:
        abort.set()
        failed.append({"index": 0, "reason": "source changed during translation"})
    if a.keep_source and (not a.keep_source.is_file()
                          or sha_file(a.keep_source) != preservation_hash):
        abort.set()
        failed.append({"index": 0, "reason": "source preservation policy changed during translation"})
    result_text = None
    allowed_source = []
    paragraph_coverage = None
    if not abort.is_set():
        # Collapse blank lines inside a block, but keep the blank line that
        # turns "[^id]: body" into a footnote definition: without it Pandoc
        # reads the definition as ordinary paragraph text and the note is lost.
        if paragraph_groups is not None:
            rendered_units = ["\n\n".join(bodies) for bodies in out_paragraphs]
            paragraph_coverage = paragraphs.coverage_rows(paragraph_groups, out_paragraphs)
        else:
            rendered_units = [render_preserved(b) if i in preserved else paragraphs.render_paragraph(b)
                              for i, b in enumerate(out, 1)]
        offset = 0
        for i, unit in enumerate(rendered_units, 1):
            if i in preserved:
                allowed_source.append(dict(preserved[i], output_start=offset,
                                           output_end=offset + len(unit)))
            offset += len(unit) + 2
        result_text = "\n\n".join(rendered_units)
        atomic_text(a.dst, result_text)
    write_json(report_path, {
        "schema": 1, "stage": "raw_mt", "run_id": __import__("uuid").uuid4().hex,
        "status": "failed" if failed or abort.is_set() else "passed",
        "source_sha256": source_hash,
        "output_sha256": sha_text(result_text) if result_text is not None else None,
        "manifest_sha256": a.manifest_sha256,
        "request_config_sha256": config_hash, "request_config": config,
        "validator": VALIDATOR_VERSION,
        "policy": dict(asdict(policy), source_preservation_sha256=preservation_hash),
        "source_preservation_sha256": preservation_hash,
        "context_per_request": a.context_per_request,
        "attempt_journal": str(journal.path), "attempt_run_id": journal.run_id,
        "src": a.src, "dst": a.dst, "mode": a.mode, "model": a.model,
        "blocks_total": n, "from_cache": n_cached,
        "translated": max(0, done[0] - len(failed)),
        "kept_source": len(failed), "failed_blocks": failed,
        "preserved_source": len(allowed_source), "allowed_source": allowed_source,
        "paragraph_coverage": paragraph_coverage,
        "aborted": abort.is_set(), "attempts": attempts,
        "requests": {str(i + 1): row for i, row in records.items()},
        "semantic_review": "not_run"})
    if abort.is_set():
        sys.stderr.write("!! repeated failures or changed input; output not published; cache retained\n")
        sys.exit(3)

    if failed:
        sys.stderr.write(
            f"!! непереведённых блоков: {len(failed)} из {n} — оставлены "
            f"исходником; список: {report_path.name}; код возврата 4. "
            "Перезапуск той же команды повторит только их (провалы не "
            "кэшируются).\n")
        # эхо на длинной сессии: hy-mt2-7b после ~350 блоков начинает
        # возвращать исходник; лечит разбиение на части по ~300 блоков
        echoes = sum(1 for f in failed if f.get("reason") == "echo")
        if echoes >= 20 and n > 350:
            sys.stderr.write(
                f"   из них {echoes} — echo (модель вернула исходник). На "
                "длинных книгах hy-mt2-7b «устаёт» после ~350 блоков: разбейте "
                "masked-файл на части по ~300 блоков и переведите каждую "
                "отдельным --cache-dir (см. README, «Длинные книги»).\n")
        sys.exit(4)
    print(f"translated {n} blocks -> {a.dst}  (report: {report_path.name})")


if __name__ == "__main__":
    main()
