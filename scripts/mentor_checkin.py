#!/usr/bin/env python3
"""
Наставник-агент — полностью отдельный от бота процесс.
Запускается launchd 6 раз в день с чередующимися промежутками 2ч/3ч
(08:30, 10:31, 13:31, 15:32, 18:32, 20:33 — между 1 и 3 сообщением ровно
5ч01м, тихие часы 22:00-08:00 не трогаем), каждый раз с разным фокусом,
чтобы не повторяться. Читает data/*.json через Claude Code (headless,
только чтение, без Bash) и шлёт живое, человеческое сообщение в Telegram
напрямую через Bot API. Бот (main.py/scheduler.py) не трогает и не
перезапускает.
"""
import asyncio
import datetime
import os
import re
import shutil
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
# launchd запускает нас с cwd=/ — переходим в проект, чтобы относительные
# "data/..." пути (storage.py, config.DATA_DIR) резолвились туда же, куда
# смотрит сам бот, а не пытались создать /data.
os.chdir(PROJECT_DIR)

import requests
from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID, USER_NAME, UFA_TZ
from data_lock import atomic_write_json, file_lock

LOG_FILE = os.path.join(PROJECT_DIR, "data", "mentor_checkin.log")
STUDY_ANALYSIS_FILE = os.path.join(PROJECT_DIR, "data", "study_analysis_latest.txt")
WEATHER_FILE = os.path.join(PROJECT_DIR, "data", "weather_latest.txt")
VK_SCHEDULE_UPDATES_FILE = os.path.join(PROJECT_DIR, "data", "vk_schedule_updates.json")
MISSED_FILE = os.path.join(PROJECT_DIR, "data", "mentor_missed.json")
KNOWLEDGE_FILE = os.path.join(PROJECT_DIR, "data", "study_knowledge.json")

# Слоты, после которых обновляется список «Наставник» в приложении
# «Напоминания» macOS (детерминированно, без Claude — см. sync_to_reminders).
REMINDERS_SYNC_SLOTS = {"morning", "winddown", "weekly"}

SLOT_LABELS = {
    "morning": "утренний план (погода, пары и темы на сегодня, срочные дедлайны)",
    "midmorning": "лёгкий дневной пинг-проверка",
    "schedule_focus": "дневное: расписание и что пришло важного с утра",
    "motivation": "мотивационное сообщение",
    "evening": "итог дня (сделанное, стрик, баллы/посещаемость)",
    "winddown": "вечернее: итог дня и план на завтра",
    "weekly": "итог недели",
}

# Статичная часть — системный промпт разового вызова (одинаковый между
# вызовами → кэшируется). Данные — в сообщении (scripts/mentor_context.py).
PERSONA = (
    "Ты — {user_name}у не бот-уведомлятор, а его личный наставник: человек, которому "
    "не всё равно, как проходит учёба. Обращайся на \"ты\", по-русски, живым разговорным "
    "языком — как умный друг, который реально следит за делами, а не как отчёт. "
    "Без \"Приветствую\", без пафоса, без выдуманных проблем.\n\n"
    "ПОЛЬЗА ВМЕСТО ДЕЖУРНЫХ ФРАЗ (главное правило). Он жалуется, что ты повторяешь одно и то же: "
    "«дедлайнов нет», «срочного нет», «можно отдохнуть», «молодец, что закрыл». Так больше нельзя:\n"
    "- В каждом сообщении — минимум одно КОНКРЕТНОЕ действие из данных: что именно сделать, "
    "по какому предмету, почему сейчас (связка «вебинар → тест к нему», домашка из чата, "
    "выложенная запись пропущенного вебинара, риск по предмету с цифрами, просрочка, которую "
    "дешевле всего закрыть, обещание).\n"
    "- Отсутствие ближайших дедлайнов — НЕ новость и не повод отдыхать, если в «РИСКАХ» "
    "предметы с прогнозом ниже зачёта или с пропусками. Говори о них прямо, с цифрами, без морали.\n"
    "- Не повторяй мысль, которая уже была в «ТВОИХ ПОСЛЕДНИХ СООБЩЕНИЯХ» (за 3 дня), даже "
    "другими словами; выбирай другой угол или другой предмет.\n"
    "- Учитывай его 👍/👎 к прошлым сообщениям, если они есть.\n"
    "- Если блок «ИСТОЧНИКИ, КОТОРЫЕ НЕ РАБОТАЮТ» не пуст — коротко скажи, что оттуда сейчас "
    "ничего не видно (не «там ничего нет»).\n\n"
    "ДАННЫЕ. Всё, что тебе нужно, уже собрано кодом в сообщении пользователя блоками "
    "«=== … ===». Файлы читать не нужно и нельзя. Блоки с пометкой «код, точное» — "
    "факты: не меняй в них даты, не добавляй и не теряй пункты. Если чего-то в блоках "
    "нет — значит этого нет, не выдумывай.\n\n"
    "ПРАВИЛА ПРОЕКТА:\n"
    "- Закрытых задач в блоках нет — не упоминай их как дела.\n"
    "- [рекомендованный срок] — мягкий срок, без давления; лабораторные, зачётные и "
    "итоговые — реально оцениваемые, их выделяй.\n"
    "- Личные напоминания — не учёба и не дедлайны; дата события и время срабатывания "
    "напоминания — разные вещи, не путай их.\n"
    "- Пары Modeus и вебинары Нетологии в одно время — это РАЗНЫЕ занятия, не дубли.\n"
    "- LXP — асинхронно, без фиксированного времени.\n"
    "- Оргфакты с вебинаров используй, только если они помогают решить что-то сейчас; "
    "не выдавай общий совет преподавателя за дедлайн.\n"
    "- Память о студенте: «подтверждено им самим» — факты; «черновые наблюдения» — "
    "автоматические сводки прошлых сообщений, они могут быть ошибочны: не опирайся на них "
    "как на факт и не повторяй их ему как истину. Текущие данные важнее любой памяти."
)

