"""
СТАТУС 2026-10-02: все вызовы проекта переведены на run_claude_oneshot
(разовый вызов с готовым контекстом, без дневной сессии) — --resume сессии
дня каждый раз пересылал весь разговор дня (0,3–1,3 млн токенов на чек-ин).
Связность теперь даёт data/agent.db (dialog_messages, события) и
data/mentor_sent.json. run_claude_session оставлен для совместимости.

Общая ВНУТРИДНЕВНАЯ сессия Claude CLI — чтобы утренняя сводка, дневная,
вечерняя и суждения "что сейчас срочно" не начинались каждый раз "с чистого
листа", а реально помнили, что уже обсуждалось сегодня (через `claude -p
--resume <id>`), как и просил пользователь.

Это НЕ замена data/ai_memory_log.json + data/ai_memory_profile.txt в
scheduler.py — тот механизм решает другую задачу: память на недели/месяцы
вперёд, где растущая история сессии не влезла бы в окно контекста и дорожала
бы с каждым днём. Здесь горизонт — один календарный день (по UFA_TZ), на
следующее утро сессия заводится заново, поэтому её длина ограничена сама
собой. При смене дня в первый промпт новой сессии подмешивается короткое
резюме через тот же ai_memory-рекап, чтобы нить не обрывалась на стыке суток
— долгосрочная память и внутридневная сессия дополняют друг друга, а не
конкурируют.

Использовать вместо прямого subprocess.run(["claude", "-p", ...]) в местах,
где реально нужно суждение модели с учётом происходившего сегодня (сводки,
проверка "что срочного" в чек-инах и почасовом стороже) — НЕ для мелких
одноразовых структурных извлечений без контекста дня (для тех прежний
разовый вызов остаётся дешевле и проще).

    from claude_session import run_claude_session, run_claude_session_async

    text, err = run_claude_session(prompt, timeout=60, model=...)          # sync
    text, err = await run_claude_session_async(prompt, timeout=60, ...)    # async (scheduler.py)
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import uuid

from data_lock import atomic_write_json, file_lock

try:
    from config import UFA_TZ
except Exception:  # pragma: no cover - на случай запуска вне проекта
    import zoneinfo
    UFA_TZ = zoneinfo.ZoneInfo("Asia/Yekaterinburg")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(PROJECT_DIR, "data", "claude_session.json")
AI_MEMORY_LOG_FILE = os.path.join(PROJECT_DIR, "data", "ai_memory_log.json")
AI_MEMORY_PROFILE_FILE = os.path.join(PROJECT_DIR, "data", "ai_memory_profile.txt")

CLAUDE_MODEL_FAST = "claude-haiku-4-5-20251001"
AGENT_CALLS_FILE = os.path.join(PROJECT_DIR, "data", "agent_calls.jsonl")


def _parse_cli_json(stdout: str) -> tuple[str, dict]:
    """`claude -p --output-format json` → (текст ответа, метаданные).
    Если вывод не JSON (старая версия CLI) — весь stdout считается текстом."""
    try:
        data = json.loads(stdout or "")
        if isinstance(data, dict):
            return (data.get("result") or "").strip(), data
    except json.JSONDecodeError:
        pass
    return (stdout or "").strip(), {}


def log_agent_call(purpose: str, mode: str, model: str, meta: dict, ok: bool, started: float):
    """Журнал расходов: data/agent_calls.jsonl, одна строка на вызов Claude.
    Шаг 1 плана (design/mentor_agent_architecture.md) — сначала мерим."""
    import time
    usage = meta.get("usage") or {}
    row = {
        "at": datetime.datetime.now(tz=UFA_TZ).isoformat(timespec="seconds"),
        "purpose": purpose or "?",
        "mode": mode,
        "model": model,
        "ok": ok,
        "input": usage.get("input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "turns": meta.get("num_turns"),
        "cost_usd_equiv": meta.get("total_cost_usd"),
        "seconds": round(time.time() - started, 1),
    }
    row["total"] = row["input"] + row["cache_read"] + row["cache_write"] + row["output"]
    try:
        with open(AGENT_CALLS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run_claude_oneshot(
    prompt: str,
    system_prompt: str,
    timeout: int = 300,
    model: str = "claude-sonnet-5",
    purpose: str = "",
) -> tuple[str, str]:
    """Разовый вызов БЕЗ дневной сессии и без инструментов: весь нужный
    контекст уже в prompt (context pack, scripts/mentor_context.py).
    system_prompt — статичная часть (персона, правила, формат): она
    одинаковая между вызовами и кэшируется."""
    import time
    claude_bin = _find_claude_bin()
    if not os.path.isfile(claude_bin):
        return "", "claude CLI не найден"
    cmd = [claude_bin, "-p", "--no-session-persistence", "--tools", "",
           "--model", model, "--output-format", "json", "--system-prompt", system_prompt]
    started = time.time()
    try:
        result = subprocess.run(cmd, input=prompt, cwd=PROJECT_DIR, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log_agent_call(purpose, "oneshot", model, {}, False, started)
        return "", f"не ответил за {timeout}с"
    except Exception as e:
        return "", f"ошибка запуска claude: {e!r}"
    text, meta = _parse_cli_json(result.stdout)
    ok = result.returncode == 0 and bool(text) and not meta.get("is_error")
    log_agent_call(purpose, "oneshot", model, meta, ok, started)
    if not ok:
        reason = _classify_failure((result.stderr or "") + " " + (result.stdout or "")[:300])
        return "", f"{reason} (returncode={result.returncode})"
    return text, ""


def _find_claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _today() -> str:
    return datetime.datetime.now(tz=UFA_TZ).strftime("%Y-%m-%d")


def _load_state() -> dict:
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _yesterday_recap() -> str:
    """Короткое резюме прошлого дня для первого промпта новой сессии — та же
    идея, что get_ai_memory_recap() в scheduler.py, читаем те же файлы
    напрямую (не импортируем scheduler.py отсюда, чтобы не тянуть цикл
    импортов: scheduler.py сам импортирует этот модуль)."""
    parts = []
    try:
        with open(AI_MEMORY_PROFILE_FILE, encoding="utf-8") as f:
            profile = f.read().strip()
        if profile:
            parts.append(f"Долгосрочная память о студенте:\n{profile}")
    except Exception:
        pass
    try:
        with open(AI_MEMORY_LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
        recent = log[-5:] if isinstance(log, list) else []
        if recent:
            lines = "\n".join(
                f"- [{e.get('source','')} {e.get('at','')[:10]}] {e.get('note','')}" for e in recent
            )
            parts.append(f"Последние записи за прошлые дни:\n{lines}")
    except Exception:
        pass
    if not parts:
        return ""
    return (
        "Новый день, новая сессия — вот короткое резюме, чтобы не терять нить "
        "с прошлых дней:\n" + "\n\n".join(parts)
    )


def _classify_failure(stderr: str) -> str:
    import re
    if re.search(r"limit|usage|quota|429", stderr, re.IGNORECASE):
        return "лимит подписки исчерпан"
    if re.search(r"network|connection|resolve|dns|econnrefused|enotfound|unreachable", stderr, re.IGNORECASE):
        return "нет соединения с сетью"
    if re.search(r"no conversation found|session.*not found|unknown session", stderr, re.IGNORECASE):
        return "сессия не найдена (продолжим новой)"
    return "ошибка claude"


def run_claude_session(
    prompt: str,
    timeout: int = 60,
    model: str = CLAUDE_MODEL_FAST,
    allowed_tools: str = "",
    append_system_prompt: str | None = None,
    purpose: str = "",
) -> tuple[str, str]:
    """Синхронный вызов `claude -p` с продолжением сегодняшней сессии.
    Никогда не поднимает исключение наружу — как _run_claude в
    mentor_checkin.py, отсутствие сети/лимит/зависание превращаются в
    пустой текст + понятную причину."""
    claude_bin = _find_claude_bin()
    if not os.path.isfile(claude_bin):
        return "", "claude CLI не найден"

    with file_lock(SESSION_FILE):
        state = _load_state()
        today = _today()
        is_new_day = state.get("date") != today or not state.get("session_id")

        if is_new_day:
            session_id = str(uuid.uuid4())
            recap = _yesterday_recap() if state.get("date") else ""
            full_prompt = f"{recap}\n\n{prompt}" if recap else prompt
            session_flags = ["--session-id", session_id]
        else:
            session_id = state["session_id"]
            full_prompt = prompt
            session_flags = ["--resume", session_id]

        cmd = [
            claude_bin, "-p", full_prompt,
            "--allowedTools", allowed_tools,
            "--permission-prompts", "none",
            "--model", model,
            "--output-format", "json",
            *session_flags,
        ]
        if append_system_prompt:
            cmd += ["--append-system-prompt", append_system_prompt]

        import time
        started = time.time()
        try:
            result = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log_agent_call(purpose, "session", model, {}, False, started)
            return "", f"не ответил за {timeout}с"
        except Exception as e:
            return "", f"ошибка запуска claude: {e!r}"

        text, meta = _parse_cli_json(result.stdout)
        ok = bool(text) and result.returncode == 0 and not meta.get("is_error")
        log_agent_call(purpose, "session", model, meta, ok, started)

        if ok:
            atomic_write_json(SESSION_FILE, {
                "date": today,
                "session_id": session_id,
                "updated_at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
            })
        elif not is_new_day:
            # --resume не нашёл сессию (например, файл истории потёрли
            # руками) — не застреваем в вечной ошибке, следующий вызов
            # начнёт новый день/новую сессию сам.
            stderr_probe = (result.stderr or "")
            if "no conversation found" in stderr_probe.lower() or "not found" in stderr_probe.lower():
                atomic_write_json(SESSION_FILE, {})

    if not ok:
        stderr = (result.stderr or "")[:500]
        reason = _classify_failure(stderr)
        return "", f"{reason} (returncode={result.returncode}, текст {'есть' if text else 'пуст'})"
    return text, ""


async def run_claude_session_async(
    prompt: str,
    timeout: int = 60,
    model: str = CLAUDE_MODEL_FAST,
    allowed_tools: str = "",
    append_system_prompt: str | None = None,
    purpose: str = "",
) -> tuple[str, str]:
    """Async-обёртка для scheduler.py (event loop бота) — сама блокирующая
    работа (включая file_lock) уходит в отдельный поток, как уже сделано для
    IMAP в parsers/mail.py."""
    import asyncio
    return await asyncio.to_thread(
        run_claude_session, prompt, timeout, model, allowed_tools, append_system_prompt, purpose
    )
