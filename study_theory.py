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


IT_TOPICS_FILE = "data/it_topics.json"
IT_TOPICS_INDEX_FILE = "data/it_topics_index.json"


def _load_it_topics() -> list:
    """Загружаем список IT тем из файла."""
    try:
        with open(IT_TOPICS_FILE, encoding="utf-8") as f:
            import json as _j
            return _j.load(f)
    except Exception as e:
        print(f"study_theory: не удалось загрузить it_topics.json: {e}")
        return []


def _get_next_it_topic() -> dict | None:
    """Возвращает следующую тему по порядку. После последней — начинает сначала."""
    topics = _load_it_topics()
    if not topics:
        return None
    try:
        with open(IT_TOPICS_INDEX_FILE) as f:
            import json as _j
            data = _j.load(f)
        idx = data.get("index", 0) % len(topics)
    except Exception:
        idx = 0
    topic = topics[idx]
    import os as _os
    _os.makedirs("data", exist_ok=True)
    import json as _j
    with open(IT_TOPICS_INDEX_FILE, "w") as f:
        _j.dump({"index": (idx + 1) % len(topics)}, f)
    return topic


IT_PRACTICE_INDEX_FILE = "data/it_practice_index.json"
IT_REVIEW_INDEX_FILE = "data/it_review_index.json"


def _get_it_topic_by_offset(offset: int) -> dict | None:
    """Возвращает тему по смещению от текущего индекса (для повторения)."""
    topics = _load_it_topics()
    if not topics:
        return None
    try:
        with open(IT_TOPICS_INDEX_FILE) as f:
            data = json.load(f)
        current = data.get("index", 0)
    except Exception:
        current = 0
    idx = (current - offset) % len(topics)
    return topics[idx]


async def send_it_theory(bot, chat_id: int):
    """11:00 — новая IT тема, объяснение через аналогию."""
    try:
        from grok import ask_grok
        topic_data = _get_next_it_topic()
        if not topic_data:
            print("study_theory: it_topics.json пуст или не найден")
            return

        topic = topic_data.get("topic", "")
        section = topic_data.get("section", "")
        topic_id = topic_data.get("id", "")

        prompt = f"""Ты наставник по IT. Ученик — полный новичок, никогда не программировал.
Раздел: {section}
Тема: {topic}

Напиши урок СТРОГО в таком формате — не отступай ни на символ:

💡 *{topic}*
_{section}_

🎯 *Зачем тебе это знать:*
[одно предложение — конкретная польза для аналитика или дата-сайентиста]

🧠 *Простыми словами:*
[объяснение через бытовую аналогию — кухня, игра, магазин, телефон. 3-4 коротких предложения. После каждой мысли — новая строка]

⚙️ *Как это выглядит:*
[если есть код — каждая строка на отдельной строке в `моноширинном`, после строки кода — краткий комментарий курсивом _что делает_. Если кода нет — конкретный жизненный пример по шагам]

🔑 *Главные термины:*
- *термин1* — объяснение одним предложением
- *термин2* — объяснение одним предложением
- *термин3* — объяснение одним предложением

⚡️ *Запомни одно:*
[одна фраза — суть темы, как другу в чате]

ПРАВИЛА ФОРМАТИРОВАНИЯ — строго:
- Между каждой секцией ОБЯЗАТЕЛЬНО пустая строка
- Заголовок секции на отдельной строке, текст с новой строки
- *жирный* только для заголовков и терминов
- _курсив_ только для комментариев к коду и аналогий
- Код — в одинарных кавычках `вот так`, каждая строка отдельно
- НЕ используй ** двойные звёздочки
- НЕ используй тройные кавычки
- НЕ используй линии ━━━ или ---
- Пиши живо и коротко, как старший друг
- Объём: 200-230 слов"""

        theory = await ask_grok(
            prompt,
            system="Ты наставник по IT для полных новичков. Всегда через аналогии и живые примеры. Обычный Telegram Markdown без тройных кавычек и линий.",
            smart=True
        )
        if not theory:
            return
        theory = re.sub(r'\*\*(.+?)\*\*', r'*\1*', theory)
        theory = theory.replace("```", "`")
        # Защита от незакрытых тегов — считаем количество * и _
        if theory.count("*") % 2 != 0:
            theory = theory + "*"
        if theory.count("_") % 2 != 0:
            theory = theory + "_"
        try:
            await bot.send_message(chat_id=chat_id, text=theory, parse_mode="Markdown")
        except Exception:
            # Если Markdown сломан — отправляем без форматирования
            clean = re.sub(r'[*_`]', '', theory)
            await bot.send_message(chat_id=chat_id, text=clean)
        print(f"study_theory: IT теория отправлена — #{topic_id} {topic}")
    except Exception as e:
        print(f"study_theory IT теория error: {e}")


