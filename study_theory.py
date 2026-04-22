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
]

THEORY_SUBJECTS = ["discrete", "matan", "networks", "history"]

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
    found = {}
    for lesson in schedule:
        name = lesson.get("course_name") or lesson.get("name") or ""
        key = _get_subject_key(name)
        if key and key in THEORY_SUBJECTS and key not in found:
            found[key] = lesson
    for pkey, _ in SUBJECT_PRIORITY:
        if pkey in found:
            return pkey, found[pkey]
    return None, None


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


async def _send_long_message(bot, chat_id: int, text: str, parse_mode: str = "Markdown"):
    limit = 4000
    if len(text) <= limit:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
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
            await bot.send_message(chat_id=chat_id, text=part, parse_mode=parse_mode)


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
            f"Если тема занятия уже есть в запрещённом списке — возьми смежную подтему, которой там нет.\n"
            f"\nНапиши подробную теорию для студента 1 курса который ничего не знает. "
            f"Пиши как живой человек — просто, понятно, без канцелярита. "
            f"Короткие предложения. Каждая мысль — отдельный абзац.\n\n"
            f"ВАЖНО — форматирование для Telegram Markdown:\n"
            f"— Заголовки разделов пиши жирным: *Раздел*\n"
            f"— Важные термины выделяй жирным: *термин*\n"
            f"— Примеры выделяй курсивом: _пример_\n"
            f"— Между разделами пустая строка\n"
            f"— НЕ используй #, ##, -, * в начале строки как маркеры списков\n\n"
            f"Структура (каждый раздел подробно, минимум 3-5 предложений):\n"
            f"*1. Что это такое* — объясни простыми словами, дай аналогию из жизни\n"
            f"*2. Основные понятия* — каждый термин объясни отдельно с примером\n"
            f"*3. Как это работает* — пошаговый разбор на конкретном примере\n"
            f"*4. Типичные ошибки* — что чаще всего путают и как не ошибиться\n"
            f"*5. Зачем это нужно* — реальное применение в жизни и профессии\n\n"
            f"Объём: не менее 500 слов. Не жалей деталей — студент должен понять с нуля."
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

        # Случайный факт с uselessfacts
        fact_block = ""
        try:
            import httpx as _httpx
            from grok import ask_grok as _ask_grok
            async with _httpx.AsyncClient(timeout=8) as _client:
                _r = await _client.get(
                    "https://uselessfacts.jsph.pl/api/v2/facts/random",
                    params={"language": "en"}
                )
                if _r.status_code == 200:
                    _fact_en = _r.json().get("text", "")
                    if _fact_en:
                        _fact_ru = await _ask_grok(
                            f"Переведи факт на русский. Только перевод:\n{_fact_en}"
                        )
                        if _fact_ru:
                            fact_block = f"\n{'─' * 20}\n💡 ФАКТ ДНЯ\n────────────────────\n{_fact_ru}"
        except Exception as e:
            print(f"Fact of day error: {e}")

        SUBJECT_EMOJI = {"discrete": "🔢", "matan": "📐", "networks": "🌐", "history": "📜"}
        emoji = SUBJECT_EMOJI.get(subject_key, "📚")
        header = (
            f"{emoji} *ТЕОРИЯ ДНЯ — {course_name[:40].upper()}*\n"
            f"────────────────────────────────────────\n"
            f"Тема: {topic[:60]}\n\n"
        )
        await _send_long_message(bot, chat_id, header + theory + fact_block)
        print(f"study_theory: теория по {subject_key} отправлена")
    except Exception as e:
        print(f"study_theory subject error: {e}")