# Расписание слотов: (час, минута, ключ слота). Порядок важен для подбора ближайшего.
SLOT_SCHEDULE = [
    (8, 30, "morning"),
    (10, 31, "midmorning"),
    (13, 31, "schedule_focus"),
    (15, 32, "motivation"),
    (18, 32, "evening"),
    (20, 33, "winddown"),
]

# Задание каждого слота. Нужные данные кладёт scripts/mentor_context.py
# (SLOT_BLOCKS) — здесь только что сказать, без "прочитай файл X".
SLOTS = {
    "morning": {
        "focus": (
            "Утро. Начни с тёплого \"доброе утро\" без штампов. Перескажи погоду своими "
            "словами. Расскажи, какие сегодня пары и по каким темам (простым языком; "
            "асинхронное LXP — без времени). Если есть дедлайн сегодня или завтра — скажи прямо; "
            "если ничего срочного — про дедлайны промолчи. Если в почте/мессенджере за сутки "
            "есть что-то заметное от преподавателей/деканата — одной фразой \"кстати, тебе "
            "писали про…\". Если в «РИСКАХ ПО ПРЕДМЕТАМ» есть предмет, о котором ты не писал "
            "последние 3 дня, — одной-двумя фразами с цифрами и одним конкретным шагом, как начать "
            "выправлять. Закончи короткой мыслью-настроем на день."
        ),
        "need_study_analysis": False, "need_weather": True, "need_vk_schedule": False,
    },
    "midmorning": {
        "focus": (
            "Середина дня. Короткий лёгкий пинг (2–3 предложения): как идёт день, не забыл ли "
            "главное на сегодня. Если всё штатно — просто коротко подбодри."
        ),
        "need_study_analysis": False, "need_weather": False, "need_vk_schedule": False,
    },
    "schedule_focus": {
        "focus": (
            "Дневное сообщение (14:00). Две части. 1) Расписание: если в блоке ВК есть "
            "объявления — перескажи своими словами, что изменилось (не копируй текст); затем "
            "коротко — какие темы на оставшихся сегодня и завтрашних парах. Не выдумывай изменений. "
            "2) Что пришло с утра: новые оценки и задания, письма и сообщения — только важное "
            "(преподаватели, деканат, сроки). «Ждёт его ответа» — ТОЛЬКО то, что в блоке «ЖДУТ ЕГО "
            "ОТВЕТА»; вопросы в общих чатах группы задают другие студенты, это не обращения к нему. "
            "Если важного не пришло — эту часть опусти."
        ),
        "need_study_analysis": False, "need_weather": False, "need_vk_schedule": True,
    },
    "motivation": {
        "focus": (
            "Короткое мотивационное сообщение в духе программистской культуры — можно с лёгким "
            "юмором (баги, коммиты, дебаг жизни), можно короткий реальный пример (без выдуманных "
            "фактов и цифр). Свяжи с его реальным положением по блокам активности и задач. "
            "2–4 предложения, без клише."
        ),
        "need_study_analysis": False, "need_weather": False, "need_vk_schedule": False,
    },
    "evening": {
        "focus": (
            "Вечер — итог дня: что реально закрыто, как активность в динамике, и если по баллам "
            "или посещаемости что-то просело — прямо, без драмы (если данные устарели — скажи "
            "об этом, не выдавай за свежие). Не осталось ли важного письма без реакции. "
            "Закончи коротким тёплым напутствием."
        ),
        "need_study_analysis": True, "need_weather": False, "need_vk_schedule": False,
    },
    "winddown": {
        "focus": (
            "Вечернее сообщение (20:33) — итог дня и план на завтра. Сначала коротко итог: что "
            "сегодня закрыто (или честно, что ничего) и как это на фоне последних дней по "
            "блоку активности — без морали. Потом что ждёт завтра (пары) и что горит в "
            "ближайшие дни, чтобы лёг спать, зная план. Если срочного нет — возьми из «РИСКОВ» "
            "предмет, о котором давно не говорил, или домашку из чатов, и предложи один шаг на завтра. "
            "Заверши коротким тёплым пожеланием или лёгкой мотивацией на завтра."
        ),
        "need_study_analysis": False, "need_weather": False, "need_vk_schedule": False,
    },
    "weekly": {
        "focus": (
            "Воскресенье — глубокий итог недели. Опирайся на дневник недели, обещания (что "
            "выполнено, что пропущено), оценки, активность и баллы. Дай честную картину: что "
            "получилось, где просадка, какие обещания сорвались и почему это важно, и КОНКРЕТНЫЙ "
            "план на следующую неделю (2–4 пункта с днями, опираясь на дедлайны). 6–10 предложений. "
            "Если что-то из прошлых твоих сообщений разошлось с фактами — прямо отметь.\n\n"
            "После основного текста — отдельной строкой ровно ===PROFILE=== и затем 0–3 строки: "
            "устойчивые наблюдения о нём, которые стоит запомнить на будущее (как он работает, что "
            "помогает, что мешает), каждое — одна фраза, только если подкреплено фактами недели. "
            "Он подтвердит или отклонит их кнопкой — не выдумывай ради количества."
        ),
        "need_study_analysis": True, "need_weather": False, "need_vk_schedule": False,
    },
}

