"""
Быстрый Q&A через headless Claude Code — используется кнопкой "💬 Вопрос" в боте.
Тот же принцип, что у scripts/mentor_checkin.py: отдельный процесс claude -p,
только чтение data/*.json, никакого Bash. Не требует отдельного API-ключа —
использует локальную подписку Claude Code.
"""
import asyncio
import datetime
import json
import os
import re
import shutil

from config import USER_NAME, UFA_TZ

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
QA_LOG_FILE = os.path.join(PROJECT_DIR, "data", "mentor_qa_log.json")
QA_LOG_MAX = 20


def _find_claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _load_qa_log() -> list:
    try:
        with open(QA_LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _append_qa_log(question: str, answer: str):
    log = _load_qa_log()
    log.append({
        "at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
        "q": question,
        "a": answer,
    })
    log = log[-QA_LOG_MAX:]
    os.makedirs(os.path.dirname(QA_LOG_FILE), exist_ok=True)
    with open(QA_LOG_FILE, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


async def ask_mentor(question: str, dialog_context: str = "", study: bool = False) -> str:
    """Вопрос наставнику (Raycast, бот). Раньше — claude -p с Read/Grep по
    файлам: замер 2026-10-02 — 20 шагов и ~1 млн токенов на ответ. Теперь
    нужные фрагменты расшифровок/конспектов/данных находит код
    (scripts/mentor_deep.py), модель отвечает без инструментов (~25–35 тыс.)."""
    import sys
    scripts = os.path.join(PROJECT_DIR, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from mentor_deep import deep_answer

    if not dialog_context:
        recent = _load_qa_log()[-4:]
        dialog_context = "\n".join(f"Вопрос: {r['q']}\nОтвет: {r['a'][:300]}" for r in recent)
    study = study or bool(re.search(r"^(объясни|расскажи|разбер|научи|продолжим|давай уч)", question.strip().lower()))
    try:
        text, err = await asyncio.to_thread(deep_answer, question, dialog_context, study)
    except Exception as e:
        return f"❌ Наставник не смог ответить (ошибка: {e})"
    if err:
        return f"❌ Наставник не смог ответить ({err})"
    _append_qa_log(question, text)
    return text
