#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""procutils.py — общая процесс-гигиена конвейера (Windows).

Выжимка боевых уроков перевода Тейлора (2026-07):

reap_mineru()   MinerU оставляет fast_api-сервер и multiprocessing-воркеров
                после КАЖДОГО запуска (0.6–2.6 ГБ RSS каждый); за очередь глав
                они исчерпывают commit-память Windows — «os error 1455»,
                извлечение падает, сервер перевода сыпет 400/500.
unload_llm()    резидентная LLM и MinerU не делят GPU: thrashing вплоть до
                зависания системы; --vram-limit это НЕ лечит.
run_mineru()    вывод mineru — в ФАЙЛ, не в пайпы: воркеры-сироты наследуют
                pipe-хэндлы, и subprocess.run(capture_output=True) дедлочится
                даже после выхода самого mineru. Плюс таймаут: VLM MinerU
                стохастически виснет в генерации (многочасовые зависания).
PipelineLock    один ГЛОБАЛЬНЫЙ замок на машину: GPU один, а reap_mineru()
                добивает mineru по всей машине — без эксклюзивности прогон A
                убьёт живой mineru прогона B.

Для безопасного reap нужен psutil: широкие CIM-фильтры и убийство произвольных
осиротевших Python-процессов запрещены. Переход к MT ждёт завершения потомков.
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from tex_logs import read_tex_log

TIMEOUT_RC = 124            # конвенция GNU timeout: rc при зависании
_LOCK_ENV = "FREETRANSLATE_LOCK"

# ------------------------------------------------------- unmask acceptance
UNRESOLVED_RE = re.compile(r"unresolved:\s*(\d+)")


def parse_unresolved(text):
    """Число unresolved из вывода mask_math.py unmask («restored exact: N,
    repaired damaged: M, unresolved: K»). None — строка не найдена (вывод не
    захвачен или формат изменился): это сигнал деградации, НЕ ноль."""
    m = UNRESOLVED_RE.search(text or "")
    return int(m.group(1)) if m else None


def untranslated_ok(kept, total, tol=0.02):
    """Можно ли продолжать конвейер, когда translate.py вернул код 4 (часть
    блоков осталась исходником)? Горсть непереведённых блоков (echo на именах
    и библиографии) не должна ронять весь прогон до вёрстки; массовый недобор
    (сервер сыплется) — должна. True = продолжать; kept<=2 или доля<=tol."""
    if kept is None:
        return False
    if kept <= 2:
        return True
    return bool(total) and kept / total <= tol


# ---------------------------------------------------------------- kill tree
def _kill_tree(pid):
    """Убить процесс со всеми потомками. psutil -> taskkill fallback."""
    try:
        import psutil
        try:
            p = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        for c in p.children(recursive=True):
            try:
                c.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    except ImportError:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------- reap
def own_lineage():
    """pid'ы своего процесса и всех предков.

    Одного os.getpid() мало, и это стоило конвейеру «тихой смерти»
    extract_chunked.py, которую не могли объяснить неделю.

    Схема гибели. translate_book.py зовёт дочерний скрипт так:
    «python extract_chunked.py … --mineru-timeout 3600», то есть слово
    «mineru» есть в командной строке САМОГО ДОЧЕРНЕГО ПРОЦЕССА. Дальше
    venv\\Scripts\\python.exe запускает настоящий интерпретатор ОТДЕЛЬНЫМ
    процессом-потомком с той же командной строкой — значит рядом всегда
    живёт двойник, который под фильтр «python + mineru в cmdline» подходит,
    а под исключение «это я» — нет. reap_mineru() берёт этого двойника,
    делает children(recursive=True) — и в детях оказывается тот самый
    процесс, который reap и вызвал. Он убивает сам себя: ни traceback, ни
    кода выхода, ни строчки в логе, смерть ровно перед первым принтом
    run_mineru(). Воспроизведено прямым опытом: тот же скрипт с
    «--mineru-timeout» в argv умирает с кодом 15, без него доживает до конца.
    """
    import psutil
    out, p = set(), psutil.Process()
    for _ in range(32):                     # защита от кольца в дереве
        out.add(p.pid)
        try:
            p = p.parent()
        except psutil.Error:
            break
        if p is None:
            break
    return out