async def send_it_practice(bot, chat_id: int):
    """13:00 — практика по теме которая была в 11:00."""
    try:
        from grok import ask_grok
        topic_data = _get_it_topic_by_offset(1)
        if not topic_data:
            return

        topic = topic_data.get("topic", "")
        section = topic_data.get("section", "")

        prompt = f"""Ученик утром изучил тему: "{topic}" (раздел: {section}).
Напиши практику СТРОГО в таком формате:

🛠 *Практика: {topic}*

🔁 *Вспомни главное:*
[одно предложение — суть темы из утреннего урока]

🔍 *Разбор примера:*
[конкретный рабочий пример. Если код — каждая строка отдельно в `моноширинном` с комментарием _что делает_. После кода — объяснение результата простыми словами]

✏️ *Попробуй сам:*
[простой вопрос или задача — можно решить в голове, без компьютера]

_Ответ:_ [правильный ответ с кратким объяснением]

⚠️ *Частая ошибка:*
[одна типичная ошибка новичков и как её избежать — одним абзацем]

ПРАВИЛА:
- Между каждой секцией пустая строка
- *жирный* для заголовков и терминов
- _курсив_ для комментариев и пояснений
- Код в `одинарных кавычках`, каждая строка отдельно
- НЕ используй ** и тройные кавычки
- НЕ используй линии ━━━
- Объём: 170-200 слов"""

        practice = await ask_grok(
            prompt,
            system="Ты наставник по IT. Пиши практические разборы живо и понятно. Telegram Markdown без тройных кавычек.",
            smart=True
        )
        if not practice:
            return
        practice = re.sub(r'\*\*(.+?)\*\*', r'*\1*', practice)
        practice = practice.replace("```", "`")
        await bot.send_message(chat_id=chat_id, text=practice, parse_mode="Markdown")
        print(f"study_theory: IT практика отправлена — {topic}")
    except Exception as e:
        print(f"study_theory IT практика error: {e}")


async def send_it_review(bot, chat_id: int):
    """19:00 — повторение темы которая была 2 дня назад."""
    try:
        from grok import ask_grok
        import json as _json
        # Повторение работает только после 5 изученных тем
        try:
            with open(IT_TOPICS_INDEX_FILE) as f:
                _idx = _json.load(f).get("index", 0)
        except Exception:
            _idx = 0
        if _idx < 5:
            print(f"study_theory: повторение пропущено — изучено только {_idx} тем, нужно минимум 5")
            return
        topic_data = _get_it_topic_by_offset(5)
        if not topic_data:
            return

        topic = topic_data.get("topic", "")
        section = topic_data.get("section", "")

        prompt = f"""Ученик несколько дней назад изучал тему: "{topic}" (раздел: {section}).
Напиши флэшкард-повторение СТРОГО в таком формате:

🔁 *Повторение: {topic}*

💬 *Помнишь эту тему?*
[одно предложение — главная суть как напоминалка]

❓ *Три вопроса — ответь сам:*
1. [вопрос на понимание — "почему" или "как", не "что называется"]
2. [вопрос на понимание]
3. [вопрос на понимание]

✅ *Ответы:*
1. [краткий ответ]
2. [краткий ответ]
3. [краткий ответ]

🔗 *Связь с Data Science:*
[одно предложение — где эта тема пригодится дальше]

ПРАВИЛА:
- Между каждой секцией пустая строка
- *жирный* для заголовков
- _курсив_ для пояснений
- НЕ используй ** и тройные кавычки
- НЕ используй линии ━━━
- Объём: 140-170 слов"""

        review = await ask_grok(
            prompt,
            system="Ты наставник по IT. Пиши флэшкарды для повторения живо и коротко. Telegram Markdown без тройных кавычек.",
            smart=True
        )
        if not review:
            return
        review = re.sub(r'\*\*(.+?)\*\*', r'*\1*', review)
        review = review.replace("```", "`")
        await bot.send_message(chat_id=chat_id, text=review, parse_mode="Markdown")
        print(f"study_theory: IT повторение отправлено — {topic}")
    except Exception as e:
        print(f"study_theory IT повторение error: {e}")



