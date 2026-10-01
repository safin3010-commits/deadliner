"""
Подробный ответ по учебным материалам — кнопка «🔎 Подробнее», режим
обучения («объясни тему») и запасной путь, когда в обычной сводке ответа нет.

Первая версия отдавала модели инструменты Read/Grep — замер 2026-10-02:
20 шагов блуждания по файлам, ~1 млн токенов на один ответ, время вебинара
взято из файла в чужом поясе. Теперь нужные куски находит КОД (поиск по
основам слов в расшифровках вебинаров, конспектах Нетологии, заметках
вебинаров, оргфактах и переписке), модель получает только их + точное
расписание и отвечает без инструментов: ~25–35 тыс. токенов.
"""
from __future__ import annotations

import glob
import json
import os
import re

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")

MAX_TRANSCRIPT_CHARS = 9000
MAX_CONSPEKT_CHARS = 7000
MAX_NOTES_CHARS = 4000

_STOP = {"объясни", "расскажи", "про", "что", "как", "это", "такое", "тему", "тема", "по", "на", "в", "и",
         "мне", "давай", "было", "был", "была", "вебинаре", "вебинар", "лекции", "лекция", "занятии",
         "продолжим", "учить", "проверь", "меня", "теме", "говорил", "преподаватель", "препод", "ли"}


def _stems(question: str) -> list[str]:
    words = re.findall(r"[\wё]+", question.lower())
    out = []
    for w in words:
        if w in _STOP or len(w) < 3:
            continue
        out.append(w[: max(4, len(w) - 2)] if len(w) > 5 else w)
    return list(dict.fromkeys(out))


def _weights(stems: list[str], texts: list[str]) -> dict:
    """Редкие слова важнее: «оконн» встречается в паре файлов, «sql» — почти
    везде; без весов поиск тонул в общих словах (idf-подобный вес)."""
    import math
    n = max(len(texts), 1)
    lows = [t.lower() for t in texts]
    return {s: math.log((n + 1) / (1 + sum(1 for t in lows if s in t))) + 0.05 for s in stems}


def _score(text_lower: str, w: dict) -> float:
    return sum(min(text_lower.count(s), 20) * k + 5 * k * (s in text_lower) for s, k in w.items())


def _windows(lines: list[str], w: dict, radius: int, budget: int) -> str:
    """Самые «плотные» по совпадениям окна строк, без перекрытий, в пределах бюджета."""
    hits = []
    for i, line in enumerate(lines):
        low = " ".join(lines[max(0, i - 1): i + 2]).lower()
        sc = sum(low.count(s) * k for s, k in w.items())
        if sc > 0.5:
            hits.append((sc, i))
    hits.sort(reverse=True)
    taken, out, used = [], [], 0
    for _, i in hits:
        a, b = max(0, i - radius), min(len(lines), i + radius + 1)
        if any(not (b <= x or a >= y) for x, y in taken):
            continue
        chunk = "\n".join(l.strip() for l in lines[a:b] if l.strip())
        if used + len(chunk) > budget:
            break
        taken.append((a, b))
        out.append((a, chunk))
        used += len(chunk)
    return "\n…\n".join(c for _, c in sorted(out))


def _best_files(paths: list[str], stems: list[str], top: int) -> tuple[list, dict]:
    docs = []
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                docs.append((p, f.read()))
        except OSError:
            continue
    w = _weights(stems, [t for _, t in docs])
    scored = []
    for p, text in docs:
        sc = _score(text.lower(), w) + 20 * _score(os.path.basename(p).lower(), w)
        if sc > 1:
            scored.append((sc, p, text))
    scored.sort(reverse=True)
    return scored[:top], w


