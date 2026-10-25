#!/usr/bin/env python3
"""Mock-server tests for translate.py v3.4. Exit 1 on failure.

Scenarios:
  1. validator retry — damaged mask token in the first reply is rejected,
     the clean second reply lands in the output, nothing damaged cached;
  2. polish mode — literary-editor system prompt reaches the server,
     the polished reply is written;
  3. fail-fast — dead server (always 500): exit 3, dst NOT written
     («keeping source» must not silently produce an English book);
  4. api_url unit — the three host forms;
  5. partial failure — one block always 500: exit 4, dst written with that
     block kept as source, sidecar report names it, failure not cached;
  6. echo — a reply that just parrots the source is rejected (exit 4),
     but short blocks and polish mode are exempt.
"""
import json
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from translate import api_url  # noqa: E402

TRANSLATE = str(ROOT / "scripts" / "translate.py")

DAMAGED = "Пусть ⟨M000001⟩ обозначает сумму."
CLEAN = "Пусть ⟦M000001⟧ обозначает сумму."


class Handler(BaseHTTPRequestHandler):
    """Поведение задаётся сервером: server.behavior(call_no, payload)
    -> (http_status, reply_text)."""

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = json.loads(raw.decode("utf-8"))
        srv = self.server
        srv.calls.append(payload)
        status, reply = srv.behavior(len(srv.calls), payload)
        body = json.dumps(
            {"choices": [{"message": {"content": reply}}]}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def start_server(behavior):
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    srv.behavior = behavior
    srv.calls = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def run_translate(src_text, port, extra=(), timeout=90):
    tmp = Path(tempfile.mkdtemp())
    src, dst = tmp / "in.masked.md", tmp / "out.masked.md"
    src.write_text(src_text, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-X", "utf8", TRANSLATE, str(src), str(dst),
         "--host", f"http://127.0.0.1:{port}",
         "--cache-dir", str(tmp / ".tcache"), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return r, dst


def read_report(dst):
    p = Path(str(dst) + ".report.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def user_text(payload):
    return "".join(m.get("content", "") for m in payload.get("messages", [])
                   if m.get("role") == "user")


def scenario_validator():
    srv, port = start_server(
        lambda n, _p: (200, DAMAGED if n == 1 else CLEAN))
    r, dst = run_translate("Let ⟦M000001⟧ denote the sum.", port)
    srv.shutdown()
    out = dst.read_text(encoding="utf-8") if dst.exists() else "<missing>"
    return [
        ("validator: repaired and validated on first call", len(srv.calls) == 1),
        ("validator: final output is the clean reply", out == CLEAN),
        ("validator: repaired answer in versioned cache",
         len(list((dst.parent / ".tcache").glob("*.json"))) == 1),
        ("validator: exit code 0", r.returncode == 0),
    ], r


def scenario_polish():
    polished = "Отполированный абзац с ⟦M000001⟧ внутри."
    srv, port = start_server(lambda n, _p: (200, polished))
    r, dst = run_translate(
        "Корявый абзац, содержащий ⟦M000001⟧ внутри себя имеет.",
        port, extra=["--mode", "polish"])
    srv.shutdown()
    out = dst.read_text(encoding="utf-8") if dst.exists() else "<missing>"
    sys_prompt = ""
    if srv.calls:
        msgs = srv.calls[0].get("messages", [])
        sys_prompt = next((m["content"] for m in msgs
                           if m.get("role") == "system"), "")
    return [
        ("polish: editor system prompt sent",
         "literary editor" in sys_prompt),
        ("polish: polished reply written", out == polished),
        ("polish: exit code 0", r.returncode == 0),
    ], r


def scenario_fail_fast():
    srv, port = start_server(lambda n, _p: (500, "boom"))
    blocks = "\n\n".join(f"Абзац номер {i} про физику." for i in range(12))
    r, dst = run_translate(blocks, port, extra=["--parallel", "12"])
    srv.shutdown()
    return [
        ("fail-fast: exit code 3", r.returncode == 3),
        ("fail-fast: dst not written", not dst.exists()),
        ("fail-fast: dead-server message",
         "output not published; cache retained" in r.stderr),
    ], r


LONG_EN = ("Newton's second law states that the net force acting on a body "
           "equals the rate of change of its momentum, and in the special "
           "case of constant mass this reduces to the familiar product of "
           "mass and acceleration, which is the workhorse of every "
           "introductory mechanics course ever taught anywhere.")


def scenario_partial_failure():
    """Один блок сервер валит всегда: остальные переводятся, dst пишется,
    но код 4 и sidecar называет виновника — раньше это был код 0 и тишина."""
    def behavior(n, payload):
        if "Блок-семь" in user_text(payload):
            return 500, "boom"
        # Successful fixture replies must preserve source numbers too; only
        # block seven is intentionally bad in this partial-failure scenario.
        return 200, "Переведённый абзац. " + " ".join(re.findall(r"\d+", user_text(payload)))

    srv, port = start_server(behavior)
    blocks = [f"Абзац номер {i} про механику и движение тел." for i in range(12)]
    blocks[6] = "Блок-семь: этот абзац сервер отказывается переводить."
    r, dst = run_translate("\n\n".join(blocks), port, extra=["--parallel", "3"])
    srv.shutdown()
    out = dst.read_text(encoding="utf-8") if dst.exists() else ""
    rep = read_report(dst)
    cache_dir = dst.parent / ".tcache"
    n_cached = len(list(cache_dir.glob("*.json"))) if cache_dir.is_dir() else 0
    return [
        ("partial: exit code 4", r.returncode == 4),
        ("partial: dst written", dst.exists()),
        ("partial: failed block kept as source", "Блок-семь" in out),
        ("partial: other blocks translated", out.count("Переведённый абзац.") == 11),
        ("partial: report kept_source=1 with the right index",
         bool(rep) and rep["kept_source"] == 1
         and rep["failed_blocks"][0]["index"] == 7),
        ("partial: failure not cached (11 of 12 blocks)", n_cached == 11),
        ("partial: loud stderr line",
         "непереведённых блоков: 1" in r.stderr),
    ], r


def scenario_echo_rejected():
    """Сервер возвращает исходник — это не перевод: брак, ретрай, код 4."""
    srv, port = start_server(lambda n, p: (200, LONG_EN))
    r, dst = run_translate(LONG_EN, port)
    srv.shutdown()
    rep = read_report(dst)
    return [
        ("echo: three attempts made", len(srv.calls) == 3),
        ("echo: exit code 4", r.returncode == 4),
        ("echo: reason recorded",
         bool(rep) and rep["failed_blocks"] == [{"index": 1, "reason": "echo"}]),
        ("echo: retry marker in stderr", "retry 1: echo" in r.stderr),
    ], r


def scenario_echo_short_ok():
    """Короткий блок (имя, подпись) — эхо-проверка не судит: код 0."""
    short = "Isaac Newton and ⟦M000001⟧."
    srv, port = start_server(lambda n, p: (200, short))
    r, dst = run_translate(short, port)
    srv.shutdown()
    return [
        ("echo-short: one call, no retries", len(srv.calls) == 1),
        ("echo-short: exit code 0", r.returncode == 0),
    ], r


def scenario_polish_echo_ok():
    """В polish «вернуть как есть» — штатный ответ, эхо-проверка выключена."""
    ru = ("Второй закон Ньютона гласит, что результирующая сила, действующая "
          "на тело, равна скорости изменения его импульса, а в частном случае "
          "постоянной массы сводится к произведению массы на ускорение.")
    srv, port = start_server(lambda n, p: (200, ru))
    r, dst = run_translate(ru, port, extra=["--mode", "polish"])
    srv.shutdown()
    return [
        ("polish-echo: no retries", len(srv.calls) == 1),
        ("polish-echo: exit code 0", r.returncode == 0),
    ], r


def scenario_api_url():
    return [
        ("api_url: bare host",
         api_url("http://localhost:1234")
         == "http://localhost:1234/v1/chat/completions"),
        ("api_url: version already in path",
         api_url("https://generativelanguage.googleapis.com/v1beta/openai")
         == "https://generativelanguage.googleapis.com/v1beta/openai"
            "/chat/completions"),
        ("api_url: full endpoint untouched",
         api_url("http://h/v1/chat/completions")
         == "http://h/v1/chat/completions"),
    ], None


def scenario_token_retry_hint():
    """После битого маркера в промпт добавляется резкая просьба про маркеры.

    Повторять ту же общую инструкцию бессмысленно: модель её уже нарушила.
    """
    srv, port = start_server(
        lambda n, _p: (200, "Пусть M000001X обозначает сумму." if n == 1 else CLEAN))
    r, dst = run_translate("Let ⟦M000001⟧ denote the sum.", port)
    srv.shutdown()
    sys1 = "".join(m.get("content", "") for m in srv.calls[0]["messages"]
                   if m.get("role") == "system")
    sys2 = "".join(m.get("content", "") for m in srv.calls[1]["messages"]
                   if m.get("role") == "system")
    return [
        ("token-retry: в первом запросе резкой просьбы нет",
         "CHARACTER BY CHARACTER" not in sys1),
        ("token-retry: во втором запросе она есть",
         "CHARACTER BY CHARACTER" in sys2),
    ], r


def scenario_damaged_recoverable_kept():
    """Known bracket damage is repaired before the SAME full validation gate."""
    srv, port = start_server(lambda n, _p: (200, DAMAGED))
    r, dst = run_translate("Let ⟦M000001⟧ denote the sum.", port)
    srv.shutdown()
    out = dst.read_text(encoding="utf-8") if dst.exists() else "<missing>"
    rep = read_report(dst)
    return [
        ("recoverable: одна проверенная попытка", len(srv.calls) == 1),
        ("recoverable: принята нормализованная маска", out == CLEAN),
        ("recoverable: блок не числится потерянным",
         bool(rep) and rep["kept_source"] == 0),
        ("recoverable: код 0", r.returncode == 0),
    ], r


def scenario_cjk_passthrough():
    """CJK в ответе законен, если он есть в ИСХОДНИКЕ.

    Прежде валидатор заворачивал такой ответ трижды, и английский блок с
    китайским именем оставался непереведённым.
    """
    src = "The term 证明 appears in the Chinese edition of this book."
    reply = "Термин 证明 встречается в китайском издании этой книги."
    srv, port = start_server(lambda n, _p: (200, reply))
    r, dst = run_translate(src, port)
    srv.shutdown()
    out = dst.read_text(encoding="utf-8") if dst.exists() else "<missing>"
    return [
        ("cjk-passthrough: одна попытка, без повторов", len(srv.calls) == 1),
        ("cjk-passthrough: перевод принят", out == reply),
        ("cjk-passthrough: код 0", r.returncode == 0),
    ], r


def scenario_cjk_leak_still_rejected():
    """А вот CJK, которого в исходнике НЕТ, по-прежнему брак."""
    srv, port = start_server(
        lambda n, _p: (200, "Пусть 证明 обозначает сумму."
                       if n == 1 else "Пусть S обозначает сумму."))
    r, dst = run_translate("Let S denote the sum of the series.", port)
    srv.shutdown()
    out = dst.read_text(encoding="utf-8") if dst.exists() else "<missing>"
    return [
        ("cjk-leak: пересэмплировано", len(srv.calls) == 2),
        ("cjk-leak: в выходе чистый ответ",
         out == "Пусть S обозначает сумму."),
        ("cjk-leak: причина названа", "CJK" in r.stderr),
    ], r


def test_group_blocks_keeps_tables_and_images_alone():
    """Grouping may only join short paragraphs; tables and pictures stay whole."""
    from translate import group_blocks, split_blocks
    text = ("First paragraph.\n\nSecond paragraph.\n\n"
            "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
            "![](images/x.jpg)\n\nThird paragraph.")
    blocks = split_blocks(text)
    assert group_blocks(blocks, 0) == blocks          # off by default
    grouped = group_blocks(blocks, 40)
    assert "\n\n".join(grouped) == text.strip()        # no text lost or reordered
    assert any(g.startswith("First paragraph.") and "Second paragraph." in g for g in grouped)
    assert any("| a | b |" in g for g in grouped)
    assert any("![](images/x.jpg)" in g for g in grouped)
    # a table block is never merged with a paragraph
    for g in grouped:
        if g.lstrip().startswith("|"):
            assert g.count("\n\n") == 0


def main():
    failed = total = 0
    for scenario in (scenario_validator, scenario_polish,
                     scenario_fail_fast, scenario_partial_failure,
                     scenario_echo_rejected, scenario_echo_short_ok,
                     scenario_polish_echo_ok, scenario_api_url,
                     scenario_token_retry_hint,
                     scenario_damaged_recoverable_kept,
                     scenario_cjk_passthrough,
                     scenario_cjk_leak_still_rejected):
        checks, r = scenario()
        bad = False
        for name, ok in checks:
            print(("PASS" if ok else "FAIL"), name)
            total += 1
            failed += not ok
            bad |= not ok
        if bad and r is not None:
            print("--- stderr ---\n", r.stderr[-2000:])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