# ─── Английский: файлы данных ────────────────────────────────────────────────
CHUNKS_FILE         = "data/english_chunks.json"
CHUNKS_BACKUP       = "data/english_chunks_backup.json"
PRONUN_FILE         = "data/english_pronunciation.json"
PRONUN_BACKUP       = "data/english_pronunciation_backup.json"
DIALOGS_FILE        = "data/english_dialogs.json"
DIALOGS_BACKUP      = "data/english_dialogs_backup.json"


def _pick_and_remove_item(work_path: str, backup_path: str) -> dict | None:
    """Берёт первый элемент из work-файла, удаляет его.
    Если файл пуст — восстанавливает из backup и перемешивает."""
    import random as _r
    def _load(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    def _save(p, data):
        os.makedirs("data", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    items = _load(work_path)
    if not items:
        items = _load(backup_path)
        if not items:
            return None
        _r.shuffle(items)
        _save(work_path, items)
        print(f"study_theory: список восстановлен из {backup_path}")
    item = items.pop(0)
    _save(work_path, items)
    return item


async def send_english_chunk(bot, chat_id: int):
    """10:00 — chunk дня: готовая разговорная фраза."""
    try:
        from grok import ask_grok
        item = _pick_and_remove_item(CHUNKS_FILE, CHUNKS_BACKUP)
        if not item:
            print("study_theory: english_chunks.json пуст")
            return

        chunk   = item.get("chunk", "")
        context = item.get("context", "")
        example = item.get("example", "")

        prompt = f"""Ты учишь разговорному английскому. Ученик хочет говорить как носитель.
Chunk дня: "{chunk}"
Контекст использования: {context}
Пример: {example}

Напиши урок СТРОГО в этом формате (Telegram Markdown, одинарные * и _):

💬 *Chunk дня: "{chunk}"*

🎯 *Когда говорить:*
[одно-два предложения — конкретная жизненная ситуация когда это говорят]

🗣 *Как произносить:*
[покажи слитное произношение курсивом, например: _"I-wz-justa-bout-tu"_]
[одно предложение почему именно так, а не по словам]

📍 *В жизни это звучит так:*
[два коротких диалога 2-3 реплики каждый. Каждая реплика с новой строки]

✏️ *Теперь ты:*
Придумай 2 своих примера из своей жизни и произнеси вслух.

💡 *Похожие фразы:*
• [похожий chunk 1]
• [похожий chunk 2]

ПРАВИЛА:
- Между секциями пустая строка
- *жирный* только заголовки
- _курсив_ для произношения и примеров
- НЕ используй ** и тройные кавычки
- НЕ используй линии ━━━
- Живо, как старший друг — не как учебник
- Объём: 180-220 слов"""

        result = await ask_grok(
            prompt,
            system="Ты учишь разговорному английскому. Фокус на живой речи носителей. Telegram Markdown без тройных кавычек и линий.",
            smart=True
        )
        if not result:
            return
        result = re.sub(r'\*\*(.+?)\*\*', r'*\1*', result)
        result = result.replace("```", "`")
        if result.count("*") % 2 != 0:
            result += "*"
        if result.count("_") % 2 != 0:
            result += "_"
        try:
            await bot.send_message(chat_id=chat_id, text=result, parse_mode="Markdown")
        except Exception:
            clean = re.sub(r'[*_`]', '', result)
            await bot.send_message(chat_id=chat_id, text=clean)
        print(f"study_theory: chunk отправлен — {chunk}")
    except Exception as e:
        print(f"study_theory english chunk error: {e}")


async def send_english_pronunciation(bot, chat_id: int):
    """15:30 — pronunciation: паттерн речи носителей."""
    try:
        from grok import ask_grok
        item = _pick_and_remove_item(PRONUN_FILE, PRONUN_BACKUP)
        if not item:
            print("study_theory: english_pronunciation.json пуст")
            return

        pattern        = item.get("pattern", "")
        formal         = item.get("formal", "")
        casual         = item.get("casual", "")
        example_formal = item.get("example_formal", "")
        example_casual = item.get("example_casual", "")
        note           = item.get("note", "")

        prompt = f"""Ты учишь разговорному произношению английского.
Паттерн: {pattern}
Формально: {formal} → Разговорно: {casual}
Пример формально: {example_formal}
Пример разговорно: {example_casual}
Заметка: {note}

Напиши урок СТРОГО в этом формате (Telegram Markdown):

🎙 *Звук дня: {pattern}*

🔊 *Как говорят носители:*
[объясни разницу между формальным и разговорным — 2-3 предложения]
_Формально:_ {example_formal}
_Разговорно:_ {example_casual}

🎯 *Ещё примеры:*
[3 коротких примера с разговорным произношением, каждый с новой строки]

⚠️ *Когда НЕ использовать:*
[одно предложение — формальные ситуации где это неуместно]

🔁 *Повтори вслух 5 раз:*
[дай короткую фразу для тренировки этого паттерна]

ПРАВИЛА:
- Между секциями пустая строка
- *жирный* только заголовки
- _курсив_ для примеров произношения
- НЕ используй ** и тройные кавычки
- НЕ используй линии ━━━
- Объём: 150-180 слов"""

        result = await ask_grok(
            prompt,
            system="Ты учишь произношению английского как у носителей. Telegram Markdown без тройных кавычек и линий.",
            smart=False
        )
        if not result:
            return
        result = re.sub(r'\*\*(.+?)\*\*', r'*\1*', result)
        result = result.replace("```", "`")
        if result.count("*") % 2 != 0:
            result += "*"
        if result.count("_") % 2 != 0:
            result += "_"
        try:
            await bot.send_message(chat_id=chat_id, text=result, parse_mode="Markdown")
        except Exception:
            clean = re.sub(r'[*_`]', '', result)
            await bot.send_message(chat_id=chat_id, text=clean)
        print(f"study_theory: pronunciation отправлен — {pattern}")
    except Exception as e:
        print(f"study_theory english pronunciation error: {e}")


async def send_english_dialog(bot, chat_id: int):
    """22:00 — диалог дня: живая ситуация на английском."""
    try:
        from grok import ask_grok
        item = _pick_and_remove_item(DIALOGS_FILE, DIALOGS_BACKUP)
        if not item:
            print("study_theory: english_dialogs.json пуст")
            return

        situation = item.get("situation", "")
        dialog    = item.get("dialog", [{}])[0]
        vocab     = item.get("vocab", [])

        lines = []
        for k, v in dialog.items():
            speaker = "A:" if k.startswith("A") else "B:"
            lines.append(f"{speaker} _{v}_")
        dialog_text = "\n".join(lines)

        vocab_lines = "\n".join([f"• *{v['word']}* — {v['meaning']}" for v in vocab])

        prompt = f"""Ты учишь разговорному английскому через живые диалоги.
Ситуация: {situation}
Диалог:
{dialog_text}
Словарь:
{vocab_lines}

Напиши урок СТРОГО в этом формате (Telegram Markdown):

🎬 *Ситуация: {situation}*

[диалог — каждая реплика с новой строки, имя говорящего жирным *A:* или *B:*, текст курсивом]

📖 *Разбор:*
{vocab_lines}

🗣 *Прочитай вслух 2 раза.*
Представь что это ты. Почувствуй ритм речи.

💭 *Ключевая мысль:*
[одно предложение — что главное в этом диалоге с точки зрения живого английского]

ПРАВИЛА:
- Между секциями пустая строка
- *жирный* для имён говорящих и терминов
- _курсив_ для реплик диалога
- НЕ используй ** и тройные кавычки
- НЕ используй линии ━━━
- Объём: 120-150 слов"""

        result = await ask_grok(
            prompt,
            system="Ты учишь разговорному английскому через диалоги. Telegram Markdown без тройных кавычек и линий.",
            smart=False
        )
        if not result:
            return
        result = re.sub(r'\*\*(.+?)\*\*', r'*\1*', result)
        result = result.replace("```", "`")
        if result.count("*") % 2 != 0:
            result += "*"
        if result.count("_") % 2 != 0:
            result += "_"
        try:
            await bot.send_message(chat_id=chat_id, text=result, parse_mode="Markdown")
        except Exception:
            clean = re.sub(r'[*_`]', '', result)
            await bot.send_message(chat_id=chat_id, text=clean)
        print(f"study_theory: диалог отправлен — {situation}")
    except Exception as e:
        print(f"study_theory english dialog error: {e}")