def _transcripts(stems) -> str:
    out, used = [], 0
    files, w = _best_files(glob.glob(os.path.join(DATA_DIR, "video_transcripts", "*.txt")), stems, 3)
    for _, path, text in files:
        part = _windows(text.splitlines(), w, 6, (MAX_TRANSCRIPT_CHARS - used) // 2 or 1)
        if part:
            name = os.path.basename(path).replace("_", " ")[:120]
            out.append(f"[расшифровка: {name}]\n{part}")
            used += len(part)
    return "\n\n".join(out)


def _conspekty(stems) -> str:
    paths = glob.glob(os.path.join(PROJECT_DIR, "netology_conspekty", "*.txt"))
    out, used = [], 0
    files, w = _best_files(paths, stems, 2)
    for _, path, text in files:
        part = _windows(text.splitlines(), w, 10, (MAX_CONSPEKT_CHARS - used) // 2 or 1)
        if part:
            out.append(f"[конспект: {os.path.basename(path)}]\n{part}")
            used += len(part)
    return "\n\n".join(out)


def _webinar_notes(stems) -> str:
    out, used = [], 0
    files, w = _best_files(glob.glob(os.path.join(DATA_DIR, "webinar_notes", "*.json")), stems, 2)
    for _, path, text in files:
        try:
            data = json.loads(text)
            flat = json.dumps(data, ensure_ascii=False, indent=0)
        except json.JSONDecodeError:
            flat = text
        part = _windows(flat.splitlines(), w, 4, (MAX_NOTES_CHARS - used) // 2 or 1)
        if part:
            out.append(f"[заметки вебинара: {os.path.basename(path)[:100]}]\n{part}")
            used += len(part)
    return "\n\n".join(out)


def build_materials(question: str) -> str:
    stems = _stems(question)
    if not stems:
        return ""
    blocks = [
        ("РАСШИФРОВКИ ВЕБИНАРОВ (фрагменты, найденные кодом)", _transcripts(stems)),
        ("КОНСПЕКТЫ НЕТОЛОГИИ (фрагменты)", _conspekty(stems)),
        ("ЗАМЕТКИ ВЕБИНАРОВ (фрагменты)", _webinar_notes(stems)),
    ]
    return "\n\n".join(f"=== {t} ===\n{b}" for t, b in blocks if b)


DEEP_SYSTEM = (
    "Ты — личный наставник и репетитор студента. Пишешь по-русски, на «ты», понятно и по делу, "
    "Telegram HTML (только <b> и <i>, без markdown). Все материалы — в сообщении, файлы не читаешь.\n"
    "Правила:\n"
    "- Опирайся на фрагменты расшифровок/конспектов/заметок и отмечай, откуда взял "
    "(«на вебинаре …», «в конспекте курса …»), особенно то, что подчёркивал преподаватель.\n"
    "- Если в материалах темы нет или мало — всё равно объясни из общих знаний, но прямо пометь: "
    "«в материалах курса этого не нашёл, объясняю в общем виде». Не отказывайся объяснять.\n"
    "- Даты и время занятий/дедлайнов бери ТОЛЬКО из блоков расписания/дедлайнов (это местное "
    "время студента); времена из расшифровок и файлов не пересчитывай и не цитируй как расписание.\n"
    "- Объяснение: суть → определения → пример → типичные ошибки → как это проверяют (тест/лаба). "
    "Если тема большая — дай первую часть и предложи продолжить."
)


def deep_answer(question: str, dialog_context: str = "", study: bool = False) -> tuple[str, str]:
    """(ответ, ошибка). Без инструментов — контекст собран кодом."""
    import sys
    sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
    sys.path.insert(0, PROJECT_DIR)
    from claude_session import run_claude_oneshot
    from mentor_context import build_chat_pack

    mode = "Он просит объяснить тему (режим обучения)." if study else \
        "Обычной сводки для ответа не хватило — ответь подробно по материалам."
    prompt = "\n\n".join(p for p in [
        f"ВОПРОС: {question}",
        mode,
        f"=== НЕДАВНИЙ РАЗГОВОР ===\n{dialog_context}" if dialog_context else "",
        build_materials(question) or "(по вопросу фрагментов в материалах не нашлось)",
        build_chat_pack(question),
    ] if p)
    return run_claude_oneshot(prompt, DEEP_SYSTEM, timeout=240, model="claude-sonnet-5",
                              purpose="mentor_study" if study else "mentor_deep")