def _mineru_procs_psutil():
    import psutil
    mine = own_lineage()
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.pid in mine or not p.info["name"]:
                continue
            # A flag/path containing "mineru" is NOT process ownership.
            # Generic orphan multiprocessing workers may belong to other apps.
            if is_mineru_command(p.info["cmdline"] or []):
                out.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def is_mineru_command(argv):
    return any(Path(arg).name.lower() in {"mineru", "mineru.exe", "mineru-api", "mineru-api.exe"}
               or arg in {"mineru.cli.client", "mineru.cli.fast_api", "mineru.cli.common"}
               for arg in argv)


def stop_owned_processes(processes, timeout=30):
    """Process handles retain identity across PID reuse. Wait before GPU handoff."""
    import psutil
    victims = {p.pid: p for p in processes if p.pid not in own_lineage()}
    for p in victims.values():
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(victims.values()), timeout=timeout)
    if alive:
        raise RuntimeError("MinerU GPU handoff refused: processes still alive: " +
                           ", ".join(str(p.pid) for p in alive))
    return len(victims)


def _mineru_pids_cim():
    """Fallback без psutil: PowerShell CIM даёт CommandLine (tasklist — нет)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
          "Where-Object { $_.CommandLine -match 'mineru' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        return [int(s) for s in r.stdout.split()
                if s.strip().isdigit() and int(s) != os.getpid()]
    except Exception:
        return []


def reap_mineru():
    """Добить всё от mineru (сервер + воркеры). Возвращает число убитых."""
    n = 0
    try:
        import psutil
        mine = own_lineage()
        victims = []
        for p in _mineru_procs_psutil():
            try:
                for c in p.children(recursive=True):
                    # второй рубеж той же защиты: даже если под фильтр попал
                    # чужой процесс, среди его потомков можем оказаться мы
                    if c.pid in mine:
                        continue
                    victims.append(c)
                victims.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        n = stop_owned_processes(victims)
    except ImportError:
        # CIM cannot prove ancestry and may be unavailable on Windows. Never
        # replace safe ownership checks with a host-wide substring kill.
        raise RuntimeError("safe MinerU cleanup requires psutil; install requirements.txt")
    if n:
        print(f">> добиты осиротевшие процессы MinerU: {n}", flush=True)
    return n


# --------------------------------------------------------------- unload llm
def unload_llm(min_free_gb=8):
    """ОБЯЗАТЕЛЬНО перед извлечением: выгрузить резидентную LLM.
    lms unload --all; JIT вернёт модель при первом запросе перевода."""
    owned_receipt = os.environ.get("FREETRANSLATE_MANAGED_LMSTUDIO_RECEIPT")
    lms = shutil.which("lms") or os.path.expanduser(r"~\.lmstudio\bin\lms.exe")
    if owned_receipt:
        from lmstudio_runtime import unload_owned_models
        receipt = json.loads(Path(owned_receipt).read_text(encoding="utf-8"))
        unload_owned_models(receipt)
        print(">> LLM выгружена через API собственного LM Studio", flush=True)
    elif Path(str(lms)).is_file():
        r = subprocess.run([str(lms), "unload", "--all"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError("LM Studio unload failed: extraction not started")
        status = subprocess.run([str(lms), "ps", "--json"], capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        if status.returncode or json.loads(status.stdout) != []:
            raise RuntimeError("LM Studio still has loaded models: extraction not started")
        if r.returncode == 0:
            print(">> LLM выгружена перед извлечением (lms unload --all)",
                  flush=True)
            # A successful LMS unload says nothing about llama.cpp/Ollama or
            # another GPU workload. Do not start extraction on an occupied GPU.
    try:
        import torch
        if not torch.cuda.is_available():
            print("?? CUDA unavailable: extraction will use CPU", flush=True)
            return
        free, _ = torch.cuda.mem_get_info()
        if free < min_free_gb * 2 ** 30:
            sys.exit(f"!! свободно лишь {free / 2**30:.1f} ГБ VRAM; проверьте "
                     "LM Studio/llama.cpp/Ollama и другие GPU-задачи. Чужие "
                     "процессы не остановлены. Освободите GPU и повторите.")
    except ImportError:
        print("?? нет ни lms, ни torch — убедитесь, что модель выгружена "
              "перед извлечением", flush=True)


# --------------------------------------------------------------- run mineru
def _log_tail(log_path, n=25):
    try:
        lines = read_tex_log(log_path).splitlines()
        print("-- хвост лога mineru --\n" + "\n".join(lines[-n:]), flush=True)
    except OSError:
        pass


def run_mineru(cmd_args, log_path, timeout=1800, env=None, vram_limit=0):
    """mineru с полной гигиеной. Возвращает rc; TIMEOUT_RC=124 при зависании."""
    reap_mineru()                       # чужих под глобальным локом нет
    env = dict(env or os.environ)
    if vram_limit:
        env["MINERU_VIRTUAL_VRAM_SIZE"] = str(vram_limit)
    exe = shutil.which("mineru")
    if not exe:
        print("!! 'mineru' не найден в PATH (venv конвейера активирован?)",
              flush=True)
        return 127
    cmd = [exe] + [str(a) for a in cmd_args]
    if exe.lower().endswith((".bat", ".cmd")):      # CreateProcess не умеет .bat
        cmd = ["cmd", "/c"] + cmd
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(">>", " ".join(cmd),
          f"[лог: {log_path}; таймаут {timeout or '∞'}s]", flush=True)
    with open(log_path, "ab") as lf:
        lf.write(f"\n===== {time.strftime('%F %T')} {' '.join(cmd)} =====\n"
                 .encode("utf-8"))
        lf.flush()
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env)
        import psutil
        tracked = {p.pid: psutil.Process(p.pid)}
        started = time.monotonic()
        try:
            while True:
                for parent in list(tracked.values()):
                    try:
                        for child in parent.children(recursive=True):
                            tracked.setdefault(child.pid, child)
                    except psutil.NoSuchProcess:
                        pass
                try:
                    rc = p.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if timeout and time.monotonic() - started >= timeout:
                        raise
        except subprocess.TimeoutExpired:
            print(f"!! mineru висит > {timeout}s (стохастическое зависание "
                  "VLM) — убиваю дерево процессов", flush=True)
            stop_owned_processes(tracked.values())
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            rc = TIMEOUT_RC
        finally:
            # Includes worker descendants whose parent already exited. Do not
            # rediscover them by an unsafe generic multiprocessing predicate.
            stop_owned_processes(tracked.values())
    reap_mineru()
    if rc:
        _log_tail(log_path)
    return rc


# ------------------------------------------------------------------- lock
class PipelineLock:
    """Глобальный замок конвейера (%TEMP%\\freetranslate.lock).

    Семантика: heartbeat-поток держателя обновляет mtime раз в 60 с;
    замок с mtime старше ttl считается протухшим (держатель погиб) и
    снимается претендентом; если pid держателя жив (psutil), но mtime
    старый — живой процесс не лишается замка по возрасту. Дочерние процессы держателя проходят
    без ожидания через env FREETRANSLATE_LOCK."""

    def __init__(self, name="freetranslate", note="", ttl=600, heartbeat=60):
        base = Path(os.environ.get("TEMP") or tempfile.gettempdir())
        self.path = base / f"{name}.lock"
        self.note, self.ttl, self.hb = note, ttl, heartbeat
        self._stop = threading.Event()
        self._owned = False

    def acquire(self, wait=True, poll=20):
        if os.environ.get(_LOCK_ENV):           # мы — ребёнок держателя
            return self
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "argv": sys.argv,
                               "note": self.note, "started": time.time()}, f)
                self._owned = True
                os.environ[_LOCK_ENV] = str(os.getpid())    # наследуют дети
                threading.Thread(target=self._beat, daemon=True).start()
                atexit.register(self.release)
                return self
            except FileExistsError:
                holder = self._holder()
                if self._stale(holder):
                    print(f">> протухший lock (pid {holder.get('pid')}) — "
                          "снимаю", flush=True)
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if not wait:
                    sys.exit(f"!! уже идёт другой прогон (pid "
                             f"{holder.get('pid')}, {holder.get('note', '')});"
                             f" замок: {self.path}")
                print(f">> жду замок конвейера (держит pid "
                      f"{holder.get('pid')}: "
                      f"{holder.get('note') or ' '.join(holder.get('argv', []))})",
                      flush=True)
                time.sleep(poll)

    def _beat(self):
        while not self._stop.wait(self.hb):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def _holder(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _stale(self, holder):
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False                # исчез — новая попытка захвата
        pid = holder.get("pid")
        # живость PID проверяем РАНЬШЕ возраста mtime: убитый (SIGKILL) держатель
        # не успевает снять замок, а его последний heartbeat свежий — по одному
        # только возрасту замок ложно считался бы живым до ttl (до 10 мин)
        try:
            import psutil
            if pid:
                if not psutil.pid_exists(pid):
                    return True         # держатель мёртв — снять сразу
                return False           # age is not authority to steal a live GPU lock
        except ImportError:
            pass
        return age >= self.ttl          # без psutil — только по возрасту mtime

    def release(self):
        self._stop.set()
        if self._owned:
            self._owned = False
            os.environ.pop(_LOCK_ENV, None)
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