async def send_english_theory(bot, chat_id: int):
    try:
        from grok import ask_grok
        seen = _load_seen()
        count = seen.get("english_count", 0)
        if count >= 5:
            return
        seen["english_count"] = count + 1
        _save_seen(seen)
        english_topic = None
        try:
            schedule = await get_todays_schedule()
            for lesson in schedule:
                name = lesson.get("course_name") or lesson.get("name") or ""
                if "английск" in name.lower() or "english" in name.lower() or "иностранн" in name.lower():
                    t = lesson.get("name") or ""
                    if t and t != name:
                        english_topic = t
                        break
        except Exception:
            pass
        ENGLISH_TYPES = [
            ("Новые слова",           "words"),
            ("Грамматика",            "grammar"),
            ("Фразовые глаголы",      "phrasal"),
            ("Лайфхаки",              "tips"),
            ("Идиомы и выражения",    "idioms"),
            ("Новые слова по теме",   "words"),
            ("Грамматика vs ошибки",  "grammar_errors"),
        ]
        day = datetime.datetime.now(tz=UFA_TZ).weekday()
        task_type, task_kind = ENGLISH_TYPES[day]

        if task_kind == "words":
            words_no_repeat = _get_words_history(seen)
            topic_hint = f'по теме "{english_topic}"' if english_topic else "на бытовую или разговорную тему"
            prompt = (
                f"Дай ровно 10 английских слов {topic_hint} уровня Pre-Intermediate.\n"
                f"{words_no_repeat + chr(10) if words_no_repeat else ''}"
                f"\nСтрого следуй формату для каждого слова (без отступлений):\n\n"
                f"*слово* [транскрипция МФА] — перевод на русский\n"
                f"Английское предложение с этим словом.\n"
                f"Перевод этого предложения на русский.\n\n"
                f"Между словами — пустая строка.\n"
                f"Слова должны быть полезными в разговорной речи.\n"
                f"НЕ используй базовые слова: apple, cat, house, book, table, school, car.\n"
                f"НЕ добавляй никаких вступлений, заголовков, пояснений — только список слов."
            )
        elif task_kind == "grammar":
            rule, new_idx = _get_next_grammar_rule(seen)
            seen["grammar_index"] = new_idx
            no_repeat_topics = _get_topics_history(seen)
            prompt = (
                f"Объясни грамматическое правило: {rule}\n"
                f"{no_repeat_topics + chr(10) if no_repeat_topics else ''}"
                f"\nФорматирование строго для Telegram Markdown:\n"
                f"— Название правила жирным: *Название*\n"
                f"— Формула жирным: *Subject + V2*\n"
                f"— Примеры курсивом: _I went to school_\n"
                f"— Между блоками пустая строка\n"
                f"— НЕ используй #, -, * как маркеры списков\n\n"
                f"Структура:\n"
                f"*Правило* — одна строка с названием\n"
                f"*Когда используется* — 2-3 предложения простыми словами\n"
                f"*Формула* — структура предложения\n"
                f"*Примеры* — ровно 5 примеров, каждый с переводом на русский\n"
                f"*Типичные ошибки русскоязычных* — 2-3 конкретные ошибки с исправлением\n"
                f"*Сравнение* — чем отличается от похожих правил (1-2 предложения)\n\n"
                f"Объём: 300-380 слов. Умещайся в одно сообщение Telegram (до 4000 символов)."
            )
            _add_to_history(seen, f"grammar:{rule[:40]}")
        elif task_kind == "phrasal":
            phrasal_no_repeat = _get_phrasal_history(seen)
            prompt = (
                f"Дай 7 популярных фразовых глаголов для разговорной речи уровня Pre-Intermediate.\n"
                f"{phrasal_no_repeat + chr(10) if phrasal_no_repeat else ''}"
                f"\nСтрогий формат для каждого (без вступлений):\n\n"
                f"*глагол + предлог* — перевод\n"
                f"Английское предложение-пример.\n"
                f"Перевод предложения на русский.\n\n"
                f"Между глаголами — пустая строка.\n"
                f"НЕ добавляй никаких вступлений и заголовков — только список."
            )
        elif task_kind == "idioms":
            idioms_no_repeat = _get_idioms_history(seen)
            prompt = (
                f"Дай 7 популярных английских идиом которые реально используют в речи и кино.\n"
                f"{idioms_no_repeat + chr(10) if idioms_no_repeat else ''}"
                f"\nСтрогий формат для каждой (без вступлений):\n\n"
                f"*идиома* — буквальный перевод → реальное значение\n"
                f"Английское предложение-пример.\n"
                f"Перевод предложения на русский.\n\n"
                f"Между идиомами — пустая строка.\n"
                f"НЕ добавляй никаких вступлений и заголовков — только список."
            )
        elif task_kind == "grammar_errors":
            error_topic, new_idx = _get_next_grammar_error_topic(seen)
            seen["grammar_errors_index"] = new_idx
            no_repeat_topics = _get_topics_history(seen)
            prompt = (
                f"Тема: {error_topic}\n"
                f"{no_repeat_topics + chr(10) if no_repeat_topics else ''}"
                f"\nПокажи 4 частые ошибки русскоязычных студентов по этой теме.\n"
                f"\nФормат для каждой ошибки (строго, без отступлений):\n\n"
                f"*Название ошибки*\n"
                f"❌ Неправильно: пример предложения\n"
                f"✅ Правильно: исправленный пример\n"
                f"Объяснение: почему так, 2-3 предложения простым языком.\n"
                f"Перевод правильного примера на русский.\n\n"
                f"Между ошибками — пустая строка.\n"
                f"НЕ добавляй вступлений, заголовков раздела, итоговых фраз — только 4 ошибки.\n"
                f"Уложись в 3500 символов."
            )
            _add_to_history(seen, f"errors:{error_topic[:40]}")
        else:
            no_repeat_topics = _get_topics_history(seen)
            prompt = (
                f"Дай 5 конкретных лайфхаков как быстрее запоминать английские слова и правила.\n"
                f"{no_repeat_topics + chr(10) if no_repeat_topics else ''}"
                f"\nДля каждого лайфхака:\n"
                f"*Название лайфхака*\n"
                f"Описание — 2-3 предложения.\n"
                f"Пример упражнения которое можно сделать прямо сейчас.\n\n"
                f"Между лайфхаками — пустая строка."
            )

        theory = await ask_grok(
            prompt,
            system="Ты преподаватель английского. Строго следуй формату. Пиши живым языком. Без воды, вступлений и лишних пояснений.",
            smart=True
        )
        if not theory:
            return

        # Сохраняем слова/фразовые/идиомы в историю
        if task_kind == "words":
            found_words = re.findall(r'\*([a-zA-Z][a-zA-Z ]{1,20}?)\*', theory)
            if found_words:
                _add_words_to_history(seen, found_words)
        elif task_kind == "phrasal":
            found = re.findall(r'\*([a-zA-Z]+ (?:up|off|on|out|in|down|back|away|over|through|around|about)[a-zA-Z ]*)\*', theory)
            if found:
                _add_phrasal_to_history(seen, found)
        elif task_kind == "idioms":
            found = re.findall(r'\*([^*]{4,40})\*', theory)
            if found:
                _add_idioms_to_history(seen, found)

        _save_seen(seen)

        # Слово дня — добавляем к каждому уроку своё содержимое
        word_block = ""
        try:
            wod = await _fetch_word_of_day()
            if wod and wod.get("word"):
                w = wod["word"]
                tr = wod.get("transcription", "")
                tl = wod.get("translation", "")
                ex = wod.get("example", "")
                ex_ru = wod.get("example_ru", "")
                if count == 1:
                    # Урок 1 — новое слово
                    word_block = (
                        f"\n{'─' * 20}\n"
                        f"📝 СЛОВО ДНЯ\n"
                        f"────────────────────\n"
                        f"{w.upper()}  {tr}\n"
                        f"Перевод: {tl}\n"
                        f"Пример: {ex}\n"
                        f"({ex_ru})"
                    )
                elif count == 2:
                    # Урок 2 — пример с этим словом
                    word_block = (
                        f"\n{'─' * 20}\n"
                        f"📝 СЛОВО ДНЯ — ПРАКТИКА\n"
                        f"────────────────────\n"
                        f"Сегодняшнее слово: {w.upper()} — {tl}\n"
                        f"Пример: {ex}\n"
                        f"({ex_ru})"
                    )
                elif count >= 3:
                    # Урок 3 — мини-тест
                    word_block = (
                        f"\n{'─' * 20}\n"
                        f"📝 СЛОВО ДНЯ — ПОВТОРЕНИЕ\n"
                        f"────────────────────\n"
                        f"Помнишь слово дня? Подсказка: {tl}\n"
                        f"Ответ: {w.upper()}  {tr}"
                    )
        except Exception as e:
            print(f"Word of day block error: {e}")

        header = (
            f"🇬🇧 *АНГЛИЙСКИЙ — {task_type.upper()}*\n\n"
        )
        await _send_long_message(bot, chat_id, header + theory + word_block)
        print(f"study_theory: английский отправлен (#{count+1})")
    except Exception as e:
        print(f"study_theory english error: {e}")