def _todays_relevant_courses() -> list:
    """Курсы, которые реально имеют отношение к сегодня — активные (не done)
    задачи LMS/Netology + предметы сегодняшнего расписания. Раньше "проверь
    study_knowledge.json, если релевантно" было необязательной инструкцией —
    модель сама решала, читать файл или нет, без гарантии. Здесь то же самое,
    что scheduler.get_pending_tasks() уже даёт боту для разбора дедлайнов,
    просто собираем список курсов кодом, а не полагаемся на выбор модели."""
    import json as _json
    courses = set()

    try:
        with open(os.path.join(PROJECT_DIR, "data", "tasks.json"), encoding="utf-8") as f:
            tasks = _json.load(f)
        for t in tasks:
            if t.get("done") or t.get("source") not in ("lms", "netology"):
                continue
            if t.get("course_name"):
                courses.add(t["course_name"])
    except Exception:
        pass

    today_str = datetime.datetime.now(tz=UFA_TZ).strftime("%Y-%m-%d")
    try:
        digest_path = os.path.join(PROJECT_DIR, "data", "schedule_vk_digest.json")
        with open(digest_path, encoding="utf-8") as f:
            digest = _json.load(f)
        if digest.get("date") == today_str:
            for item in digest.get("items", []):
                if item.get("subject"):
                    courses.add(item["subject"])
    except Exception:
        pass

    try:
        cache_path = os.path.join(PROJECT_DIR, "data", "schedule_cache.json")
        with open(cache_path, encoding="utf-8") as f:
            cache = _json.load(f)
        for week in cache.values():
            if not isinstance(week, dict):
                continue
            for source_key in ("data", "netology"):
                today_lessons = (week.get(source_key) or {}).get(today_str) or []
                for lesson in today_lessons:
                    if lesson.get("course_name"):
                        courses.add(lesson["course_name"])
    except Exception:
        pass

    return sorted(courses)


def _deterministic_knowledge_block() -> str:
    """Принудительно (кодом) вставляем релевантные оргфакты из вебинаров в
    промпт — раньше это была необязательная инструкция модели "проверь файл,
    если относится к делу", без гарантии, что она реально это сделает."""
    courses = _todays_relevant_courses()
    if not courses:
        return ""
    try:
        from scheduler import get_relevant_knowledge_facts
        return get_relevant_knowledge_facts(courses, limit=10)
    except Exception as e:
        log(f"_deterministic_knowledge_block: не удалось собрать факты: {e!r}")
        return ""


OUTPUT_RULE = (
    "Ответь ТОЛЬКО текстом сообщения для Telegram в формате Telegram HTML "
    "(разрешены только теги <b>, <i>, оставь обычные переводы строк для абзацев — "
    "никаких других тегов и никакого markdown-звёздочек/подчёркиваний).\n\n"
    "Структура — по принципу BLUF (bottom line up front), как в деловых executive-брифингах, "
    "а не сплошной прозой:\n"
    "1. Первая строка — САМ ГЛАВНЫЙ ВЫВОД одним коротким предложением, без подписи-категории, "
    "прямо суть (что реально важно прямо сейчас).\n"
    "2. Дальше — короткие подписанные блоки, каждый: <b>Категория:</b> одно-два предложения. "
    "Используй только те категории, для которых есть реальное содержание (не выдумывай, не "
    "заполняй ради количества): <b>Горит</b> (срочные дедлайны/риски), <b>Расписание</b> "
    "(если по теме слота), <b>Прогресс</b> (стрик/баллы, если есть данные), <b>Совет</b> "
    "(короткая рекомендация или мотивация, не всегда нужна).\n"
    "3. Обычно 1-3 таких блока, не больше — если categorий для наполнения нет, оставь только "
    "главный вывод из пункта 1.\n"
    "Тон внутри предложений — живой, не канцелярский, но структура — чёткая и сканируемая. "
    "Не переусердствуй с эмодзи (1 в начале главного вывода — ок, в категориях не нужно). "
    "Без вступлений, без описания своих действий. "
    "ПЕРВЫЙ символ ответа — это первый символ главного вывода. НИКАКИХ фраз до него: "
    "не пиши \"все данные есть\", \"пишу сообщение\", \"вот что нашёл\" и подобное себе под нос."
)

