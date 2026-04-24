"""
Модуль теории по предметам и английскому.
"""
import datetime
import json
import os
import re
from config import UFA_TZ

THEORY_SEEN_FILE = "data/theory_seen.json"
WORD_OF_DAY_FILE = "data/word_of_day.json"


def _load_word_of_day() -> dict:
    """Загружаем слово дня."""
    try:
        with open(WORD_OF_DAY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_word_of_day(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(WORD_OF_DAY_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


async def _fetch_word_of_day() -> dict:
    """Получаем слово дня с API Ninjas или генерируем через ИИ."""
    import datetime as _dt
    from config import UFA_TZ
    today = _dt.datetime.now(tz=UFA_TZ).date().isoformat()

    # Проверяем кэш
    cached = _load_word_of_day()
    if cached.get("date") == today and cached.get("word"):
        return cached

    word_data = {}
    # Пробуем API Ninjas
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.api-ninjas.com/v1/randomword",
                headers={"X-Api-Key": ""}
            )
            if r.status_code == 200:
                data = r.json()
                word = data.get("word", "")
                if word and word.isalpha() and 4 <= len(word) <= 12:
                    word_data["word"] = word
    except Exception as e:
        print(f"Word API error: {e}")

    # Если API не сработал — генерируем через ИИ
    if not word_data.get("word"):
        try:
            from grok import ask_grok
            seen = _load_seen()
            words_history = _get_words_history(seen)
            prompt = (
                f"Дай одно английское слово уровня Pre-Intermediate для изучения.\n"
                f"{words_history + chr(10) if words_history else ''}"
                f"Ответь ТОЛЬКО одним словом на английском, без пояснений."
            )
            word = await ask_grok(prompt)
            if word:
                word_data["word"] = word.strip().lower().split()[0]
        except Exception as e:
            print(f"Word ИИ error: {e}")

    if not word_data.get("word"):
        return {}

    # Генерируем полные данные через ИИ
    try:
        from grok import ask_grok
        word = word_data["word"]
        prompt = (
            f"Слово: {word}\n"
            f"Дай строго в таком формате (без отступлений):\n"
            f"WORD: {word}\n"
            f"TRANSCRIPTION: /транскрипция МФА/\n"
            f"TRANSLATION: перевод на русский\n"
            f"EXAMPLE: пример предложения на английском\n"
            f"EXAMPLE_RU: перевод примера на русский\n"
            f"Только эти 5 строк, ничего лишнего."
        )
        result = await ask_grok(prompt, system="Ты словарь английского языка. Отвечай строго по формату.")
        if result:
            lines = {l.split(":")[0].strip(): ":".join(l.split(":")[1:]).strip()
                     for l in result.strip().split("\n") if ":" in l}
            word_data = {
                "date": today,
                "word": lines.get("WORD", word),
                "transcription": lines.get("TRANSCRIPTION", ""),
                "translation": lines.get("TRANSLATION", ""),
                "example": lines.get("EXAMPLE", ""),
                "example_ru": lines.get("EXAMPLE_RU", ""),
            }
            _save_word_of_day(word_data)
            print(f"study_theory: слово дня — {word_data['word']}")
    except Exception as e:
        print(f"Word data error: {e}")

    return word_data

SUBJECT_PRIORITY = [
    ("english", ["английский", "english", "иностранный"]),
    ("discrete", ["дискретная", "discrete"]),
    ("matan", ["математический анализ", "матанализ", "matan"]),
    ("networks", ["компьютерные сети", "сети"]),
    ("history", ["история", "history"]),
    ("cpp", ["программирование", "алгоритмизация", "c++", "cpp"]),
]

THEORY_SUBJECTS = ["discrete", "matan", "networks", "history", "cpp"]

# Расписание предметов по дням недели (0=пн, 6=вс)
WEEKDAY_SUBJECT = {
    0: "matan",      # понедельник
    1: "networks",   # вторник
    2: "cpp",        # среда
    3: "discrete",   # четверг
    4: "matan",      # пятница
    5: "networks",   # суббота
    6: "cpp",        # воскресенье
}

# Полный список грамматических правил для цикличного обхода
GRAMMAR_RULES = [
    "Present Simple (факты, привычки, расписание)",
    "Present Continuous (действие прямо сейчас, планы)",
    "Past Simple (завершённое действие в прошлом)",
    "Past Continuous (действие в процессе в прошлом)",
    "Present Perfect (опыт, результат, недавнее прошлое)",
    "Present Perfect Continuous (действие началось в прошлом и продолжается)",
    "Past Perfect (действие до другого прошлого действия)",
    "Future Simple will (спонтанные решения, предсказания)",
    "Future be going to (планы, намерения)",
    "Артикли a/an/the/zero article",
    "Исчисляемые и неисчисляемые существительные",
    "Модальные глаголы can/could (умение, возможность)",
    "Модальные глаголы must/have to (обязанность)",
    "Модальные глаголы should/ought to (совет)",
    "Модальные глаголы may/might (вероятность)",
    "Условные предложения 0 типа (всегда верные факты)",
    "Условные предложения 1 типа (реальное условие в будущем)",
    "Условные предложения 2 типа (нереальное условие в настоящем)",
    "Условные предложения 3 типа (нереальное условие в прошлом)",
    "Пассивный залог Present Simple Passive",
    "Пассивный залог Past Simple Passive",
    "Герундий vs инфинитив (after/before/enjoy vs want/hope/plan)",
    "Относительные придаточные who/which/that/where",
    "Косвенная речь (reported speech) — утверждения",
    "Косвенная речь — вопросы",
    "Сравнительные и превосходные степени прилагательных",
    "Наречия частотности (always/usually/often/never)",
    "Предлоги времени in/on/at",
    "Предлоги места in/on/at/under/between",
    "Предлоги движения to/into/out of/through",
    "Фразовые глаголы с get (get up, get on, get through...)",
    "Фразовые глаголы с take (take off, take up, take back...)",
    "Фразовые глаголы с make/do (make up, do up, do without...)",
    "Порядок слов в вопросах (вспомогательный глагол)",
    "Разделительные вопросы (question tags)",
    "Much/many/a lot of/a little/a few",
    "Some/any/no/every и производные",
    "Притяжательный падеж (possessive case: -'s)",
    "Числительные: порядковые и количественные",
    "Выражение времени: ago / before / for / since",
]

# Список тем для ошибок (grammar_errors) — цикличный
GRAMMAR_ERROR_TOPICS = [
    "артикли a/an/the — самые частые пропуски",
    "Present Perfect vs Past Simple — когда что выбрать",
    "путаница he/she/it у русскоязычных",
    "порядок слов в вопросах без вспомогательного глагола",
    "do/make — разница и типичные ошибки",
    "say/tell/speak/talk — путаница в значениях",
    "предлоги in/on/at со временем",
    "предлоги in/on/at с местом",
    "since vs for — с какого момента vs сколько длится",
    "must vs have to — обязанность внутренняя vs внешняя",
    "will vs going to — спонтанное vs запланированное",
    "инфинитив vs герундий после глаголов",
    "much/many/a lot of — с исчисляемыми и неисчисляемыми",
    "some/any — в утверждениях и вопросах",
    "глагол to be в Present Simple — пропуск у русскоязычных",
    "двойное отрицание (I don't know nothing → anything)",
    "another vs other vs others",
    "already/yet/still — позиция и значение",
    "too vs enough — порядок с прилагательным",
    "used to vs be used to — привычка в прошлом vs привычка сейчас",
]


def _load_seen() -> dict:
    try:
        with open(THEORY_SEEN_FILE) as f:
            data = json.load(f)
            today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
            if data.get("date") != today:
                return {
                    "date": today,
                    "subject_sent": [],
                    "english_count": 0,
                    "topics_history": data.get("topics_history", []),
                    "words_history": data.get("words_history", []),
                    "grammar_index": data.get("grammar_index", 0),
                    "grammar_errors_index": data.get("grammar_errors_index", 0),
                    "phrasal_history": data.get("phrasal_history", []),
                    "idioms_history": data.get("idioms_history", []),
                }
            return data
    except Exception:
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return {
            "date": today,
            "subject_sent": [],
            "english_count": 0,
            "topics_history": [],
            "words_history": [],
            "grammar_index": 0,
            "grammar_errors_index": 0,
            "phrasal_history": [],
            "idioms_history": [],
        }


def _save_seen(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(THEORY_SEEN_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _get_topics_history(seen: dict) -> str:
    history = seen.get("topics_history", [])
    if not history:
        return ""
    return "СТРОГО ЗАПРЕЩЕНО повторять эти темы — выбери другую: " + "; ".join(history[-20:])


def _get_words_history(seen: dict) -> str:
    words = seen.get("words_history", [])
    if not words:
        return ""
    return "Эти слова уже были — НЕ используй их ни в каком виде: " + ", ".join(words[-500:])


def _get_phrasal_history(seen: dict) -> str:
    history = seen.get("phrasal_history", [])
    if not history:
        return ""
    return "Эти фразовые глаголы уже были — не повторяй: " + ", ".join(history[-60:])


def _get_idioms_history(seen: dict) -> str:
    history = seen.get("idioms_history", [])
    if not history:
        return ""
    return "Эти идиомы уже были — не повторяй: " + ", ".join(history[-60:])


def _get_next_grammar_rule(seen: dict) -> tuple[str, int]:
    """Возвращает следующее правило и новый индекс (цикличный)."""
    idx = seen.get("grammar_index", 0) % len(GRAMMAR_RULES)
    rule = GRAMMAR_RULES[idx]
    new_idx = (idx + 1) % len(GRAMMAR_RULES)
    return rule, new_idx


def _get_next_grammar_error_topic(seen: dict) -> tuple[str, int]:
    """Возвращает следующую тему для ошибок и новый индекс (цикличный)."""
    idx = seen.get("grammar_errors_index", 0) % len(GRAMMAR_ERROR_TOPICS)
    topic = GRAMMAR_ERROR_TOPICS[idx]
    new_idx = (idx + 1) % len(GRAMMAR_ERROR_TOPICS)
    return topic, new_idx


def _add_words_to_history(seen: dict, words: list):
    history = seen.get("words_history", [])
    for w in words:
        w = w.strip().lower()
        if w and len(w) > 2 and w not in history:
            history.append(w)
    if len(history) > 500:
        history = history[-500:]
    seen["words_history"] = history


def _add_phrasal_to_history(seen: dict, phrasals: list):
    history = seen.get("phrasal_history", [])
    for w in phrasals:
        w = w.strip().lower()
        if w and w not in history:
            history.append(w)
    if len(history) > 100:
        history = history[-100:]
    seen["phrasal_history"] = history


def _add_idioms_to_history(seen: dict, idioms: list):
    history = seen.get("idioms_history", [])
    for w in idioms:
        w = w.strip().lower()
        if w and w not in history:
            history.append(w)
    if len(history) > 100:
        history = history[-100:]
    seen["idioms_history"] = history


def _add_to_history(seen: dict, topic: str):
    history = seen.get("topics_history", [])
    if topic not in history:
        history.append(topic)
    if len(history) > 50:
        history = history[-50:]
    seen["topics_history"] = history


def _get_subject_key(course_name: str) -> str | None:
    name_lower = course_name.lower()
    for key, keywords in SUBJECT_PRIORITY:
        for kw in keywords:
            if kw in name_lower:
                return key
    return None


def _get_todays_priority_subject(schedule: list) -> tuple[str | None, dict | None]:
    """Выбираем предмет по дню недели. Тему берём из расписания текущей/ближайшей недели."""
    today = datetime.datetime.now(tz=UFA_TZ)
    weekday = today.weekday()
    subject_key = WEEKDAY_SUBJECT.get(weekday, "matan")

    # Ищем тему в расписании текущей недели
    lesson = _find_lesson_for_subject(schedule, subject_key)

    # Если не нашли в сегодняшнем расписании — ищем в расписании недели
    if not lesson:
        lesson = _find_lesson_in_week(subject_key)

    # Если совсем ничего — создаём заглушку с предметом
    if not lesson:
        subject_names = {
            "matan": "Математический анализ",
            "networks": "Компьютерные сети",
            "cpp": "Программирование и алгоритмизация на C++",
            "discrete": "Дискретная математика",
            "history": "История России",
        }
        lesson = {"course_name": subject_names.get(subject_key, subject_key), "name": ""}

    return subject_key, lesson


def _find_lesson_for_subject(schedule: list, subject_key: str) -> dict | None:
    """Ищем занятие по ключу предмета в списке."""
    for lesson in schedule:
        name = lesson.get("course_name") or lesson.get("name") or ""
        key = _get_subject_key(name)
        if key == subject_key:
            return lesson
    return None


def _find_lesson_in_week(subject_key: str) -> dict | None:
    """Ищем тему в расписании текущей и следующей недели."""
    try:
        from parsers.modeus import _load_schedule_cache
        today = datetime.datetime.now(tz=UFA_TZ).date()

        # Проверяем текущую и следующую неделю
        for week_offset in range(3):
            week_start = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(weeks=week_offset)
            cache = _load_schedule_cache()
            entry = cache.get(week_start.isoformat())
            if not entry:
                continue
            week_data = entry.get("data", {})
            for day_lessons in week_data.values():
                for lesson in day_lessons:
                    name = lesson.get("course_name") or lesson.get("name") or ""
                    key = _get_subject_key(name)
                    if key == subject_key:
                        return lesson
    except Exception as e:
        print(f"study_theory: ошибка поиска темы в неделе: {e}")
    return None


async def get_todays_schedule() -> list:
    try:
        from parsers.modeus import _load_schedule_cache, fetch_schedule_today
        today = datetime.datetime.now(tz=UFA_TZ).date()
        week_start = today - datetime.timedelta(days=today.weekday())
        cache = _load_schedule_cache()
        entry = cache.get(week_start.isoformat())
        modeus_today = []
        netology_today = []
        if entry:
            modeus_today = entry.get("data", {}).get(today.isoformat(), [])
            netology_today = entry.get("netology", {}).get(today.isoformat(), []) if entry.get("netology") else []
        if not modeus_today:
            modeus_today = await fetch_schedule_today()
        return sorted(modeus_today + netology_today, key=lambda x: x.get("start_time", ""))
    except Exception as e:
        print(f"study_theory: ошибка получения расписания: {e}")
        return []


async def _send_long_message(bot, chat_id: int, text: str, parse_mode: str = None):
    import html as _html
    # Убираем двойные звёздочки, оставляем одинарные как есть
    text = text.replace("**", "*")
    # Отправляем как <pre> — моноширинный стиль как у английского
    def _wrap_pre(t):
        return f"<pre>{_html.escape(t)}</pre>"
    limit = 3500
    if len(text) <= limit:
        await bot.send_message(chat_id=chat_id, text=_wrap_pre(text), parse_mode="HTML")
        return
    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current.strip())
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        parts.append(current.strip())
    for part in parts:
        if part:
            await bot.send_message(chat_id=chat_id, text=_wrap_pre(part), parse_mode="HTML")

async def send_subject_theory(bot, chat_id: int):
    try:
        from grok import ask_grok
        schedule = await get_todays_schedule()
        if not schedule:
            return
        subject_key, lesson = _get_todays_priority_subject(schedule)
        if not subject_key or not lesson:
            return
        seen = _load_seen()
        if seen["subject_sent"].count(subject_key) >= 1:
            return
        seen["subject_sent"].append(subject_key)
        _save_seen(seen)
        course_name = lesson.get("course_name") or lesson.get("name") or ""
        topic = lesson.get("name") or lesson.get("description") or course_name
        no_repeat = _get_topics_history(seen)
        prompt = (
            f"Предмет: {course_name}\n"
            f"Тема занятия: {topic}\n"
            f"{no_repeat + chr(10) if no_repeat else ''}"
            f"Если тема уже в запрещённом списке — возьми смежную подтему.\n\n"
            f"Напиши теорию для студента 1 курса. Просто, понятно, как умный друг.\n\n"
            f"Структура:\n"
            f"📌 *ТЕМА — {{topic[:50]}}*\n\n"
            f"*Что это такое* — 2-3 предложения простыми словами + аналогия из жизни\n\n"
            f"*Основные понятия* — 2-3 ключевых термина с объяснением\n\n"
            f"*Как работает* — конкретный пример разбор\n\n"
            f"*Типичные ошибки* — 2 ошибки которые все делают\n\n"
            f"*Зачем нужно* — реальное применение\n\n"
            f"Правила форматирования:\n"
            f"- *жирный* для заголовков и терминов\n"
            f"- _курсив_ для примеров\n"
            f"- НЕ используй ** двойные звёздочки\n"
            f"- Между разделами пустая строка\n"
            f"- Объём: строго 250-300 слов. Умещайся в одно сообщение Telegram."
        )
        theory = await ask_grok(
            prompt,
            system="Ты преподаватель. Объясняй просто и понятно. Короткие предложения. Без воды.",
            smart=True
        )
        if not theory:
            return
        _add_to_history(seen, f"{course_name[:20]}:{topic[:30]}")
        _save_seen(seen)

        SUBJECT_EMOJI = {"discrete": "🔢", "matan": "📐", "networks": "🌐", "history": "📜"}
        emoji = SUBJECT_EMOJI.get(subject_key, "📚")
        header = f"{emoji} *ТЕОРИЯ ДНЯ — {course_name[:40].upper()}*\n\n"
        # Заменяем двойные звёздочки на одинарные
        import re as _re
        theory = _re.sub(r'\*\*(.+?)\*\*', r'*\1*', theory)
        await _send_long_message(bot, chat_id, header + theory)
        print(f"study_theory: теория по {subject_key} отправлена")
    except Exception as e:
        print(f"study_theory subject error: {e}")


async def send_english_theory(bot, chat_id: int):
    """Урок английского — слово из JSON + правило из JSON + объяснение через DeepSeek."""
    try:
        import json as _json
        import random as _random
        from grok import ask_grok

        WORDS_FILE = "data/english_words.json"
        WORDS_BACKUP = "data/english_words_backup.json"
        RULES_FILE = "data/english_rules.json"
        RULES_BACKUP = "data/english_rules_backup.json"

        # ── Берём слово ──────────────────────────────────────────────
        def _load(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return _json.load(f)
            except Exception:
                return []

        def _save(path, data):
            os.makedirs("data", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)

        def _pick_and_remove(work_path, backup_path):
            items = _load(work_path)
            if not items:
                # Список закончился — восстанавливаем из бэкапа и перемешиваем
                items = _load(backup_path)
                if not items:
                    return None
                _random.shuffle(items)
                _save(work_path, items)
                print(f"study_theory: список восстановлен из {backup_path}")
            item = items.pop(0)
            _save(work_path, items)
            return item

        word = _pick_and_remove(WORDS_FILE, WORDS_BACKUP)
        rule = _pick_and_remove(RULES_FILE, RULES_BACKUP)

        if not word or not rule:
            print("study_theory: нет слов или правил")
            return

        w = word["word"]
        transcription = word.get("transcription", "")
        translation = word.get("translation", "")
        rule_name = rule["rule"]
        rule_category = rule.get("category", "")
        rule_desc = rule.get("description", "")

        # ── Промпт для DeepSeek ──────────────────────────────────────
        prompt = f"""Ты преподаватель английского для русскоязычного студента уровня Pre-Intermediate.

Слово: {w} {transcription} — {translation}
Правило: {rule_name} ({rule_category}) — {rule_desc}

Составь подробный урок в формате Telegram Markdown. Строго следуй структуре:

🇬🇧 *АНГЛИЙСКИЙ*

━━━━━━━━━━━━━━━━━━━━
📌 *СЛОВО — {w.upper()}*
━━━━━━━━━━━━━━━━━━━━
🔤 Транскрипция: {transcription}
🇷🇺 Перевод: {translation}

🧠 *Как запомнить:*
[НА РУССКОМ — ассоциация или мнемоника — 2-3 предложения]

📝 *Примеры:*
- [English sentence with {w}]
  [Перевод на русский]
- [Another English sentence with {w}]
  [Перевод на русский]

🔄 Synonyms: [2-3 English synonyms, e.g.: progress, develop, move forward]
❌ Antonym: [1 English antonym, e.g.: retreat]

━━━━━━━━━━━━━━━━━━━━
📚 *ПРАВИЛО — {rule_name}*
━━━━━━━━━━━━━━━━━━━━
🤔 *Что это простыми словами:*
[НА РУССКОМ — объяснение как для чайника — 3-4 предложения без умных слов]

✅ *Формула:*
[Покажи формулу кратко, например: Subject + will + be + Verb-ing]

📝 *Примеры:*
- [Correct English sentence using the rule] ✅
  [Перевод на русский]
- [Another correct English sentence] ✅
  [Перевод на русский]

❌ *Common mistakes (Russian learners):*
- [Wrong English] → [Correct English]
- [Wrong English] → [Correct English]

💡 *Запомни:*
[НА РУССКОМ — лайфхак 1-2 предложения]

━━━━━━━━━━━━━━━━━━━━
🔗 *СЛОВО + ПРАВИЛО*
━━━━━━━━━━━━━━━━━━━━
- [English sentence using {w} + rule {rule_name}]
  [Перевод на русский]
- [Another English sentence]
  [Перевод на русский]

ВАЖНЫЕ ПРАВИЛА:
- Синонимы, антонимы, ошибки — ТОЛЬКО на английском
- Объяснения, мнемоники, лайфхаки — ТОЛЬКО на русском
- Примеры — английское предложение + русский перевод
- Используй *жирный* для заголовков
- НЕ используй ** двойные звёздочки
- НЕ добавляй ничего лишнего кроме структуры
- Объём: 400-500 слов"""

        theory = await ask_grok(prompt, system="Ты преподаватель английского. Строго следуй структуре. Объясняй как чайнику.", smart=False)
        if not theory:
            return

        await _send_long_message(bot, chat_id, theory)
        print(f"study_theory: английский отправлен — слово={w}, правило={rule_name}")

    except Exception as e:
        print(f"study_theory english error: {e}")
        import traceback
        traceback.print_exc()