def log(msg: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")


DRY_RUN = "--dry-run" in sys.argv   # собрать и показать, но не отправлять


def pick_slot() -> str:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args and args[0] in SLOTS:
        return args[0]
    now = datetime.datetime.now(tz=UFA_TZ)
    # Ближайший слот по времени (на случай смещения запуска launchd)
    now_minutes = now.hour * 60 + now.minute
    best_key, best_diff = "midmorning", 10**9
    for h, m, key in SLOT_SCHEDULE:
        diff = abs(now_minutes - (h * 60 + m))
        if diff < best_diff:
            best_diff, best_key = diff, key
    # В воскресенье последний вечерний запуск заменяется на недельный обзор.
    # launchd сейчас стартует в 08:30/14:00/20:33 — слота "evening" (18:32)
    # в расписании нет, поэтому раньше "weekly" не срабатывал никогда.
    if now.weekday() == 6 and best_key in ("evening", "winddown"):
        return "weekly"
    return best_key


def refresh_study_analysis():
    """Живой запрос баллов/посещаемости в Modeus — только для вечернего/недельного слота."""
    try:
        from parsers.study_analysis import fetch_study_analysis
        text = asyncio.run(fetch_study_analysis())
        if text and text.lstrip().startswith("❌"):
            # Ошибка Modeus — не затираем последние хорошие данные текстом ошибки.
            log(f"study_analysis: Modeus вернул ошибку, старый файл оставлен: {text[:120]}")
            return
        if text:
            with open(STUDY_ANALYSIS_FILE, "w") as f:
                f.write(f"# Обновлено: {datetime.datetime.now(tz=UFA_TZ).isoformat()}\n\n{text}")
            log("study_analysis обновлён")
    except Exception as e:
        log(f"не удалось обновить study_analysis: {e!r}")


def refresh_weather():
    """Живой запрос погоды — только для утреннего слота. Переиспользует scheduler._fetch_weather."""
    try:
        from scheduler import _fetch_weather
        text = asyncio.run(_fetch_weather())
        if text:
            with open(WEATHER_FILE, "w") as f:
                f.write(text)
            log("погода обновлена")
    except Exception as e:
        log(f"не удалось обновить погоду: {e!r}")


def load_vk_schedule_updates() -> list:
    """Читаем накопленные объявления под локом — общим с scheduler.py, который
    дописывает сюда новые объявления из ВК каждые 15 минут."""
    import json
    with file_lock(VK_SCHEDULE_UPDATES_FILE):
        try:
            with open(VK_SCHEDULE_UPDATES_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return []
        except Exception as e:
            # Отсутствие файла — нормально (ещё не появлялись объявления),
            # любая другая ошибка (битый JSON и т.п.) — логируем, а не глотаем
            # молча (once уже словили тут NameError из-за забытого import).
            log(f"load_vk_schedule_updates: ошибка чтения: {e!r}")
            return []


def remove_consumed_vk_schedule_updates(consumed: list):
    """Убираем из файла только те записи, что реально обработали (сверяем по
    паре текст+время добавления). Раньше файл целиком перезаписывался в [] —
    если scheduler.py дописывал новое объявление уже ПОСЛЕ того как мы прочитали
    снимок (consume_vk_schedule_updates ждёт ответ claude -p до 10 минут), оно
    терялось безвозвратно. Теперь оно просто остаётся в файле и попадёт в
    следующий проход."""
    import json
    if not consumed:
        return
    consumed_keys = {(e.get("text"), e.get("at")) for e in consumed}
    with file_lock(VK_SCHEDULE_UPDATES_FILE):
        try:
            with open(VK_SCHEDULE_UPDATES_FILE) as f:
                current = json.load(f)
        except Exception:
            current = []
        remaining = [e for e in current if (e.get("text"), e.get("at")) not in consumed_keys]
        atomic_write_json(VK_SCHEDULE_UPDATES_FILE, remaining)


def consume_vk_schedule_updates() -> list:
    """Даём Claude прочитать накопленные объявления о расписании из ВК. Заодно
    (на случай если почасовой сторож ещё не успел) извлекаем факты в
    schedule_overrides.json/schedule_vk_digest.json, чтобы их видели ВСЕ
    промпты, а не только этот. Возвращает прочитанный снимок — вызывающая
    сторона должна затем вызвать remove_consumed_vk_schedule_updates(снимок),
    а не очищать файл целиком (см. remove_consumed_vk_schedule_updates)."""
    entries = load_vk_schedule_updates()
    if entries:
        log(f"vk_schedule_updates: {len(entries)} записей будет прочитано")
        extract_and_store_schedule_overrides(entries)
        extract_and_store_vk_daily_digest(entries)
    return entries


SCHEDULE_OVERRIDES_FILE = os.path.join(PROJECT_DIR, "data", "schedule_overrides.json")
SCHEDULE_OVERRIDE_TTL_DAYS = 4


def _prune_schedule_overrides(entries: list) -> list:
    now = datetime.datetime.now(tz=UFA_TZ)
    kept = []
    for e in entries:
        try:
            added = datetime.datetime.fromisoformat(e["added_at"])
            if (now - added).days < SCHEDULE_OVERRIDE_TTL_DAYS:
                kept.append(e)
        except Exception:
            continue
    return kept


def extract_and_store_schedule_overrides(vk_entries: list = None):
    """Отдельный, узкий вызов Claude: превращает сырые объявления в ВК в короткие
    ФАКТИЧЕСКИЕ строки об изменениях расписания и сохраняет их в постоянный файл,
    который видят ВСЕ промпты про расписание (не только тот, что читал ВК первым).
    vk_entries — снимок из load_vk_schedule_updates(); если не передан, читаем
    сами (нужно для вызова из mentor_hourly_watch.py)."""
    import json
    if vk_entries is None:
        vk_entries = load_vk_schedule_updates()
    if not vk_entries:
        return

    raw_texts = "\n---\n".join(e.get("text", "") for e in vk_entries)
    _now = datetime.datetime.now(tz=UFA_TZ)
    _days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    now_str = _now.strftime("%d.%m.%Y") + f" ({_days_ru[_now.weekday()]})"
    prompt = (
        f"Сегодня {now_str}. Учитывай это при переводе относительных дат "
        "(\"завтра\", \"сегодня\", \"послезавтра\") в конкретные ДД.ММ.\n\n"
        "Ниже — сырые сообщения из беседы ВК студенческой группы. Найди среди них "
        "РЕАЛЬНЫЕ изменения расписания (перенос, отмена, добавление пары/вебинара). "
        "Игнорируй вопросы студентов, ответы преподавателей на вопросы про дедлайны "
        "заданий (это не расписание пар) и обычные ежедневные анонсы без изменений.\n\n"
        "Даты пар бери из самих сообщений.\n\n"
        "Выведи ТОЛЬКО короткие фактические строки, по одной на изменение, формат: "
        "\"Предмет — было ДД.ММ, стало ДД.ММ\" или \"Предмет ДД.ММ отменён\". "
        "Если реальных изменений нет — выведи ровно слово: нет.\n"
        "Никаких пояснений, только строки фактов или слово \"нет\".\n\n"
        f"Сообщения:\n{raw_texts}"
    )
    # Узкая структурная задача (найти факт изменения расписания и сжать в
    # строку) — не творческая генерация текста, Haiku справляется и дешевле.
    out, reason = _run_claude(prompt, timeout=300, model=CLAUDE_MODEL_FAST)
    if not out:
        log(f"schedule_overrides: не удалось получить ответ ({reason})")
        return
    if out.strip().lower() in ("нет", "нет.", "нет изменений"):
        log("schedule_overrides: реальных изменений не найдено")
        return

    lines = [l.strip("- ").strip() for l in out.split("\n") if l.strip()]
    now_iso = datetime.datetime.now(tz=UFA_TZ).isoformat()
    with file_lock(SCHEDULE_OVERRIDES_FILE):
        try:
            with open(SCHEDULE_OVERRIDES_FILE) as f:
                existing = json.load(f)
        except Exception:
            existing = []
        existing = _prune_schedule_overrides(existing)
        # mentor_hourly_watch.py и этот чек-ин могут обработать один и тот же
        # снимок vk_schedule_updates.json дважды (если сторож не успел его
        # вычистить до следующего чек-ина) — не дублируем одинаковые факты.
        existing_notes = {e.get("note") for e in existing}
        for line in lines:
            if line not in existing_notes:
                existing.append({"note": line, "added_at": now_iso})
                existing_notes.add(line)
        atomic_write_json(SCHEDULE_OVERRIDES_FILE, existing)
    log(f"schedule_overrides: записано {len(lines)} изменени(й): {lines}")


SCHEDULE_VK_DIGEST_FILE = os.path.join(PROJECT_DIR, "data", "schedule_vk_digest.json")


def extract_and_store_vk_daily_digest(vk_entries: list = None):
    """ВК-объявления 'Учебные/Актуальные мероприятия на сегодня' оказались ПОЛНЕЕ
    официального кэша Modeus (там есть вебинары на my.mts-link.ru, которых в
    schedule_cache.json просто нет) — сохраняем их как основной источник на СЕГОДНЯ,
    с явной пометкой асинхронных (LXP) пунктов без придуманного времени.
    vk_entries — снимок из load_vk_schedule_updates(); если не передан, читаем
    сами (нужно для вызова из mentor_hourly_watch.py)."""
    import json
    if vk_entries is None:
        vk_entries = load_vk_schedule_updates()
    if not vk_entries:
        return

    raw_texts = "\n---\n".join(e.get("text", "") for e in vk_entries)
    _now = datetime.datetime.now(tz=UFA_TZ)
    today_iso = _now.date().isoformat()
    prompt = (
        f"Сегодня {today_iso}. Ниже — сырые сообщения из беседы ВК. Найди среди них "
        "СПИСОК учебных мероприятий на СЕГОДНЯ (обычно заголовок вида 'Учебные "
        "мероприятия на сегодня' или 'Актуальные учебные мероприятия на сегодня'). "
        "Если такого списка нет — выведи ровно слово: нет.\n\n"
        "Если список есть — выведи ПО ОДНОЙ строке на каждый пункт, формат ровно такой "
        "(разделитель — символ |):\n"
        "ВРЕМЯ|Предмет|Тема|формат|ссылка\n"
        "где ВРЕМЯ — 'ЧЧ:ММ' если это живой вебинар/занятие с конкретным временем, "
        "или слово 'асинхронно' если в сообщении написано 'Асинхронно' (значит это "
        "LXP-материал без фиксированного времени, можно смотреть когда угодно — "
        "формат в таком случае должен быть 'асинхронно (LXP)', не выдумывай время). "
        "Для живых вебинаров формат — 'вебинар'. ССЫЛКА — это URL сразу после этого "
        "пункта в сообщении (обычно my.mts-link.ru), если она есть рядом с пунктом; "
        "если ссылки нет — оставь поле пустым, но символ | всё равно поставь.\n"
        "Никаких пояснений — только эти строки или слово \"нет\".\n\n"
        f"Сообщения:\n{raw_texts}"
    )
    # Тоже узкая структурная экстракция (распарсить сообщения в table-like
    # строки) — Haiku, не Sonnet.
    out, reason = _run_claude(prompt, timeout=300, model=CLAUDE_MODEL_FAST)
    if not out:
        log(f"vk_digest: не удалось получить ответ ({reason})")
        return
    if out.strip().lower() in ("нет", "нет.", "нет списка"):
        log("vk_digest: список мероприятий на сегодня не найден в сообщениях")
        return

    items = []
    for line in out.split("\n"):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            url = parts[4] if len(parts) >= 5 else ""
            items.append({"time": parts[0], "subject": parts[1], "topic": parts[2], "format": parts[3], "url": url})
    if not items:
        log(f"vk_digest: не удалось распарсить ответ: {out[:200]}")
        return

    atomic_write_json(SCHEDULE_VK_DIGEST_FILE, {"date": today_iso, "items": items})
    log(f"vk_digest: сохранено {len(items)} пункт(ов) на {today_iso}")


def find_claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _classify_claude_failure(stderr: str) -> str:
    if re.search(r"limit|usage|quota|429", stderr, re.IGNORECASE):
        return "лимит подписки исчерпан"
    if re.search(r"network|connection|resolve|dns|econnrefused|enotfound|unreachable", stderr, re.IGNORECASE):
        return "нет соединения с сетью"
    return "ошибка claude"


CLAUDE_MODEL_SMART = "claude-sonnet-5"       # живой текст с тоном/характером — чек-ины
CLAUDE_MODEL_FAST = "claude-haiku-4-5-20251001"  # узкая структурная экстракция из текста, без "личности"


def _run_claude(prompt: str, timeout: int, allowed_tools: str = "Read,Glob", model: str = CLAUDE_MODEL_SMART) -> tuple:
    """Единая точка вызова `claude -p` для всего наставника. Никогда не
    поднимает исключение наружу — отсутствие интернета, исчерпанный лимит
    подписки, зависший процесс или любая другая ошибка ОС превращаются в
    пустой текст + понятную причину, чтобы вызывающий код мог тихо
    деградировать (записать пропуск, оставить прежнюю сводку и т.п.), а не
    падать с трейсбеком посреди launchd-джобы.

    Разовый вызов без дневной сессии и без инструментов — используется для
    узких извлечений из текста (переносы пар из ВК), всё нужное уже в prompt.
    allowed_tools оставлен в сигнатуре для совместимости и не используется."""
    sys.path.insert(0, PROJECT_DIR)
    from claude_session import run_claude_oneshot
    text, err = run_claude_oneshot(
        prompt, "Ты извлекаешь факты из текста. Отвечай строго в заданном формате, без пояснений.",
        timeout, model, "mentor_extract",
    )
    if err:
        return "", err
    return _strip_preamble(text), ""


QUOTES_FILE = os.path.join(PROJECT_DIR, "data", "quotes.json")
QUOTE_STATE_FILE = os.path.join(PROJECT_DIR, "data", "mentor_quote_state.json")


def get_daily_quote() -> str:
    """Одна цитата на календарный день, без повторов, пока не пройдём весь список —
    переиспользует уже готовый data/quotes.json (350 цитат).

    file_lock — эту же функцию может дёрнуть и mentor_checkin.py, и
    mentor_hourly_watch.py и окно на столе (рендер mentor_dashboard), окна
    выполнения которых пересекаются) — без лока конкурентный read-modify-write
    "remaining"-очереди мог потерять чужой прогресс или выдать одну и ту же
    цитату дважды за цикл."""
    import json
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    with file_lock(QUOTE_STATE_FILE):
        try:
            with open(QUOTE_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            state = {}

        if state.get("date") == today and state.get("quote"):
            return state["quote"]

        try:
            with open(QUOTES_FILE, encoding="utf-8") as f:
                all_quotes = json.load(f)
        except Exception as e:
            log(f"quote: не удалось прочитать quotes.json: {e!r}")
            return ""

        remaining = state.get("remaining", [])
        if not remaining:
            import random
            remaining = list(range(len(all_quotes)))
            random.shuffle(remaining)

        idx = remaining.pop(0)
        quote = all_quotes[idx]
        try:
            atomic_write_json(QUOTE_STATE_FILE, {"date": today, "quote": quote, "remaining": remaining})
        except Exception as e:
            log(f"quote: не удалось сохранить состояние: {e!r}")
        return quote


def record_missed_checkin(slot: str, reason: str):
    """Чек-ин не отправился — запоминаем, чтобы следующая успешная отправка
    ответила и за него тоже (общим, цельным сообщением), а не молчала."""
    import json
    missed = []
    try:
        with open(MISSED_FILE) as f:
            missed = json.load(f)
    except Exception:
        pass
    missed.append({
        "slot": slot,
        "label": SLOT_LABELS.get(slot, slot),
        "reason": reason,
        "at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
    })
    atomic_write_json(MISSED_FILE, missed[-6:])
    log(f"пропуск записан: слот={slot}, причина={reason}")


def load_missed() -> list:
    """Только читаем, не очищаем — если текущий запуск тоже упадёт, старые
    пропуски не должны потеряться (record_missed_checkin допишет к ним)."""
    import json
    try:
        with open(MISSED_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def clear_missed():
    try:
        atomic_write_json(MISSED_FILE, [])
    except Exception as e:
        log(f"ошибка очистки missed: {e!r}")


_PREAMBLE_MARKERS = ("данные есть", "пишу сообщение", "вот что нашёл", "сейчас отвечу", "хорошо, ",
                     "финальный ответ", "вот ответ", "here is", "final answer")


def _strip_preamble(text: str) -> str:
    """Страховка: если модель всё же дописала себе под нос что-то вроде "все
    данные есть, пишу сообщение" первой строкой — убираем эту строку."""
    lines = text.split("\n", 1)
    if len(lines) == 2 and any(m in lines[0].lower() for m in _PREAMBLE_MARKERS):
        return lines[1].strip()
    return text


def _plain_text(html_text: str) -> str:
    """Убираем <b>/<i> для мест, куда HTML не годится (Напоминания, меню-бар)."""
    return re.sub(r"</?[bi]>", "", html_text).strip()




def sync_to_reminders(text: str = ""):
    """Список 'Наставник' в приложении «Напоминания» macOS — те же списки,
    что в окне на столе (детерминированно, scripts/mentor_dashboard.py).
    Параметр text оставлен для совместимости и не используется."""
    try:
        from mentor_dashboard import build_model, model_as_text
        model = build_model()
        body_plain = model_as_text(model)
        title = f"Наставник: просрочено {len(model['overdue'])}, дедлайнов на 3 дня {len(model['deadlines'])}"
    except Exception as e:
        log(f"Напоминания: не удалось собрать списки: {e!r}")
        return
    # Бэкслеши экранируем ПЕРВЫМИ — иначе экранирование кавычки (" -> \") само
    # дописывает бэкслеш, а следующий .replace("\\", "\\\\") его удваивает.
    body = body_plain.replace("\\", "\\\\").replace('"', '\\"')
    title_esc = title.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Reminders"
        if not (exists list "Наставник") then
            make new list with properties {{name:"Наставник"}}
        end if
        set theList to list "Наставник"
        repeat while (count of (reminders of theList whose completed is false)) > 0
            delete (first reminder of theList whose completed is false)
        end repeat
        make new reminder at end of theList with properties {{name:"{title_esc}", body:"{body}", remind me date:(current date), due date:(current date)}}
    end tell
    '''
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            log(f"ошибка синхронизации с Напоминаниями: {result.stderr.strip()[:300]}")
        else:
            log("синхронизировано с Напоминаниями")
    except Exception as e:
        log(f"не удалось синхронизировать с Напоминаниями: {e!r}")


def render_feed_html(history: list = None):
    """Пересобрать окно на столе (списки из data/*.json, без Claude).
    Аргумент history оставлен для совместимости старых вызовов."""
    try:
        from mentor_dashboard import render_feed
        render_feed()
    except Exception as e:
        log(f"не удалось записать mentor_feed.html: {e!r}")


def send_telegram(text: str, feedback_id: str = ""):
    """feedback_id — кнопки 👍/👎 под сообщением (шаг 4 плана: оценка
    полезности, обработчик mfb: в bot/handlers.py пишет data/mentor_feedback.jsonl)."""
    import json as _json
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    markup = None
    if feedback_id:
        markup = _json.dumps({"inline_keyboard": [[
            {"text": "👍 Полезно", "callback_data": f"mfb:{feedback_id}:up"},
            {"text": "👎 Мимо", "callback_data": f"mfb:{feedback_id}:down"},
        ]]})
    data = {"chat_id": MY_TELEGRAM_ID, "text": text, "parse_mode": "HTML"}
    if markup:
        data["reply_markup"] = markup
    r = requests.post(url, data=data, timeout=30)
    if not r.ok:
        # Если модель всё же прислала невалидный HTML — не теряем сообщение,
        # шлём как обычный текст без форматирования.
        data.pop("parse_mode")
        r2 = requests.post(url, data=data, timeout=30)
        r2.raise_for_status()


def send_profile_claim(claim: str):
    """Наблюдение недельного итога попадает в память только после «Да»
    (agent_db.profile_claims, обработчик pc: в bot/handlers.py)."""
    import json as _json
    from html import escape
    from agent_db import add_profile_claim
    cid = add_profile_claim(claim)
    markup = _json.dumps({"inline_keyboard": [[
        {"text": "✅ Верно, запомни", "callback_data": f"pc:{cid}:confirmed"},
        {"text": "✖️ Нет", "callback_data": f"pc:{cid}:rejected"},
    ]]})
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": MY_TELEGRAM_ID, "parse_mode": "HTML", "reply_markup": markup,
                            "text": f"🧠 Я заметил: <i>{escape(claim)}</i>\nЗапомнить это о тебе?"}, timeout=20)
    except Exception as e:
        log(f"profile claim: не отправлено: {e!r}")


def _build_catchup_block(missed: list) -> str:
    if not missed:
        return ""
    lines = []
    for m in missed:
        when = m.get("at", "")[:16].replace("T", " ")
        lines.append(f"- {when}: должен был быть чек-ин «{m.get('label')}», не отправился ({m.get('reason')})")
    return (
        "\n\nВАЖНО: предыдущие чек-ины НЕ отправились — их тема ещё не раскрыта:\n"
        + "\n".join(lines) +
        "\nОтветь и за них тоже — одним цельным сообщением, без упоминания механики пропуска."
    )


def main():
    slot = pick_slot()
    cfg = SLOTS[slot]
    log(f"старт, слот={slot}")

    missed = load_missed()
    missed_needs_vk = any(m.get("slot") == "schedule_focus" for m in missed)

    # --dry-run: ничего не обновляем и не отправляем — только собираем
    # сводку и показываем ответ модели (один вызов Claude всё же будет).
    if cfg["need_study_analysis"] and not DRY_RUN:
        refresh_study_analysis()
    if cfg["need_weather"] and not DRY_RUN:
        refresh_weather()
    consumed_vk_entries = []
    if (cfg["need_vk_schedule"] or missed_needs_vk) and not DRY_RUN:
        consumed_vk_entries = consume_vk_schedule_updates()

    # Context pack вместо --resume дневной сессии с Read по сырым JSON:
    # код собирает только нужные слоту блоки данных, модель пишет текст
    # (было 0,3–1,3 млн токенов за утренний вызов). См. scripts/mentor_context.py.
    from mentor_context import build_checkin_pack, record_sent
    from claude_session import run_claude_oneshot
    extra = ["vk"] if missed_needs_vk else []
    pack = build_checkin_pack(slot, extra_blocks=extra)
    user_prompt = (
        f"ЗАДАНИЕ ({SLOT_LABELS.get(slot, slot)}):\n{cfg['focus']}{_build_catchup_block(missed)}"
        f"\n\n{pack}"
    )
    system_prompt = PERSONA.format(user_name=USER_NAME) + "\n\n" + OUTPUT_RULE
    text, reason = run_claude_oneshot(
        user_prompt, system_prompt, timeout=420 if slot == "weekly" else 300,
        model=CLAUDE_MODEL_SMART, purpose=f"checkin_{slot}",
    )
    text = _strip_preamble(text) if text else text
    if not text:
        log(f"claude: не удалось получить ответ ({reason})")
        if not DRY_RUN:
            record_missed_checkin(slot, reason)
        return

    profile_claims = []
    if "===PROFILE===" in text:
        text, tail = text.split("===PROFILE===", 1)
        text = text.strip()
        profile_claims = [l.strip(" -•\t") for l in tail.strip().splitlines() if len(l.strip(" -•\t")) > 10][:3]

    if DRY_RUN:
        print(f"--- PACK ({len(user_prompt)} симв.) ---\n{user_prompt}\n\n--- ОТВЕТ ---\n{text}")
        if profile_claims:
            print("--- НАБЛЮДЕНИЯ ---\n" + "\n".join(profile_claims))
        return

    import uuid
    feedback_id = uuid.uuid4().hex[:10]
    try:
        send_telegram(text, feedback_id=feedback_id)
        log(f"[{slot}] отправлено: {text[:150]}")
        record_sent(slot, text, feedback_id)
    except Exception as e:
        log(f"ошибка отправки в telegram: {e!r}")
        record_missed_checkin(slot, "не удалось отправить в telegram")
        return

    for claim in profile_claims:
        send_profile_claim(claim)

    # Окно на столе Claude больше не пишет (решение пользователя 2026-10-02:
    # выводы в пару предложений не стоили ~14–240 тыс. токенов за раз) —
    # списки строит scripts/mentor_dashboard.py, окно пересобирается само.
    if slot in REMINDERS_SYNC_SLOTS:
        sync_to_reminders()

    if missed:
        clear_missed()
    if cfg["need_vk_schedule"] or missed_needs_vk:
        remove_consumed_vk_schedule_updates(consumed_vk_entries)


if __name__ == "__main__":
    main()
