import asyncio
import datetime
import json
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import UFA_TZ, PARSE_HOURS, USER_NAME, WEATHER_LAT, WEATHER_LON, VK_CHAT_URLS, TASKS_FILE
from storage import get_pending_tasks as _get_pending_raw, get_tasks, save_tasks
from data_lock import file_lock, atomic_write_json


def get_pending_tasks():
    """Задачи без reminder_only — они не должны попадать в брифинги и дедлайны."""
    return [t for t in _get_pending_raw() if t.get('source') != 'reminder_only']


MONTHS_RU = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
DAYS_RU = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]


PENDING_NOTIFICATIONS_FILE = "data/pending_notifications.json"
JARVIS_QUEUE_FILE = "data/jarvis_queue.json"

# Lock для Playwright — мессенджер и ВК не запускаются одновременно
import asyncio as _asyncio_lock
_playwright_lock = _asyncio_lock.Lock()


async def _retry(coro_fn, attempts=3, delay=5):
    """Повторяем корутину до attempts раз при ошибке или пустом результате."""
    for i in range(attempts):
        try:
            result = await coro_fn()
            if result:
                return result
        except Exception as e:
            print(f"_retry: попытка {i+1}/{attempts} упала: {e!r}")
        if i < attempts - 1:
            await asyncio.sleep(delay)
    return None


async def _ask_claude_cli(prompt: str, timeout: int = 60, model: str = "claude-haiku-4-5-20251001") -> str:
    """Разовый вызов `claude -p` без доступа к файлам — все нужные данные уже
    переданы в prompt текстом, так что --allowedTools пустой. Использует
    подписку Claude Code (как наставник), а не Groq — тот регулярно упирается
    в лимит/недоступен (см. README, GROQ_KEY_1).

    Разовый вызов без дневной сессии (claude_session.run_claude_oneshot):
    раньше здесь был --resume сессии дня, и каждый вызов заново пересылал весь
    разговор дня. Связность даёт get_ai_memory_recap() в самих промптах."""
    from claude_session import run_claude_oneshot
    append_system_prompt = (
        # Явно закрепляем имя студента поверх системного промпта — этот
        # вызов не получает файлового контекста и полагается только на
        # текст prompt (обычно "{USER_NAME}а/у" грамматически склеенное).
        # Обнаружено 2026-09-20: вечернее ИИ-сообщение вдруг стало
        # обращаться к студенту чужим именем (другого человека из переписки)
        # — и раз попав в data/ai_memory_log.json,
        # самозакреплялось в каждом следующем вызове через get_ai_memory_recap().
        f"Студента, для которого ты сейчас пишешь короткое сообщение, зовут "
        f"{USER_NAME} — используй только это имя, даже если в тексте промпта "
        f"или истории упоминаются другие имена людей (одногруппники, "
        f"преподаватели и т.п.) — они НЕ адресат сообщения."
    )
    system_prompt = (
        "Ты — личный наставник студента в Telegram-боте: пишешь по-русски, живо, "
        "на «ты», без канцелярита. Все данные — в сообщении, файлы не читаешь.\n\n"
        + append_system_prompt
    )
    text, err = await asyncio.to_thread(
        run_claude_oneshot, prompt, system_prompt, timeout, model, "bot_ai_message",
    )
    if err:
        print(f"Claude CLI: {err}")
        return ""
    # Страховка: модель иногда всё равно дописывает markdown-** (легаси
    # Telegram Markdown понимает только одиночную *, двойная звёздочка
    # оставалась бы в тексте буквально) — нормализуем на этом уровне
    # один раз для всех вызовов, а не в каждом промпте отдельно.
    return text.replace("**", "*")


# ─── Общая память для учебных AI-сообщений ────────────────────────────
# Не настоящая сессия Claude (--resume): между вызовами часы, кэш промптов
# не спасает, а окно контекста (200к) кончится задолго до желаемых 400к —
# каждый вызов пересчитывал бы всю историю с нуля и дороже, и медленнее.
# Вместо этого — растущий журнал коротких записей (то, что реально было
# отправлено пользователю) + периодически пересжимаемый в абзац "профиль"
# устойчивых фактов. Каждый новый промпт получает маленький фиксированный
# рекап, а не всю историю — стоимость вызова не растёт со временем.
AI_MEMORY_LOG_FILE = "data/ai_memory_log.json"
AI_MEMORY_PROFILE_FILE = "data/ai_memory_profile.txt"
AI_MEMORY_LOG_MAX = 30   # после этого старые записи сжимаются в профиль
AI_MEMORY_LOG_KEEP = 15  # сколько последних оставляем как есть при сжатии


def _load_ai_memory_log() -> list:
    try:
        with open(AI_MEMORY_LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _load_ai_memory_profile() -> str:
    try:
        with open(AI_MEMORY_PROFILE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


STUDY_KNOWLEDGE_FILE = "data/study_knowledge.json"
# kind'ы, реально полезные для разбора дедлайнов/задач — не тащим в промпт
# всё подряд (kind="candidate" — необработанные кандидаты, их в 6 раз больше
# остальных вместе взятых, и большинство status=needs_review, не факт).
_KNOWLEDGE_KINDS_FOR_TASKS = {"deadline", "course_organization", "homework", "schedule"}


def get_relevant_knowledge_facts(course_names, limit: int = 6) -> str:
    """Короткая выжимка организационных фактов из вебинаров (data/study_knowledge.json),
    относящихся к данным курсам — для промптов scheduler.py (_ask_claude_cli без
    доступа к файлам, весь контекст только текстом). Раньше этим пользовался
    только наставник (scripts/mentor_checkin.py, отдельный процесс с доступом
    к Read/Grep) — сам бот факты из вебинаров не учитывал вообще, даже когда
    разбирал те же просроченные задачи, к которым эти факты прямо относятся.

    Только status="active" (проверенные) — "needs_review" ещё не подтверждены,
    не выдаём как факт. Файл может быть большим (эта же выжимка) — читаем и
    фильтруем в Python, не полагаемся на то, что модель сама найдёт нужное
    (не даём ей вообще доступа к файлу в этих промптах, только готовый текст)."""
    try:
        with open(STUDY_KNOWLEDGE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ""

    wanted = {c.strip().lower() for c in (course_names or []) if c and c.strip()}
    if not wanted:
        return ""

    matched = []
    for fact in data.get("facts", []) or []:
        if fact.get("status") != "active":
            continue
        if fact.get("kind") not in _KNOWLEDGE_KINDS_FOR_TASKS:
            continue
        course = (fact.get("course") or "").strip().lower()
        if not any(w in course or course in w for w in wanted):
            continue
        matched.append(fact)

    if not matched:
        return ""

    # importance="high" вперёд, остальное — как есть (в файле уже примерно
    # по порядку появления на вебинаре)
    matched.sort(key=lambda f: 0 if f.get("importance") == "high" else 1)
    matched = matched[:limit]

    lines = [
        f"- [{f.get('course','')}] {f.get('text','')}"
        for f in matched
    ]
    return (
        "Организационные факты с вебинаров по этим предметам (подтверждённые, "
        "из data/study_knowledge.json) — учти при разборе, если релевантно, но "
        "не выдумывай сверх того, что написано:\n" + "\n".join(lines)
    )


def get_ai_memory_recap() -> str:
    """Короткий блок для вставки в промпт любого учебного AI-сообщения —
    долгосрочный профиль + несколько последних записей. Синхронная —
    только чтение файлов, без вызова AI."""
    profile = _load_ai_memory_profile()
    log = _load_ai_memory_log()[-5:]
    parts = []
    if profile:
        parts.append(f"Долгосрочная память о студенте:\n{profile}")
    if log:
        recent = "\n".join(f"- [{e.get('source','')} {e.get('at','')[:10]}] {e.get('note','')}" for e in log)
        parts.append(f"Последние сообщения ему по учёбе:\n{recent}")
    if not parts:
        return ""
    return (
        "\n\n".join(parts) +
        "\n\nЭто память о прошлых сообщениях — не повторяй то же самое теми же словами, "
        "учитывай, что уже говорилось."
    )


async def record_ai_memory(source: str, note: str):
    """Фиксируем то, что реально отправили пользователю (source — например
    'midday'/'evening'/'itog'/'analysis'). Когда журнал переполняется — не
    обрезаем старое молча, а сжимаем его в долгосрочный профиль одним
    вызовом Claude (дёшево: это разовая операция раз в N сообщений, а не
    на каждый вызов)."""
    with file_lock(AI_MEMORY_LOG_FILE):
        log = _load_ai_memory_log()
        log.append({
            "at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
            "source": source,
            "note": (note or "")[:400],
        })
        if len(log) > AI_MEMORY_LOG_MAX:
            old, keep = log[:-AI_MEMORY_LOG_KEEP], log[-AI_MEMORY_LOG_KEEP:]
            old_text = "\n".join(f"[{e.get('source','')} {e.get('at','')[:10]}] {e.get('note','')}" for e in old)
            prev_profile = _load_ai_memory_profile()
            prompt = (
                (f"Текущий долгосрочный профиль студента:\n{prev_profile}\n\n" if prev_profile else "") +
                f"Новые записи для объединения в профиль:\n{old_text}\n\n"
                "Обнови долгосрочный профиль: устойчивые факты, повторяющиеся паттерны "
                "поведения, важные обещания/прогнозы, которые стоит помнить надолго. "
                "5-8 предложений, по-русски, связным текстом (без markdown, без списка), "
                "только суть, без вступлений."
            )
            try:
                new_profile = await _ask_claude_cli(prompt, timeout=90)
            except Exception as e:
                new_profile = ""
                print(f"AI memory: сжатие профиля упало: {e!r}")
            if new_profile:
                try:
                    with open(AI_MEMORY_PROFILE_FILE, "w", encoding="utf-8") as f:
                        f.write(new_profile)
                    log = keep
                except Exception as e:
                    print(f"AI memory: не удалось сохранить профиль: {e!r}")
            # Если сжатие не удалось — записи не теряем, оставляем журнал как есть
            # (он просто ещё немного подрастёт до следующей удачной попытки).
        atomic_write_json(AI_MEMORY_LOG_FILE, log)


def _jarvis_should_read(text: str) -> bool:
    """Определяем стоит ли передавать сообщение Джарвису."""
    skip = [
        "Выбери фильтр", "Выбери период", "ДедЛайнер запущен",
        "Привет, " + USER_NAME, "Отмечено выполненными", "Задание удалено",
        "Все задания", "Личные задачи", "Показано:", "Выбери задания",
        "Срочные", "Редактировать", "Удалить", "Отмена", "Сохранить",
        "Это домашнее задание", "Пропустить", "Заданий нет",
        "Выбери задания которые",
    ]
    if any(p in text for p in skip):
        return False
    # Английский и теория — читаем всегда независимо от длины
    always_read = ["АНГЛИЙСКИЙ", "ТЕОРИЯ ДНЯ", "Анекдот на ночь", "СЛОВО ДНЯ"]
    if any(p in text for p in always_read):
        return True
    if len(text) > 2000:
        return False
    return True


def _jarvis_write(text: str):
    """Пишем сообщение в очередь для Джарвиса (атомарная запись). Вызывается из
    send_with_retry — а значит потенциально конкурентно из множества джобов
    планировщика, поэтому read-modify-write защищён локом, как и остальные
    общие data/*.json в этом файле."""
    if not _jarvis_should_read(text):
        return
    try:
        os.makedirs("data", exist_ok=True)
        with file_lock(JARVIS_QUEUE_FILE):
            try:
                with open(JARVIS_QUEUE_FILE) as f:
                    queue = json.load(f)
            except Exception:
                queue = []
            queue.append({
                "text": text,
                "ts": datetime.datetime.now(tz=UFA_TZ).isoformat()
            })
            if len(queue) > 50:
                queue = queue[-50:]
            atomic_write_json(JARVIS_QUEUE_FILE, queue)
    except Exception as e:
        print(f"Jarvis queue error: {e!r}")
RANDOM_SCHEDULE_FILE = "data/random_reminders.json"
LESSON_REMINDERS_FILE = "data/lesson_reminders_sent.json"
SENT_NOTIFICATIONS_FILE = "data/sent_notifications.json"

def _clean_joke(text: str) -> str:
    """Убираем всё лишнее после основного текста — скобки, PS, подписи."""
    import re as _re
    if not text:
        return text
    # Убираем строки начинающиеся со скобок, P.S., (P.S., примечаний
    lines = text.split("\n")
    clean = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Стоп-паттерны — строки которые надо убрать
        if _re.match(r"^[\(\[\*]", stripped):
            break
        if _re.match(r"^(P\.S\.|PS|Примечание|Note:|Если|Можно|—|\*)", stripped, _re.IGNORECASE):
            break
        clean.append(line)
    result = "\n".join(clean).strip()
    # Обрезаем по последнему знаку препинания
    m = _re.search(r"[.!?»\"']+\s*$", result)
    if m:
        result = result[:m.end()].strip()
    return result or text



FACTS_FILE = "data/facts.json"
FACTS_INDEX_FILE = "data/facts_index.json"


def _get_next_fact() -> str:
    """Возвращает следующий факт по кругу."""
    try:
        with open(FACTS_FILE, encoding="utf-8") as f:
            facts = json.load(f)
        try:
            with open(FACTS_INDEX_FILE) as f:
                data = json.load(f)
            idx = data.get("index", 0) % len(facts)
        except Exception:
            idx = 0
        fact = facts[idx]
        # Сохраняем следующий индекс
        os.makedirs("data", exist_ok=True)
        with open(FACTS_INDEX_FILE, "w") as f:
            json.dump({"index": (idx + 1) % len(facts)}, f)
        return fact
    except Exception:
        return ""



def _notify_header(category: str) -> str:
    """Жирный заголовок категории для каждого уведомления."""
    return f"*{category}*\n\n"


DAILY_STATS_FILE = "data/daily_stats.json"


def _load_daily_stats() -> dict:
    try:
        with open(DAILY_STATS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_daily_stats(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(DAILY_STATS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_daily_stats(done_today: int, pending: int, passed_lessons: int):
    """Записываем статистику дня. Вызывается из send_daily_results."""
    stats = _load_daily_stats()
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    stats[today] = {
        "done": done_today,
        "pending": pending,
        "lessons": passed_lessons,
        "recorded_at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
    }
    # Храним последние 90 дней
    if len(stats) > 90:
        keys = sorted(stats.keys())
        for k in keys[:-90]:
            del stats[k]
    _save_daily_stats(stats)


def get_weekly_done_avg() -> float:
    """Среднее выполненных задач за последние 7 дней."""
    stats = _load_daily_stats()
    today = datetime.datetime.now(tz=UFA_TZ).date()
    total = 0
    days = 0
    for i in range(7):
        d = (today - datetime.timedelta(days=i+1)).isoformat()
        if d in stats:
            total += stats[d].get("done", 0)
            days += 1
    return round(total / days, 1) if days else 0


def get_stats_summary() -> str:
    """Краткая сводка за последние 7 дней для промпта ИИ."""
    stats = _load_daily_stats()
    today = datetime.datetime.now(tz=UFA_TZ).date()
    lines = []
    for i in range(7):
        d = (today - datetime.timedelta(days=i+1)).isoformat()
        if d in stats:
            s = stats[d]
            lines.append(f"{d}: выполнено {s.get('done',0)}, осталось {s.get('pending',0)}, пар {s.get('lessons',0)}")
    return "\n".join(lines) if lines else "нет данных"




def _load_sent_notifications() -> set:
    try:
        with open(SENT_NOTIFICATIONS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_sent_notifications(sent: set):
    os.makedirs("data", exist_ok=True)
    with open(SENT_NOTIFICATIONS_FILE, "w") as f:
        json.dump(list(sent), f, ensure_ascii=False)


def _is_notification_sent(key: str) -> bool:
    return key in _load_sent_notifications()


def _mark_notification_sent(key: str):
    # file_lock — вызывается из нескольких независимых джобов планировщика
    # (check_grades_and_notify, check_lms_grades_and_notify и т.д.), которые
    # реально могут выполняться параллельно (разные offset'ы старта на одном
    # 60-минутном интервале). Без лока конкурентный read-modify-write мог
    # потерять чужую отметку "отправлено" — та же оценка ушла бы дублем.
    with file_lock(SENT_NOTIFICATIONS_FILE):
        sent = _load_sent_notifications()
        sent.add(key)
        # Храним не больше 1000 записей
        if len(sent) > 1000:
            sent = set(list(sent)[-1000:])
        _save_sent_notifications(sent)
DEADLINE_SENT_FILE = "data/sent_deadline_reminders.json"


# ─── Pending notifications ────────────────────────────────────────────

def _load_pending_notifications() -> list:
    try:
        with open(PENDING_NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_pending_notifications(notifications: list):
    # Атомарно (temp + replace): обрыв посреди записи не обнулит очередь.
    atomic_write_json(PENDING_NOTIFICATIONS_FILE, notifications)


def _add_pending_notification(chat_id: int, text: str, parse_mode: str = "Markdown"):
    # file_lock — send_with_retry (и через него _add_pending_notification)
    # вызывается конкурентно из множества независимых джобов планировщика
    # (почта/мессенджер/ВК/оценки и т.д.), особенно пачкой в начале тихих
    # часов. Без лока конкурентный read-modify-write мог потерять чужое
    # уведомление, добавленное почти одновременно.
    with file_lock(PENDING_NOTIFICATIONS_FILE):
        pending = _load_pending_notifications()
        # Дедупликация — не добавляем если такой текст уже есть в очереди
        for existing in pending:
            if existing.get("chat_id") == chat_id and existing.get("text") == text:
                print(f"Scheduler: дубликат уведомления пропущен")
                return
        pending.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "created_at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
            "attempts": 0,
        })
        _save_pending_notifications(pending)
        print(f"Scheduler: уведомление в очередь (всего: {len(pending)})")


def _in_quiet_hours(now_h: int) -> bool:
    """Бот работает 8:00-22:00 — за пределами этого окна ничего не шлём,
    всё уходит в очередь и доставляется retry_pending_notifications с 8 утра."""
    return now_h >= 22 or now_h < 8


def _split_for_telegram(text: str, limit: int = 3900) -> list[str]:
    """Если текст не влезает в лимит Telegram на одно сообщение (4096 симв.) —
    делим по границам строк и шлём несколько сообщений подряд. Контент никогда
    не обрезаем (было — почта/мессенджер резались по [:600])."""
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


async def send_with_retry(bot, chat_id: int, text: str, parse_mode: str = "Markdown", reply_markup=None, ignore_quiet_hours: bool = False, disable_web_page_preview: bool = False):
    # Тихие часы 22:00-08:00 — не отправляем (кроме явного игнорирования)
    if not ignore_quiet_hours:
        now_h = datetime.datetime.now(tz=UFA_TZ).hour
        if _in_quiet_hours(now_h):
            print(f"Scheduler: тихие часы ({now_h}:xx) — сообщение отложено")
            _add_pending_notification(chat_id, text, parse_mode)
            return False
    chunks = _split_for_telegram(text)
    ok = True
    for i, chunk in enumerate(chunks):
        kb = reply_markup if i == len(chunks) - 1 else None
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode, reply_markup=kb,
                                    disable_web_page_preview=disable_web_page_preview)
            _jarvis_write(chunk)
        except Exception as e:
            # Разбивка могла задеть HTML-тег (например <blockquote> без пары
            # в этом куске) — пробуем без форматирования, чтобы не терять
            # содержимое из-за подсветки.
            try:
                await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=kb,
                                        disable_web_page_preview=disable_web_page_preview)
                _jarvis_write(chunk)
            except Exception as e2:
                print(f"Scheduler: не удалось отправить: {e2}")
                _add_pending_notification(chat_id, chunk, parse_mode)
                ok = False
    return ok


async def retry_pending_notifications(bot):
    # Источники (почта/ВК/мессенджер/Netology/Gmail) помечают сообщение
    # "увиденным" ДО попытки отправки (осознанно — иначе следующий же
    # цикл опроса найдёт тот же непрочитанный элемент и продублирует его
    # в эту же очередь; _add_pending_notification и так дедуплицирует по
    # точному тексту, но параллельная прямая отправка при восстановлении
    # сети всё равно могла бы уйти вторым разом). Поэтому здесь — единственное
    # место, откуда сообщение реально может дойти до пользователя, если первая
    # попытка не удалась. Раньше после 144 попыток (~24ч) запись тихо
    # выбрасывалась — при сутки-длинном сбое сети/Telegram сообщение исчезало
    # безвозвратно, и источник уже никогда не отдаст его повторно. Теперь
    # повторяем без ограничения по времени — риска бесконтрольного роста нет,
    # т.к. новые записи не дублируются (дедуп по тексту), а старые постепенно
    # уходят по мере восстановления связи.
    now_h = datetime.datetime.now(tz=UFA_TZ).hour
    if _in_quiet_hours(now_h):
        return
    # Лок НЕ держим во время await bot.send_message: file_lock — синхронный
    # flock, и если пока мы ждём сеть, другая джоба зайдёт в
    # _add_pending_notification, она заблокирует ВЕСЬ event loop на том же
    # локе — и наш await уже никогда не продолжится (ревью Codex 2026-10-02).
    # Поэтому: под локом забрали снимок → отправили без лока → под локом
    # убрали из очереди только реально доставленное (новые записи сохраняются).
    with file_lock(PENDING_NOTIFICATIONS_FILE):
        pending = _load_pending_notifications()
    if not pending:
        return
    print(f"Scheduler: retry {len(pending)} уведомлений...")
    delivered, failed = set(), {}
    for n in pending:
        key = (n.get("chat_id"), n.get("text"))
        try:
            await bot.send_message(chat_id=n["chat_id"], text=n["text"], parse_mode=n.get("parse_mode", "Markdown"))
            delivered.add(key)
        except Exception as e:
            failed[key] = n.get("attempts", 0) + 1
            if failed[key] % 50 == 0:
                print(f"Scheduler: уведомление всё ещё не доставлено после {failed[key]} попыток: {e!r}")
    with file_lock(PENDING_NOTIFICATIONS_FILE):
        current = _load_pending_notifications()
        remaining = []
        for n in current:
            key = (n.get("chat_id"), n.get("text"))
            if key in delivered:
                continue
            if key in failed:
                n["attempts"] = failed[key]
            remaining.append(n)
        _save_pending_notifications(remaining)


# ─── Синхронизация расписания ─────────────────────────────────────────

async def refresh_schedule_cache_silent(bot=None, chat_id=None):
    """Тихое фоновое обновление data/schedule_cache.json (Modeus + Нетология),
    без отправки чего-либо в Telegram. Раньше единственный код, который живьём
    наполнял этот файл (get_week_schedule), запускался только как побочный
    эффект проактивных утренних/дневных брифингов — после их отключения
    (наставник теперь отдельным процессом) кэш перестал обновляться сам
    вообще, единственный способ был вручную зайти в /schedule и нажать
    «Загрузить свежее». Наставник и виджет на столе читали замороженный
    снимок (пропускали новые/перенесённые пары, например английский).
    get_week_schedule сам кэширует Modeus-часть на 12ч — при более частом
    вызове это дёшево (без запроса) — данные подтягиваются не чаще, чем
    реально имеет смысл."""
    from storage import is_quiz_active
    if is_quiz_active():
        print("refresh_schedule_cache: пропуск — активен квиз")
        return
    try:
        from parsers.modeus import get_week_schedule, _load_schedule_cache, _save_schedule_cache, SCHEDULE_CACHE_FILE
        from parsers.netology import fetch_netology_schedule_week
        from data_lock import file_lock

        today = datetime.datetime.now(tz=UFA_TZ).date()
        this_week = today - datetime.timedelta(days=today.weekday())
        for week_start in (this_week, this_week + datetime.timedelta(weeks=1)):
            key = week_start.isoformat()
            # get_week_schedule делает 7 последовательных запросов к Modeus
            # (по дню) при промахе кэша — 25с (как в интерактивном /schedule,
            # где торопиться есть ради) тут не хватает, это фоновая джоба.
            modeus_data, netology_data = await asyncio.gather(
                asyncio.wait_for(get_week_schedule(week_start), timeout=90),
                asyncio.wait_for(fetch_netology_schedule_week(week_start), timeout=30),
                return_exceptions=True,
            )

            with file_lock(SCHEDULE_CACHE_FILE):
                cache = _load_schedule_cache()
                prev = cache.get(key, {})

                # get_schedule()/fetch_netology_schedule_week() сами глотают сетевые
                # ошибки и при сбое возвращают ПУСТОЙ результат, а не исключение —
                # неотличимо от "пар реально нет". Если раньше в кэше были реальные
                # пары на эту неделю, а сейчас внезапно пусто — это больше похоже на
                # сбой запроса, чем на то, что все пары одновременно исчезли, так
                # что не затираем последние хорошие данные пустотой.
                if isinstance(modeus_data, Exception) or not isinstance(modeus_data, dict):
                    print(f"refresh_schedule_cache: Modeus ошибка ({key}): {type(modeus_data).__name__}: {modeus_data}")
                    modeus_data = None
                elif not any(modeus_data.values()) and any((prev.get("data") or {}).values()):
                    print(f"refresh_schedule_cache: Modeus вернул пусто для {key} при непустом прежнем кэше — похоже на сбой, оставляем прежние данные")
                    modeus_data = None

                if isinstance(netology_data, Exception) or not isinstance(netology_data, dict):
                    print(f"refresh_schedule_cache: Нетология ошибка ({key}): {type(netology_data).__name__}: {netology_data}")
                    netology_data = None
                elif not any(netology_data.values()) and any((prev.get("netology") or {}).values()):
                    print(f"refresh_schedule_cache: Нетология вернула пусто для {key} при непустом прежнем кэше — оставляем прежние данные")
                    netology_data = None

                # Резерв — yetanothercalendar.ru, только если И Modeus, И Нетология
                # сами по себе не смогли отдать день (а не просто "пар нет" —
                # modeus_data/netology_data уже None именно на этом основании выше).
                # Раньше yetanothercalendar использовался только для сверки текста
                # наставника и ссылок на вебинары, настоящим резервом при сбое
                # основных источников не был — при полном отказе Modeus/Нетологии
                # расписание оставалось прежним замороженным кэшем без изменений.
                if (modeus_data is None or netology_data is None) and week_start == this_week:
                    try:
                        from parsers.yetanothercalendar import fetch_week_events, to_day_schedule
                        yac_result = await asyncio.wait_for(fetch_week_events(), timeout=40)
                    except Exception as ye:
                        print(f"refresh_schedule_cache: yetanothercalendar резерв не удался: {ye!r}")
                        yac_result = None
                    if yac_result:
                        yac_modeus, yac_netology = {}, {}
                        for i in range(7):
                            day = week_start + datetime.timedelta(days=i)
                            day_key = day.isoformat()
                            day_events = to_day_schedule(yac_result, day)
                            yac_modeus[day_key] = [e for e in day_events if e.get("source") != "netology"]
                            yac_netology[day_key] = [e for e in day_events if e.get("source") == "netology"]
                        # Только если yetanothercalendar реально что-то нашёл на неделю —
                        # иначе он тоже мог не открыться (протухшая сессия и т.п.),
                        # и лучше остаться на прежнем кэше, чем затереть его пустотой.
                        if modeus_data is None and any(yac_modeus.values()):
                            modeus_data = yac_modeus
                            print(f"refresh_schedule_cache: Modeus восстановлен через yetanothercalendar ({key})")
                        if netology_data is None and any(yac_netology.values()):
                            netology_data = yac_netology
                            print(f"refresh_schedule_cache: Нетология восстановлена через yetanothercalendar ({key})")

                if modeus_data is None and netology_data is None:
                    continue  # ни одна часть реально не обновилась — нечего сохранять

                cache[key] = {
                    "cached_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                    "data": modeus_data if modeus_data is not None else prev.get("data", {}),
                    "netology": netology_data if netology_data is not None else prev.get("netology", {}),
                }
                _save_schedule_cache(cache)
        print("refresh_schedule_cache: обновлено")
    except Exception as e:
        print(f"refresh_schedule_cache error: {e!r}")


async def _fetch_schedule_fresh_or_cache() -> list:
    """Загружаем расписание на сегодня — сначала свежее, при ошибке кэш."""
    try:
        from parsers.modeus import get_cached_jwt, get_person_id_from_jwt, get_schedule, _get_week_start, _load_schedule_cache, _save_schedule_cache, get_week_schedule
        import asyncio as _asyncio

        today = datetime.datetime.now(tz=UFA_TZ).date()
        week_start = today - datetime.timedelta(days=today.weekday())

        # Читаем текущий кэш недели — НЕ стираем его (раньше весь кэш недели
        # удалялся здесь в начале и никогда не восстанавливался, эта функция
        # его только читала и выкидывала результат: другие читатели той же
        # недели, например _fetch_tomorrow_schedule, получали пустоту до
        # следующего фонового refresh_schedule_cache_silent, раз в 4ч).
        cache = _load_schedule_cache()
        entry = cache.get(week_start.isoformat(), {})
        prev_netology = entry.get("netology", {})

        jwt_token = await _asyncio.wait_for(get_cached_jwt(), timeout=25)
        person_id = get_person_id_from_jwt(jwt_token) if jwt_token else None
        if not person_id:
            raise Exception("нет person_id")

        modeus_schedule = await _asyncio.wait_for(get_schedule(jwt_token, person_id, today), timeout=25)
        print(f"Modeus: свежее расписание на сегодня — {len(modeus_schedule)} занятий")

        # Добавляем Нетологию
        try:
            from parsers.netology import fetch_netology_schedule_week
            netology_week = await _asyncio.wait_for(
                fetch_netology_schedule_week(week_start), timeout=20
            )
            netology_today = netology_week.get(today.isoformat(), []) if isinstance(netology_week, dict) else []
            print(f"Нетология: занятий на сегодня — {len(netology_today)}")
        except Exception as ne:
            netology_today = prev_netology.get(today.isoformat(), [])
            print(f"Нетология today error: {ne!r} — используем предыдущий кэш ({len(netology_today)})")

        # Обновляем в кэше только СЕГОДНЯШНИЙ день недели — остальные дни не
        # трогаем, чтобы другие читатели (например завтрашний день из
        # _fetch_tomorrow_schedule) не теряли данные из-за этого вызова.
        from data_lock import file_lock
        from parsers.modeus import SCHEDULE_CACHE_FILE
        with file_lock(SCHEDULE_CACHE_FILE):
            cache = _load_schedule_cache()
            entry = cache.get(week_start.isoformat(), {})
            modeus_data = dict(entry.get("data", {}))
            modeus_data[today.isoformat()] = modeus_schedule
            netology_data = dict(entry.get("netology", prev_netology))
            netology_data[today.isoformat()] = netology_today
            cache[week_start.isoformat()] = {
                "cached_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                "data": modeus_data,
                "netology": netology_data,
            }
            _save_schedule_cache(cache)

        # ВАЖНО: Modeus и Нетология — это ДВА РАЗНЫХ РЕАЛЬНЫХ источника пар,
        # даже если у записей совпадает время и похожи названия курса —
        # никогда не дедуплицировать между ними (было ошибочно "исправлено"
        # 2026-09-17 как "дубли", пользователь подтвердил, что это две
        # разные реальные пары — откатано в тот же день).
        combined = sorted(modeus_schedule + netology_today, key=lambda x: x.get("start_time", ""))
        return combined

    except Exception as e:
        print(f"Modeus: свежая загрузка не удалась ({e}), берём кэш...")
        try:
            from parsers.modeus import fetch_schedule_today
            return await asyncio.wait_for(fetch_schedule_today(), timeout=15)
        except Exception as e2:
            print(f"Modeus: кэш тоже не удался: {e2}")
            return []


# ─── Синхронизация задач ─────────────────────────────────────────────

AUTH_FAILURE_ALERTS_FILE = "data/auth_failure_alerts.json"
AUTH_FAILURE_ALERT_COOLDOWN_HOURS = 6


async def _alert_auth_failure_once(bot, chat_id, service_label: str, key: str, detail: str):
    """Явная ошибка авторизации (не "данных просто нет") — раньше это тихо
    выглядело как "новых заданий нет" неделями, пока пароль/сессия не были
    восстановлены вручную (см. ModeusAuthError — тот же принцип, теперь и
    для Netology/LMS). Не чаще AUTH_FAILURE_ALERT_COOLDOWN_HOURS на сервис,
    чтобы не спамить на каждый часовой прогон sync_all_tasks."""
    if not bot or not chat_id:
        return
    try:
        with open(AUTH_FAILURE_ALERTS_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = datetime.datetime.now(tz=UFA_TZ)
    last = state.get(key)
    if last:
        hours = (now - datetime.datetime.fromisoformat(last)).total_seconds() / 3600
        if hours < AUTH_FAILURE_ALERT_COOLDOWN_HOURS:
            return
    await send_with_retry(
        bot, chat_id,
        f"⚠️ *{service_label}*: не получилось авторизоваться ({detail}). "
        f"Похоже на протухший пароль/сессию, а не на то, что заданий просто нет — "
        f"стоит проверить вручную.",
    )
    state[key] = now.isoformat()
    os.makedirs("data", exist_ok=True)
    with open(AUTH_FAILURE_ALERTS_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False)


async def sync_all_tasks(bot=None, chat_id=None):
    """Синхронизируем LMS и Нетологию."""
    from storage import is_quiz_active
    if is_quiz_active():
        print("sync_all_tasks: пропуск — активен квиз")
        return
    _tasks_lock = None
    try:
        from parsers.lms import fetch_lms_deadlines, LMSAuthError
        from parsers.netology import fetch_netology_deadlines, NetologyAuthError

        lms_result, netology_result = await asyncio.gather(
            fetch_lms_deadlines(),
            fetch_netology_deadlines(),
            return_exceptions=True
        )

        if isinstance(lms_result, LMSAuthError):
            print(f"sync_all_tasks: LMS auth error: {lms_result!r}")
            await _alert_auth_failure_once(bot, chat_id, "LMS", "lms", str(lms_result))
        if isinstance(netology_result, NetologyAuthError):
            print(f"sync_all_tasks: Netology auth error: {netology_result!r}")
            await _alert_auth_failure_once(bot, chat_id, "Нетология", "netology", str(netology_result))

        existing_tasks = get_tasks()
        existing_ids = {t.get("id") for t in existing_tasks}
        existing_keys = {(t.get("title", ""), t.get("course_name", ""), t.get("deadline", "")) for t in existing_tasks}
        added = 0

        # LMS
        if isinstance(lms_result, tuple):
            lms_tasks, completed_ids = lms_result
            from storage import mark_lms_tasks_done
            marked = mark_lms_tasks_done(completed_ids, lms_tasks or [])
            if marked:
                print(f"sync_all_tasks: помечено выполненными {marked} LMS задач")
        else:
            lms_tasks = lms_result if isinstance(lms_result, list) else []
            completed_ids = set()

        # Нетология
        netology_tasks = []
        netology_completed_ids = set()
        if isinstance(netology_result, tuple):
            netology_tasks, _, netology_completed_ids = netology_result
        elif isinstance(netology_result, list):
            netology_tasks = netology_result
        if netology_completed_ids:
            from storage import mark_netology_tasks_done
            marked = mark_netology_tasks_done(netology_completed_ids)
            if marked:
                print(f"sync_all_tasks: помечено выполненными {marked} Netology задач")

        # Перезагружаем после mark_lms_tasks_done — он мог изменить файл.
        # Держим file_lock на весь остаток функции (до save_tasks ниже) —
        # без этого узкое окно между чтением и записью могло бы затереть
        # параллельную отметку "выполнено" (mark_task_done из бота,
        # mark_lms_tasks_done из другого прогона). __enter__/__exit__
        # вручную, а не "with", чтобы не переотступать весь блок ниже.
        _tasks_lock = file_lock(TASKS_FILE)
        _tasks_lock.__enter__()
        existing_tasks = get_tasks()
        existing_ids = {t.get("id") for t in existing_tasks}

        import os as _os3
        notified_file = "data/notified_tasks.json"
        try:
            notified = json.load(open(notified_file)) if _os3.path.exists(notified_file) else []
        except Exception:
            notified = []
        notified_changed = False

        updated = 0
        for t in (lms_tasks or []) + (netology_tasks or []):
            task_id = t.get("id")
            notif_key = str(task_id) if task_id else f"{t.get('title','')}_{t.get('course_name','')}"

            # Если задача уже есть — обновляем дедлайн если изменился
            found = False
            for existing in existing_tasks:
                if str(existing.get("id")) == str(task_id):
                    found = True
                    if existing.get("deadline") != t.get("deadline") and t.get("deadline"):
                        existing["deadline"] = t["deadline"]
                        updated += 1
                        print(f"Scheduler: обновлён дедлайн: {t.get('title','')[:40]}")
                    break
            if not found:
                key = (t.get("title", ""), t.get("course_name", ""))
                # Ищем совпадение по названию+курсу среди активных (не done) задач
                existing_key_pairs = {
                    (e.get("title",""), e.get("course_name",""))
                    for e in existing_tasks
                    if not e.get("done")
                }
                if key not in existing_key_pairs:
                    existing_tasks.append(t)
                    existing_ids.add(task_id)
                    added += 1
                    _log_task_added_recent(t)

            # Уведомление — отдельно от добавления, по notified_key
            # Push-уведомления бота отключены (мессенджер и т.п.) — синхронизация остаётся тихой.
            # Оставлено закомментированным, чтобы легко вернуть при желании
            # (как и остальные отключённые джобы в setup_scheduler ниже).
            # if bot and chat_id and t.get("source") in ("lms", "netology"):
            #     if notif_key not in notified:
            #         # Тихий старт — первые 20 минут только помечаем, не шлём
            #         grace_file = "data/startup_grace.json"
            #         in_grace = False
            #         try:
            #             if _os3.path.exists(grace_file):
            #                 import time as _time
            #                 grace_data = json.load(open(grace_file))
            #                 if _time.time() - grace_data.get("started_at", 0) < 1200:
            #                     in_grace = True
            #         except Exception:
            #             pass
            #         notified.append(notif_key)
            #         notified_changed = True
            #         if in_grace:
            #             continue
            #         from bot.messages import _esc_md
            #         source_name = "LMS" if t.get("source") == "lms" else "Нетология"
            #         title = _esc_md(t.get("title", "Без названия"))
            #         course = _esc_md(t.get("course_name", ""))
            #         deadline = t.get("deadline", "")
            #         deadline_str = ""
            #         if deadline:
            #             try:
            #                 dt = datetime.datetime.fromisoformat(deadline).astimezone(UFA_TZ)
            #                 deadline_str = f"\n📅 Дедлайн: {dt.strftime('%d.%m.%Y %H:%M')}"
            #             except Exception:
            #                 pass
            #         text = (
            #             f"💬 *{source_name}*\n"
            #             "\n"
            #             "💬 Новая задача\n"
            #             "────────────────────\n"
            #             f"📌 {title}\n"
            #             f"📚 {course}"
            #             f"{deadline_str}"
            #         )
            #         try:
            #             await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            #         except Exception as ex:
            #             print(f"sync_all_tasks: не удалось отправить уведомление: {ex}")

        if updated:
            print(f"Scheduler: обновлено дедлайнов: {updated}")

        save_tasks(existing_tasks)
        if added:
            print(f"Scheduler: добавлено {added} новых задач")

        # notified сохраняем ПОСЛЕ save_tasks — чтобы при падении задачи не потерялись
        if notified_changed:
            try:
                with open(notified_file, "w") as _f:
                    json.dump(notified[-500:], _f, ensure_ascii=False)
                print(f"sync_all_tasks: сохранено {len(notified)} уведомлённых задач")
            except Exception as e:
                print(f"sync_all_tasks: ошибка сохранения notified: {e!r}")

    except Exception as e:
        print(f"Scheduler sync error: {e!r}")
    finally:
        # Гарантируем разблокировку даже при исключении внутри защищённого
        # участка — иначе flock так и останется висеть до перезапуска бота,
        # и все последующие мутации tasks.json (mark_task_done и т.п.) встанут.
        if _tasks_lock is not None:
            try:
                _tasks_lock.__exit__(None, None, None)
            except Exception:
                pass


# ─── Утренний брифинг 9:00 ───────────────────────────────────────────


async def _get_study_analysis_short() -> str:
    """Короткий анализ учёбы для сводок (5-6 предложений)."""
    try:
        from parsers.study_analysis import fetch_study_analysis
        from grok import ask_grok
        raw = await fetch_study_analysis()
        prompt = (
            f"Данные успеваемости студента {USER_NAME}:\n{raw}\n\n"
            f"Напиши короткий анализ — ровно 5-6 предложений. "
            f"Укажи лучший и худший предмет по баллам, "
            f"общую тенденцию и один конкретный совет. "
            f"Без воды, по-русски, без скобок."
        )
        result = await ask_grok(prompt, system="Ты академический аналитик. Отвечай кратко — строго 5-6 предложений.")
        return result or ""
    except Exception as e:
        print(f"Study analysis short error: {e!r}")
        return ""

# ─── Дневной брифинг 14:00 ───────────────────────────────────────────



async def _fetch_tomorrow_schedule() -> list:
    """Расписание на завтра — Modeus + Нетология. Сначала кэш (его каждые 4ч
    наполняет refresh_schedule_cache_silent) — живой запрос только при
    промахе. Раньше Нетология ВСЕГДА бралась живым запросом, даже когда
    Modeus брался из кэша — лишний повторный логин в Netology внутри той же
    самой функции брифинга (после sync_all_tasks() и schedule-fetch чуть выше)
    регулярно упирался в таймаут 20с (в логах было видно как пустое
    "netology error: " — это asyncio.TimeoutError, у которого str() пустая
    строка) и завтрашние вебинары просто пропадали из сообщения."""
    try:
        from parsers.modeus import _load_schedule_cache, _save_schedule_cache, get_week_schedule, SCHEDULE_CACHE_FILE
        from parsers.netology import fetch_netology_schedule_week
        from data_lock import file_lock
        tomorrow = (datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(days=1)).date()
        week_start = tomorrow - datetime.timedelta(days=tomorrow.weekday())
        tomorrow_key = tomorrow.isoformat()

        cache = _load_schedule_cache()
        entry = cache.get(week_start.isoformat()) or {}

        # Modeus — кэш есть, только если ИМЕННО завтрашний день реально в нём
        # присутствует (раньше проверяли только наличие ключа "data" у недели —
        # пустой список для конкретного дня ошибочно считался "есть в кэше").
        if tomorrow_key in entry.get("data", {}):
            modeus_tomorrow = entry["data"][tomorrow_key]
        else:
            print("_fetch_tomorrow_schedule: кэша Modeus нет, запрашиваем...")
            week_data = await asyncio.wait_for(get_week_schedule(week_start), timeout=25)
            modeus_tomorrow = week_data.get(tomorrow_key, [])

        # Нетология — та же логика. При удачном живом запросе сохраняем
        # результат в кэш (раньше не сохраняли — следующий читатель снова
        # бил Netology живым запросом вместо использования уже полученных
        # данных).
        if tomorrow_key in entry.get("netology", {}):
            netology_tomorrow = entry["netology"][tomorrow_key]
        else:
            netology_tomorrow = []
            try:
                netology_week = await asyncio.wait_for(
                    fetch_netology_schedule_week(week_start), timeout=20
                )
                netology_tomorrow = netology_week.get(tomorrow_key, [])
                with file_lock(SCHEDULE_CACHE_FILE):
                    cache2 = _load_schedule_cache()
                    entry2 = cache2.get(week_start.isoformat(), {})
                    netology_data = dict(entry2.get("netology", {}))
                    netology_data[tomorrow_key] = netology_tomorrow
                    cache2[week_start.isoformat()] = {
                        "cached_at": entry2.get("cached_at") or datetime.datetime.now(tz=datetime.UTC).isoformat(),
                        "data": entry2.get("data", {}),
                        "netology": netology_data,
                    }
                    _save_schedule_cache(cache2)
            except Exception as ne:
                print(f"_fetch_tomorrow_schedule netology error: {ne!r}")

        # ВАЖНО: Modeus и Нетология — разные реальные источники пар, не дедуплицировать
        # (см. комментарий в _fetch_schedule_fresh_or_cache выше).
        combined = sorted(modeus_tomorrow + netology_tomorrow, key=lambda x: x.get("start_time", ""))
        return combined
    except Exception as e:
        print(f"_fetch_tomorrow_schedule error: {e!r}")
        return []


# ─── Проверка дедлайнов ───────────────────────────────────────────────

def _load_sent_deadlines() -> dict:
    try:
        with open(DEADLINE_SENT_FILE) as f:
            data = json.load(f)
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        if data.get("date") != today:
            return {"date": today, "sent": []}
        return data
    except Exception:
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return {"date": today, "sent": []}


def _save_sent_deadlines(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(DEADLINE_SENT_FILE, "w") as f:
        json.dump(data, f)


async def check_deadline_reminders(bot, chat_id: int):
    try:
        from bot.messages import deadline_reminder
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        sent_data = _load_sent_deadlines()
        sent = sent_data["sent"]
        changed = False
        for task in tasks:
            try:
                if not task.get("deadline"):
                    continue
                deadline = datetime.datetime.fromisoformat(task["deadline"])
                days_left = (deadline - now).days
                if days_left not in [7, 3, 1]:
                    continue
                key = f"{task.get('id')}_{days_left}"
                if key in sent:
                    continue
                text = deadline_reminder(task, days_left)
                await send_with_retry(bot, chat_id, text)
                sent.append(key)
                changed = True
            except Exception:
                continue
        if changed:
            # _load_sent_deadlines уже сбрасывает "sent" в пустой список при смене
            # даты — отдельная по-возрастная чистка здесь была не нужна (и была
            # сломана: `if not k.split("_")[0].isdigit() or True` — `or True` делает
            # условие всегда истинным, ничего не отфильтровывает). Оставляем только
            # ограничение размера на случай очень длинного дня.
            if len(sent_data["sent"]) > 500:
                sent_data["sent"] = sent_data["sent"][-500:]
            _save_sent_deadlines(sent_data)
    except Exception as e:
        print(f"Scheduler deadline reminders error: {e!r}")


# ─── Оценки ──────────────────────────────────────────────────────────

async def check_grades_and_notify(bot, chat_id: int):
    from storage import is_quiz_active
    if is_quiz_active():
        print("check_grades: пропуск — активен квиз")
        return
    try:
        from parsers.lms import fetch_lms_deadlines
        from parsers.modeus_grades import fetch_modeus_grades
        from bot.messages import format_grade_notification_new, new_grade_message

        try:
            lms_result = await fetch_lms_deadlines()
            if isinstance(lms_result, tuple):
                lms_tasks, completed_ids = lms_result
            else:
                lms_tasks, completed_ids = [], set()
            from storage import mark_lms_tasks_done
            marked = mark_lms_tasks_done(completed_ids, lms_tasks)
            if marked:
                print(f"Scheduler grades: помечено выполненными из LMS: {marked}")
        except Exception as e:
            print(f"Scheduler grades LMS error: {e!r}")

        # Тихий старт — первые 20 минут только помечаем, не шлём
        import time as _time2, json as _json2, os as _os2
        _in_grace = False
        try:
            if _os2.path.exists("data/startup_grace.json"):
                _grace = _json2.load(open("data/startup_grace.json"))
                if _time2.time() - _grace.get("started_at", 0) < 1200:
                    _in_grace = True
        except Exception:
            pass

        modeus_grades = await _retry(fetch_modeus_grades) or []
        last_seen = None
        for grade in modeus_grades:
            grade_key = f"modeus_grade:{grade.get('course','')[:30]}:{grade.get('value','')}:{(grade.get('lesson_date') or '')[:10]}:{(grade.get('id') or '')[-8:]}"
            if _is_notification_sent(grade_key):
                continue
            if _in_grace:
                # В grace period только обновляем seen — НЕ помечаем как отправленное
                if "_seen" in grade:
                    last_seen = grade["_seen"]
                continue
            text = grade.get("_text") or format_grade_notification_new(grade)
            sent_ok = await send_with_retry(bot, chat_id, text)
            if sent_ok:
                _mark_notification_sent(grade_key)
                _log_grade_recent(grade, "modeus")
                if "_seen" in grade:
                    from parsers.modeus_grades import _save_seen
                    _save_seen(grade["_seen"])

    except Exception as e:
        print(f"Scheduler grades check error: {e!r}")


# ─── Почта и мессенджер ───────────────────────────────────────────────

async def check_mail_and_notify(bot, chat_id: int):
    try:
        from parsers.mail import fetch_new_emails
        from bot.messages import new_email_message
        from bot.keyboards import task_from_message_keyboard
        from storage import add_seen_message
        emails = await fetch_new_emails()
        for email_data in emails:
            # Тело письма — как есть, без AI-переформулирования и без обрезки
            # (раньше здесь была beautify_message: сокращала до ~300 симв.).
            text = new_email_message(email_data)
            keyboard = task_from_message_keyboard(email_data["id"])
            sent = await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)
            # Помечаем виденным сразу — чтобы не накапливать дубли в очереди
            add_seen_message(email_data["id"])
            _log_mail_recent(email_data)

    except Exception as e:
        print(f"Scheduler mail check error: {e!r}")


async def check_netology_notifications_and_notify(bot, chat_id: int):
    try:
        from parsers.netology import fetch_netology_notifications
        from bot.messages import new_netology_notification_message
        from bot.keyboards import task_from_message_keyboard
        from storage import is_seen, add_seen_message
        notifications = await fetch_netology_notifications()
        for notif in notifications:
            if is_seen(notif["id"]):
                continue
            text = new_netology_notification_message(notif)
            keyboard = task_from_message_keyboard(notif["id"])
            sent = await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)
            add_seen_message(notif["id"])
            _log_netology_notif_recent(notif)
    except Exception as e:
        print(f"Netology notifications check error: {e!r}")


async def check_gmail_and_notify(bot, chat_id: int):
    try:
        from parsers.mail import fetch_new_gmail_emails
        from bot.messages import new_email_message
        from bot.keyboards import task_from_message_keyboard
        from storage import add_seen_message
        emails = await fetch_new_gmail_emails()
        for email_data in emails:
            # Тело письма — как есть, без AI-переформулирования и без обрезки.
            text = new_email_message(email_data)
            keyboard = task_from_message_keyboard(email_data["id"])
            sent = await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)
            add_seen_message(email_data["id"])
            _log_mail_recent(email_data)
    except Exception as e:
        print(f"Scheduler Gmail check error: {e!r}")


def _log_recent(file: str, entry: dict, hours: int = 48, keep_last: int = 30):
    """Короткая запись о входящем сообщении (почта/мессенджер/ВК) — наставник
    читает это в своих чек-инах, чтобы знать, что приходило, даже если сам
    форвард в Telegram давно проскроллен и не был прочитан вовремя.

    file_lock — некоторые из этих файлов пишутся более чем одним джобом
    (grades_recent.json — и check_grades_and_notify, и check_lms_grades_and_notify;
    tasks_added_recent.json — sync_all_tasks запускается и по интервалу, и из
    брифингов, и отдельной таской на старте), так что конкурентный
    read-modify-write без лока мог потерять чужую запись."""
    with file_lock(file):
        try:
            with open(file) as f:
                data = json.load(f)
        except Exception:
            data = []
        entry = dict(entry)
        entry["at"] = datetime.datetime.now(tz=UFA_TZ).isoformat()
        data.append(entry)
        cutoff = datetime.datetime.now(tz=UFA_TZ) - datetime.timedelta(hours=hours)
        data = [d for d in data if _safe_parse_iso(d.get("at")) and _safe_parse_iso(d["at"]) > cutoff][-keep_last:]
        atomic_write_json(file, data)


def _log_mail_recent(email_data: dict):
    # Раньше здесь не было тела письма вообще — send_noon_review видел только
    # тему и не мог оценить, есть ли в письме что-то реально важное (2026-09-17,
    # найдено после того, что разбор дня писал общими словами про "письма
    # такие-то", не разобрав их содержание).
    _log_recent("data/mail_recent.json", {
        "subject": email_data.get("subject", ""),
        "sender": email_data.get("sender", ""),
        "date": email_data.get("date", ""),
        "body": (email_data.get("body") or "")[:800],
    })
    # Долговременный журнал (agent_db): полное тело, без скользящего окна.
    from agent_db import record_event
    record_event("mail", "mail",
                 f"{email_data.get('sender', '')}|{email_data.get('date', '')}|{email_data.get('subject', '')}",
                 title=email_data.get("subject", ""), body=email_data.get("body") or "",
                 sender=email_data.get("sender", ""), occurred_at=email_data.get("date") or None)


def _log_netology_notif_recent(notif: dict):
    _log_recent("data/netology_notif_recent.json", {
        "title": notif.get("title", ""),
        "program": notif.get("program_title", ""),
        "text": (notif.get("text") or "")[:500],
    })
    from agent_db import record_event
    record_event("netology_notif", "notification", str(notif.get("id") or "") or None,
                 title=notif.get("title", ""), body=notif.get("text") or "",
                 course=notif.get("program_title", ""))


def _log_messenger_recent(sender: str, text: str):
    # keep_last выше дефолта — ночной полный обход (messenger_nightly_full_sweep)
    # может залогировать за раз десятки сообщений сразу по всем чатам.
    _log_recent("data/messenger_recent.json", {
        "sender": sender,
        "text": (text or "")[:500],
    }, keep_last=150)
    from agent_db import record_event
    record_event("messenger", "message", None, sender=sender, body=text or "")


def _log_vk_recent(chat_label: str, text: str):
    _log_recent("data/vk_recent.json", {
        "chat_label": chat_label,
        "text": (text or "")[:500],
    })
    from agent_db import record_event
    record_event("vk", "message", None, sender=chat_label, body=text or "")


def _log_grade_recent(grade: dict, source: str):
    """Оценки, реально отправленные пользователю (Modeus/LMS) — читает
    send_noon_review, чтобы включить в разбор дня."""
    entry = {
        "source": source,
        "course": grade.get("course_name") or grade.get("course", ""),
        "title": grade.get("subject_name") or grade.get("title", ""),
        "value": grade.get("value") or grade.get("grade", ""),
        "old_value": grade.get("old_value") or grade.get("old_grade", ""),
    }
    _log_recent("data/grades_recent.json", entry)
    from agent_db import record_event
    record_event("grade", "grade", f"{source}|{entry['course']}|{entry['title']}",
                 title=entry["title"], course=entry["course"],
                 body=f"оценка {entry['value']}" + (f" (было {entry['old_value']})" if entry["old_value"] else ""),
                 meta={"value": entry["value"], "old_value": entry["old_value"], "system": source})


def _log_task_added_recent(task: dict):
    """Новые задачи, добавленные sync_all_tasks (уведомления о них в Telegram
    сейчас отключены — см. комментарий выше) — читает send_noon_review."""
    _log_recent("data/tasks_added_recent.json", {
        "title": task.get("title", ""),
        "course": task.get("course_name", ""),
        "deadline": task.get("deadline", ""),
        "source": task.get("source", ""),
    })
    from agent_db import record_event
    record_event(task.get("source") or "task", "task_new", str(task.get("id") or "") or None,
                 title=task.get("title", ""), course=task.get("course_name", ""), url=task.get("url", ""),
                 effective_at=task.get("deadline") or None)


def _safe_parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


async def check_messenger_and_notify(bot, chat_id: int):
    if _playwright_lock.locked():
        print("Messenger: Playwright занят — пропускаем")
        return
    async with _playwright_lock:
        await _check_messenger_and_notify_inner(bot, chat_id)

async def _check_messenger_and_notify_inner(bot, chat_id: int):
    try:
        from parsers.messenger import fetch_new_messages
        from bot.messages import new_messenger_message
        from bot.keyboards import task_from_message_keyboard
        from storage import add_seen_message
        import time as _tm, json as _jm, os as _om
        _in_grace_msg = False
        try:
            if _om.path.exists("data/startup_grace.json"):
                _gm = _jm.load(open("data/startup_grace.json"))
                if _tm.time() - _gm.get("started_at", 0) < 1200:
                    _in_grace_msg = True
        except Exception:
            pass
        messages = await fetch_new_messages()
        if _in_grace_msg:
            from storage import add_seen_message as _asm
            for msg in messages:
                _asm(msg["id"])
            return
        for msg in messages:
            # Текст сообщения — как есть, без AI-переформулирования и без обрезки.
            text = new_messenger_message(msg)
            keyboard = task_from_message_keyboard(msg["id"])
            sent = await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)
            # Помечаем виденным сразу — чтобы не накапливать дубли в очереди
            add_seen_message(msg["id"])
            _log_messenger_recent(msg.get("sender", ""), msg.get("text") or msg.get("preview", ""))

    except Exception as e:
        print(f"Scheduler messenger check error: {e!r}")


async def messenger_nightly_full_sweep(bot=None, chat_id=None):
    """Раз в сутки (00:00) читаем ВСЕ чаты Мессенджера целиком — не только
    превью непрочитанных, как в check_messenger_and_notify. Заходим внутрь
    каждого чата (и уже прочитанного, и ещё нет), поэтому непрочитанные могут
    пометиться прочитанными у отправителя — сознательный компромисс ради
    того, чтобы наставник видел полный текст (домашки, важные детали по
    учёбе), а не только обрезанное превью. В Telegram НИЧЕГО не форвардим —
    только логируем для наставника (data/messenger_recent.json)."""
    if _playwright_lock.locked():
        print("Messenger (ночной обход): Playwright занят — пропускаем")
        return
    async with _playwright_lock:
        try:
            from parsers.messenger import fetch_full_day_sweep
            from storage import add_seen_message
            messages = await fetch_full_day_sweep()
            for msg in messages:
                add_seen_message(msg["id"])
                _log_messenger_recent(msg.get("sender", ""), msg.get("text", ""))
            print(f"Messenger (ночной обход): залогировано {len(messages)} сообщени(й)")
        except Exception as e:
            print(f"Messenger nightly sweep error: {e!r}")


# ─── Напоминания пользователя ─────────────────────────────────────────

async def check_user_reminders(bot, chat_id: int):
    """Проверяем пользовательские напоминания каждые 5 минут."""
    try:
        now = datetime.datetime.now(tz=UFA_TZ)
        now_h = now.hour
        if _in_quiet_hours(now_h):
            # Тихие часы (22:00-08:00) — пропускаем без изменений
            # mark_sent() нельзя: уменьшает times_left и ставит next_at=сейчас+interval
            return

        from reminders import get_due_reminders, mark_sent, delete_reminder
        from storage import get_tasks
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        due = get_due_reminders()
        tasks = get_tasks()
        tasks_by_id = {str(t["id"]): t for t in tasks}

        for r in due:
            task_id = str(r.get("task_id", ""))
            task_obj = tasks_by_id.get(task_id)

            # П.5 — если задача выполнена или удалена — удаляем напоминание
            if task_id and not task_obj:
                delete_reminder(r["id"])
                print(f"Reminder: задача не найдена, удаляем напоминание {r['id']}")
                continue
            if task_obj and task_obj.get("done"):
                delete_reminder(r["id"])
                print(f"Reminder: задача выполнена, удаляем напоминание {r['id']}")
                continue

            # П.1 — дедлайн в тексте
            deadline_line = ""
            _is_daily = r.get("times_left", 0) >= 9000 or task_obj and task_obj.get("source") == "reminder_only"
            if task_obj and task_obj.get("deadline") and not _is_daily:
                try:
                    dl = datetime.datetime.fromisoformat(task_obj["deadline"]).astimezone(UFA_TZ)
                    days_left = (dl.date() - now.date()).days
                    if days_left == 0:
                        deadline_line = "\n📅 Дедлайн: _сегодня!_"
                    elif days_left == 1:
                        deadline_line = "\n📅 Дедлайн: _завтра_"
                    elif days_left > 0:
                        deadline_line = f"\n📅 Дедлайн: _через {days_left} дн. ({dl.strftime('%d.%m')})_"
                    else:
                        deadline_line = f"\n📅 Дедлайн: _просрочен ({dl.strftime('%d.%m')})_"
                except Exception:
                    pass

            # П.3 — время следующего напоминания
            times_left_after = r["times_left"] - 1
            next_line = ""
            if times_left_after > 0 and r.get("interval_minutes", 0) > 0:
                next_dt = now + datetime.timedelta(minutes=r["interval_minutes"])
                from reminders import format_times_left
                next_line = f"\n⏭ {next_dt.strftime('%d.%m %H:%M')} ({format_times_left(times_left_after)})"
            elif times_left_after == 0:
                next_line = "\n_Больше не напомню — но останется в списке, пока не закроешь_"

            from bot.messages import _esc_md
            msg_text = (
                f"🔔 *Напоминание*\n\n"
                f"📌 {_esc_md(r['task_title'])}"
                f"{deadline_line}"
                f"{next_line}"
            )

            # П.2 — кнопки в уведомлении
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Сделано", callback_data=f"rem_done:{task_id}:{r['id']}"),
                    InlineKeyboardButton("🗑 Не напоминать", callback_data=f"rem_delete:{r['id']}"),
                ],
                [
                    InlineKeyboardButton("⏰ Через 15 мин", callback_data=f"rem_snooze:{r['id']}:15"),
                    InlineKeyboardButton("⏭ Через 1 час", callback_data=f"rem_snooze:{r['id']}:60"),
                ],
            ])

            try:
                # Удаляем предыдущее сообщение этого напоминания
                prev_msg_id = r.get("last_message_id")
                if prev_msg_id:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=prev_msg_id)
                    except Exception:
                        pass

                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=msg_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                _jarvis_write(f"Напоминание: {r['task_title']}")

                # Сохраняем message_id для удаления при следующем напоминании
                from reminders import save_last_message_id
                save_last_message_id(r["id"], sent.message_id)
            except Exception as e:
                print(f"Reminder send error: {e!r}")

            mark_sent(r["id"])

            # Звук + Mac-уведомление + Reminders
            try:
                import subprocess as _sp
                _title = r.get("task_title", "Напоминание")[:50]
                # Заголовок вводит сам пользователь — кавычки/бэкслеши ломали
                # AppleScript-строку (тихий сбой всей секции), экранируем.
                _safe_title = _title.replace("\\", "\\\\").replace('"', '\\"')
                _sp.Popen(["afplay", "/System/Library/Sounds/Funk.aiff"])

                def _run_osascript(script: str):
                    # osascript может зависнуть навсегда (например TCC-промпт на
                    # управление "Напоминаниями" ждёт клика, которого никто не
                    # сделает) — вызов синхронный, поэтому его нельзя делать
                    # напрямую в async-функции: он заблокирует ВЕСЬ event loop
                    # и остановит остальные джобы и ответы бота. Уносим в поток
                    # с жёстким таймаутом.
                    _sp.run(["osascript", "-e", script], timeout=10)

                await asyncio.to_thread(
                    _run_osascript,
                    f'display notification "{_safe_title}" with title "ДедЛайнер" sound name "Funk"',
                )
                await asyncio.to_thread(
                    _run_osascript,
                    f'tell application "Reminders" to delete (every reminder whose name is "{_safe_title}")',
                )
                await asyncio.to_thread(
                    _run_osascript,
                    f'tell application "Reminders" to make new reminder with properties {{name:"{_safe_title}", due date:current date}}',
                )
            except Exception:
                pass

            # После последнего срабатывания reminder_only-задача больше НЕ
            # удаляется: напоминание остаётся (exhausted) в боте и на столе,
            # пока пользователь сам его не закроет — см. reminders.mark_sent.

    except Exception as e:
        print(f"Scheduler user reminders error: {e!r}")


# ─── Рандомные мотивационные ─────────────────────────────────────────


# ─── Напоминание о паре ───────────────────────────────────────────────

def _load_sent_lesson_reminders() -> set:
    try:
        with open(LESSON_REMINDERS_FILE) as f:
            data = json.load(f)
            today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
            if data.get("date") != today:
                return set()
            return set(data.get("sent", []))
    except Exception:
        return set()


def _save_sent_lesson_reminders(sent: set):
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    os.makedirs("data", exist_ok=True)
    with open(LESSON_REMINDERS_FILE, "w") as f:
        json.dump({"date": today, "sent": list(sent)}, f)


LESSON_NOTICE_LEADS = (60, 15)      # за сколько минут до начала предупреждать
LESSON_NOTICE_CATCHUP_MIN = 10      # если бот проспал момент — догоняем в этом окне


def _lesson_lines_today() -> list:
    """Пары/вебинары на сегодня — РОВНО те же строки, что в окне наставника на
    столе (scripts/mentor_dashboard.build_schedule): Modeus + Нетология, две
    пары одной записью разбиты по слотам, ссылки на вебинары, VK-сводка если
    она на сегодня. Раньше здесь был отдельный живой запрос только в Modeus
    раз в 5 минут — вебинары Нетологии в уведомления не попадали вовсе."""
    import sys as _sys
    _scripts = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from mentor_dashboard import build_schedule
    today = datetime.datetime.now(tz=UFA_TZ).date()
    return [l for l in build_schedule(today)["lines"] if l.get("start")]


async def check_lesson_reminders(bot, chat_id: int):
    """Раз в 2 минуты: уведомление о паре за 60 и за 15 минут до начала —
    в Telegram и системным уведомлением macOS. Асинхронные LXP-пункты (без
    времени) не трогаем. Тихие часы игнорируем сознательно: уведомление
    имеет смысл только сейчас, а не утренней доставкой из очереди (пара в
    08:30 → предупреждение в 07:30)."""
    try:
        now = datetime.datetime.now(tz=UFA_TZ)
        lines = await asyncio.to_thread(_lesson_lines_today)
        if not lines:
            return
        sent = _load_sent_lesson_reminders()

        for lesson in lines:
            start_dt = lesson["start"]
            minutes_until = (start_dt - now).total_seconds() / 60
            for lead in LESSON_NOTICE_LEADS:
                if not (lead - LESSON_NOTICE_CATCHUP_MIN < minutes_until <= lead):
                    continue
                key = f"{start_dt.isoformat()}|{lesson['label']}|{lead}"
                if key in sent:
                    continue
                from html import escape as _h
                when = "через час" if lead == 60 else "через 15 минут"
                text = (
                    f"⏰ <b>Пара {when}</b> — в {start_dt.strftime('%H:%M')}\n"
                    f"📚 {_h(lesson['label'])}"
                )
                if lesson.get("url"):
                    text += f'\n🔗 <a href="{_h(lesson["url"])}">Ссылка на занятие</a>'
                try:
                    await send_with_retry(bot, chat_id, text, parse_mode="HTML",
                                          ignore_quiet_hours=True, disable_web_page_preview=True)
                except Exception as e:
                    print(f"Scheduler lesson reminder send error: {e!r}")
                    continue
                sent.add(key)
                _save_sent_lesson_reminders(sent)
                print(f"Scheduler: напоминание о паре ({lead} мин): {lesson['label'][:50]} в {start_dt.strftime('%H:%M')}")

                try:
                    import subprocess as _sp
                    _t = f"Пара {when} — {start_dt.strftime('%H:%M')}"
                    _b = lesson["label"][:120]
                    _t = _t.replace("\\", "\\\\").replace('"', '\\"')
                    _b = _b.replace("\\", "\\\\").replace('"', '\\"')
                    await asyncio.to_thread(
                        _sp.run,
                        ["osascript", "-e", f'display notification "{_b}" with title "{_t}" sound name "Glass"'],
                        timeout=10, capture_output=True,
                    )
                except Exception:
                    pass
    except Exception as e:
        print(f"Scheduler lesson reminder error: {e!r}")


def _generate_random_times() -> list[str]:
    """
    3-4 случайных времени: 11:00–13:00 и 15:00–22:00.
    Если попадает рядом с 9/14 — сдвиг +3-40 мин.
    Минимум 3 часа между рандомными.
    """
    import random
    windows = [(11 * 60, 13 * 60), (15 * 60, 22 * 60)]
    min_gap = 180  # 3 часа
    times = []
    last = 8 * 60

    for w_start, w_end in windows:
        current = max(w_start, last + min_gap)
        while current + 20 <= w_end and len(times) < 4:
            jitter = random.randint(0, 20)
            t = current + jitter
            if t < w_end:
                h, m = divmod(t, 60)
                times.append(f"{h:02d}:{m:02d}")
                last = t
            current += min_gap + random.randint(0, 20)

    return times


def _load_random_schedule() -> dict:
    try:
        with open(RANDOM_SCHEDULE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_random_schedule(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(RANDOM_SCHEDULE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _get_todays_random_times() -> list[str]:
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    schedule = _load_random_schedule()
    if schedule.get("date") != today:
        times = _generate_random_times()
        _save_random_schedule({"date": today, "times": times, "sent": []})
        print(f"Scheduler: рандомные напоминания: {times}")
        return times
    return schedule.get("times", [])


def _mark_random_sent(time_str: str):
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    schedule = _load_random_schedule()
    if schedule.get("date") == today:
        sent = schedule.get("sent", [])
        if time_str not in sent:
            sent.append(time_str)
        schedule["sent"] = sent
        _save_random_schedule(schedule)


def _get_todays_motivation_time() -> str:
    """Рандомное время около 12:00 ±30 мин — генерируется раз в день."""
    import random as _random
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    schedule = _load_random_schedule()
    if schedule.get("motivation_date") == today and schedule.get("motivation_time"):
        return schedule["motivation_time"]
    # Генерируем: 11:30 — 12:30
    total_minutes = 11 * 60 + 30 + _random.randint(0, 60)
    h = total_minutes // 60
    m = total_minutes % 60
    t = f"{h:02d}:{m:02d}"
    schedule["motivation_date"] = today
    schedule["motivation_time"] = t
    _save_random_schedule(schedule)
    print(f"Scheduler: мотивация сегодня в {t}")
    return t


async def check_random_reminder(bot, chat_id: int):
    try:
        now = datetime.datetime.now(tz=UFA_TZ)
        if now.hour < 11 or now.hour >= 13:
            return

        motivation_time = _get_todays_motivation_time()
        schedule = _load_random_schedule()
        today = now.date().isoformat()

        # Уже отправляли мотивацию сегодня?
        if schedule.get("motivation_sent_date") == today:
            return

        th, tm = map(int, motivation_time.split(":"))
        target = datetime.datetime(now.year, now.month, now.day, th, tm, tzinfo=UFA_TZ)
        if abs((now - target).total_seconds()) <= 300:
            await _send_random_motivation(bot, chat_id)
            schedule["motivation_sent_date"] = today
            _save_random_schedule(schedule)
            print(f"Scheduler: мотивация отправлена в {motivation_time}")
    except Exception as e:
        print(f"Scheduler random reminder error: {e!r}")


async def _send_random_motivation(bot, chat_id: int):
    try:
        from grok import ask_grok
        from bot.messages import _short_course, _format_date
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)

        # Находим самый срочный дедлайн
        urgent_task = None
        min_days = 9999
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if 0 <= days < min_days:
                    min_days = days
                    urgent_task = t
            except Exception:
                continue

        # Фраза от ИИ
        ai_line = ""
        if urgent_task:
            course = _short_course(urgent_task.get("course_name", ""))
            title = urgent_task.get("title", "")
            prompt = (
                f"Студент {USER_NAME}, самая срочная задача: {course} — {title}, через {min_days} дн. "
                f"Напиши одну короткую мотивирующую фразу про эту задачу. "
                f"Без предисловий, без скобок. Только сама фраза."
            )
            ai_line = await ask_grok(prompt)

        # Цитата из файла
        quote_text = _get_quote()
        quote_author = ""

        lines = ["⚡️ *Не расслабляйся*", ""]

        if ai_line:
            lines.append(f"{ai_line}")
            lines.append("")

        if urgent_task:
            from bot.messages import _esc_md
            course = _short_course(urgent_task.get("course_name", ""))
            title = _esc_md(urgent_task.get("title", ""))
            date = _format_date(urgent_task.get("deadline"))
            deadline_emoji = "🔴" if min_days == 0 else "🟡" if min_days <= 3 else "🟢"
            task_str = f"{course} — {title}" if course else title
            lines.append(f"{deadline_emoji} *{task_str}*")
            if date:
                lines.append(f"📅 _{date}_")
            lines.append("")

        if quote_text:
            lines.append(f"{'─' * 20}")
            lines.append(f"💬 {quote_text}")
            if quote_author:
                lines.append(f"— {quote_author}")

        await send_with_retry(bot, chat_id, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        print(f"Scheduler random motivation error: {e!r}")
async def send_weekly_report(bot, chat_id: int):
    try:
        from grok import ask_grok
        tasks = get_tasks()
        done = len([t for t in tasks if t.get("done") and t.get("source") != "reminder_only"])
        pending = len([t for t in tasks if not t.get("done") and t.get("source") != "reminder_only"])
        pct = int(done / max(done + pending, 1) * 100)
        week_summary = get_stats_summary()
        avg = get_weekly_done_avg()

        # Просроченные
        now = datetime.datetime.now(tz=UFA_TZ)
        overdue = [t for t in tasks if not t.get("done") and t.get("deadline") and
            (datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ) - now).days < 0]

        lines = [
            "📊 *Недельный отчёт*",
            "",
            f"✅ Выполнено: *{done}*",
            f"📋 Осталось: *{pending}*",
            f"📈 Выполнение: *{pct}%*",
            f"📉 Среднее в день: *{avg}*",
            "",
        ]
        if week_summary and week_summary != "нет данных":
            lines.append("*📅 По дням:*")
            lines.append(week_summary)
            lines.append("")
        if overdue:
            from bot.messages import _esc_md
            lines.append(f"⚠️ Просроченных задач: *{len(overdue)}*")
            for t in overdue[:3]:
                course = _esc_md(t.get("course_name", "")[:25])
                lines.append(f"  • {course} — {_esc_md(t.get('title','')[:40])}")
            lines.append("")

        await send_with_retry(bot, chat_id, "\n".join(lines))

        prompt = (
            f"Итог недели студента {USER_NAME}: выполнено {done} из {done+pending} задач ({pct}%). "
            f"Среднее в день: {avg}. Просроченных: {len(overdue)}. "
            f"Статистика по дням:\n{week_summary}\n\n"
            f"Напиши 3 предложения: оцени неделю честно, отметь тенденцию, дай конкретный совет на следующую. "
            f"Без воды, по-русски."
        )
        grok_text = await ask_grok(prompt)
        if grok_text:
            await send_with_retry(bot, chat_id, f"🤖 {grok_text}")

    except Exception as e:
        print(f"Scheduler weekly report error: {e!r}")


# ─── Главные задачи планировщика ─────────────────────────────────────


# ─── Настройка планировщика ───────────────────────────────────────────


# ─── Утренний брифинг 9:00 (новый) ───────────────────────────────────


async def _fetch_yandex_weather() -> str:
    """Погода через Яндекс Погоду — парсим текст страницы."""
    try:
        from playwright.async_api import async_playwright
        from config import COOKIES_MESSENGER_FILE, USER_CITY
        import json, os, re

        if not os.path.exists(COOKIES_MESSENGER_FILE):
            return ""
        with open(COOKIES_MESSENGER_FILE) as f:
            raw = json.load(f)
        cookies = raw.get("cookies", raw) if isinstance(raw, dict) else raw
        if not cookies:
            return ""

        city = (USER_CITY or "").strip().lower()
        _city_map = {
            "тюмень": "tyumen", "москва": "moscow", "санкт-петербург": "saint-petersburg",
            "екатеринбург": "yekaterinburg", "уфа": "ufa", "новосибирск": "novosibirsk",
            "казань": "kazan", "челябинск": "chelyabinsk", "омск": "omsk",
        }
        city_en = _city_map.get(city, city or "tyumen")
        url = f"https://yandex.ru/pogoda/ru/{city_en}"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            await context.add_cookies(cookies)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(4000)
            text = await page.inner_text("body")
            await browser.close()

        page_lines = [l.strip() for l in text.split("\n") if l.strip()]

        # Строка 35 (индекс 34): "Уфа, погода сейчас: облачно с прояснениями..."
        # Строка 38: температура (просто "+30°")
        # Строка 39: "Ощущается как +23°"
        # Строка 46: "4,4 м/с, З"
        # Строки 237-240: утром/днём/вечером/ночью

        now_desc = ""
        now_temp = ""
        feels = ""
        wind = ""
        morning_t = ""; morning_f = ""; morning_w = ""; morning_d = ""
        day_t = ""; day_f = ""; day_w = ""; day_d = ""
        evening_t = ""; evening_f = ""; evening_w = ""; evening_d = ""
        night_t = ""; night_f = ""; night_w = ""; night_d = ""

        for i, l in enumerate(page_lines):
            if "погода сейчас:" in l.lower() and not now_desc:
                m = re.search(r"погода сейчас:\s*(.+?)(?:\.|$)", l, re.IGNORECASE)
                if m:
                    now_desc = m.group(1).strip().rstrip(".")
            if re.match(r"^[+\-−]?\d+°$", l) and not now_temp:
                now_temp = l.replace("−", "-")
            if l.startswith("Ощущается как") and not feels:
                feels = l.replace("Ощущается как ", "").strip()
            if re.match(r"^\d", l) and "м/с" in l and not wind:
                wind = l
            # Прогноз по частям дня: "утром температура воздуха +20°, ощущается как +16°, облачно, скорость ветра 6 м/с, южный"
            _part_re = r"([+\-−]?\d+°)[^,]*, ощущается как ([+\-−]?\d+°), ([^,]+), скорость ветра ([\d,.]+)\s*м/с,?\s*([^,]*)"
            if l.startswith("утром температура") and not morning_t:
                m = re.search(_part_re, l)
                if m:
                    morning_t, morning_f, morning_d = m.group(1), m.group(2), m.group(3)
                    morning_w = f"{m.group(4)} м/с {m.group(5)}".strip().rstrip(",")
            if l.startswith("днём ") and not day_t:
                m = re.search(_part_re, l)
                if m:
                    day_t, day_f, day_d = m.group(1), m.group(2), m.group(3)
                    day_w = f"{m.group(4)} м/с {m.group(5)}".strip().rstrip(",")
            if l.startswith("вечером ") and not evening_t:
                m = re.search(_part_re, l)
                if m:
                    evening_t, evening_f, evening_d = m.group(1), m.group(2), m.group(3)
                    evening_w = f"{m.group(4)} м/с {m.group(5)}".strip().rstrip(",")
            if l.startswith("ночью ") and not night_t:
                m = re.search(_part_re, l)
                if m:
                    night_t, night_f, night_d = m.group(1), m.group(2), m.group(3)
                    night_w = f"{m.group(4)} м/с {m.group(5)}".strip().rstrip(",")

        if not now_temp and not morning_t:
            return ""

        def _part_line(icon, label, t, f, w, d):
            if not t:
                return ""
            parts = [f"{icon} *{label}:* {t}"]
            if f and f != t:
                parts.append(f"ощущ. {f}")
            if d:
                parts.append(d)
            if w:
                parts.append(f"💨 {w}")
            return ", ".join(parts) if len(parts) > 1 else parts[0]

        out = []
        # Текущая погода
        if now_temp:
            feels_str = f", ощущ. {feels}" if feels and feels != now_temp else ""
            wind_str = f", 💨 {wind}" if wind else ""
            desc_str = f" — {now_desc}" if now_desc else ""
            out.append(f"🌡 *Сейчас {now_temp}*{feels_str}{desc_str}{wind_str}")
            out.append("─────────────────────")

        # Прогноз по частям дня
        for line in [
            _part_line("🌅", "Утром",   morning_t, morning_f, morning_w, morning_d),
            _part_line("☀️",  "Днём",    day_t,     day_f,     day_w,     day_d),
            _part_line("🌆", "Вечером", evening_t, evening_f, evening_w, evening_d),
            _part_line("🌙", "Ночью",   night_t,   night_f,   night_w,   night_d),
        ]:
            if line:
                out.append(line)

        result = "\n".join(out)
        print(f"Yandex weather OK: {result[:80]}")
        return result

    except Exception as e:
        print(f"Yandex weather error: {e!r}")
        import traceback; traceback.print_exc()
        return ""


async def _fetch_weather() -> str:
    """Погода — сначала Яндекс (2 попытки), fallback на Open-Meteo."""
    for attempt in range(2):
        try:
            ya = await _fetch_yandex_weather()
            if ya:
                return ya
        except Exception as e:
            print(f"_fetch_weather: попытка {attempt+1} упала: {e!r}")
        if attempt == 0:
            await asyncio.sleep(3)
    # Fallback
    try:
        om = await _fetch_weather_openmeteo()
        if om:
            print("Weather: используем Open-Meteo fallback")
            return om
    except Exception as e:
        print(f"_fetch_weather fallback error: {e!r}")
    return ""


async def _fetch_weather_openmeteo() -> str:
    """Погода через Open-Meteo (без ключа)."""
    try:
        import httpx
        lat, lon = WEATHER_LAT, WEATHER_LON
        WMO = {
            0:"ясно",1:"почти ясно",2:"переменная облачность",3:"пасмурно",
            45:"туман",48:"туман с инеем",51:"лёгкая морось",53:"морось",55:"сильная морось",
            61:"лёгкий дождь",63:"дождь",65:"сильный дождь",
            71:"лёгкий снег",73:"снег",75:"сильный снег",77:"снежная крупа",
            80:"ливень",81:"ливни",82:"сильный ливень",
            85:"снегопад",86:"сильный снегопад",
            95:"гроза",96:"гроза с градом",99:"гроза с сильным градом",
        }
        def _icon(code):
            if code == 0: return "☀️"
            elif code in (1,2): return "⛅"
            elif code == 3: return "☁️"
            elif code in (45,48): return "🌫️"
            elif code in (51,53,55,61,63,65,80,81,82): return "🌧️"
            elif code in (71,73,75,77,85,86): return "❄️"
            elif code in (95,96,99): return "⛈️"
            else: return "🌤️"

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,weathercode,windspeed_10m,precipitation",
                    "hourly": "temperature_2m,weathercode,windspeed_10m",
                    "forecast_days": 1,
                    "timezone": "auto",
                }
            )
            d = r.json()

        cur = d["current"]
        hourly = d["hourly"]
        now_temp = round(cur["temperature_2m"])
        now_code = cur["weathercode"]
        now_wind = round(cur["windspeed_10m"])
        now_desc = WMO.get(now_code, "")
        now_precip = cur.get("precipitation", 0)
        precip_str = f" 🌧️{now_precip:.1f}мм" if now_precip and now_precip > 0 else ""

        times = hourly["time"]
        temps = hourly["temperature_2m"]
        codes = hourly["weathercode"]
        winds = hourly["windspeed_10m"]

        def _get_hour(target_h):
            for i, t in enumerate(times):
                h = int(t[11:13])
                if h >= target_h:
                    return temps[i], codes[i], winds[i]
            return None, None, None

        out = []
        out.append(f"{_icon(now_code)} Сейчас {now_temp}°C, {now_desc}, ветер {now_wind} м/с{precip_str}")

        d_temp, d_code, d_wind = _get_hour(13)
        if d_temp is not None:
            out.append(f"{_icon(d_code)} Днём {round(d_temp)}°C, {WMO.get(d_code,'')}, ветер {round(d_wind)} м/с")

        e_temp, e_code, e_wind = _get_hour(19)
        if e_temp is not None:
            out.append(f"{_icon(e_code)} Вечером {round(e_temp)}°C, {WMO.get(e_code,'')}, ветер {round(e_wind)} м/с")

        return "\n".join(out)

    except Exception as e:
        print(f"Weather fetch error: {e!r}")
        return ""
MORNING_SENT_FILE = "data/morning_sent.json"



def _is_evening_sent() -> bool:
    try:
        import json
        from config import UFA_TZ
        import datetime
        with open(EVENING_SENT_FILE) as f:
            data = json.load(f)
        return data.get("date") == datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    except Exception:
        return False

def _mark_evening_sent():
    import json, os, datetime
    from config import UFA_TZ
    os.makedirs("data", exist_ok=True)
    with open(EVENING_SENT_FILE, "w") as f:
        json.dump({"date": datetime.datetime.now(tz=UFA_TZ).date().isoformat()}, f)

def _is_morning_sent() -> bool:
    try:
        with open(MORNING_SENT_FILE) as f:
            data = json.load(f)
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return data.get("date") == today
    except Exception:
        return False


def _mark_morning_sent():
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    os.makedirs("data", exist_ok=True)
    with open(MORNING_SENT_FILE, "w") as f:
        json.dump({"date": today}, f)


async def send_morning_briefing(bot, chat_id: int):
    """9:00 — погода + расписание + дедлайны + Groq шутка. Не более 1 раза в день."""
    if _is_morning_sent():
        print("Scheduler: утренний брифинг уже был сегодня — пропускаем")
        return
    try:
        print("Scheduler: утренний брифинг 9:00...")
        await sync_all_tasks()
        from grok import ask_grok
        from bot.messages import _expand_and_sort, _lesson_emoji, _s, _short_course, _esc_md
        schedule = await _retry(_fetch_schedule_fresh_or_cache) or []
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        SEP = "┄" * 20
        day_str = f"{DAYS_RU[now.weekday()].capitalize()}, {now.day} {MONTHS_RU[now.month-1]}"
        border = "═" * 26
        pad = "   "
        lines = [
            f"☀️ *Доброе утро, {USER_NAME}!*",
            f"{day_str}",
            "",
        ]

        # Погода
        weather = await _fetch_weather()
        if weather:
            lines.append(weather)
            lines.append("")

        # Расписание
        if schedule:
            lines.append(f"*📅 ПАРЫ СЕГОДНЯ*")
            lines.append(SEP)
            from bot.messages import lxp_tag
            for lesson in _expand_and_sort(schedule):
                emoji = _lesson_emoji(lesson)
                name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
                start_t = _s(lesson.get("start_time"))
                lines.append(f"{emoji}  {start_t}  {name}{lxp_tag(lesson)}")
        else:
            lines.append("📅 Пар сегодня нет 🎉")
        lines.append("")

        # Дедлайны
        urgent = []
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if days <= 3:
                    urgent.append((days, t))
            except Exception:
                continue
        urgent.sort(key=lambda x: x[0])
        DAYS_SHORT = ["пн","вт","ср","чт","пт","сб","вс"]

        overdue = [(d, t) for d, t in urgent if d < 0]
        upcoming = [(d, t) for d, t in urgent if d >= 0]

        if upcoming:
            has_today = any(d == 0 for d, _ in upcoming)
            has_tomorrow = any(d == 1 for d, _ in upcoming)
            has_3days = any(d > 1 for d, _ in upcoming)
            if has_today:
                dl_label = "СЕГОДНЯ"
            elif has_tomorrow:
                dl_label = "ЗАВТРА"
            else:
                dl_label = "БЛИЖАЙШИЕ"
            lines.append(f"*🔴 ДЕДЛАЙНЫ — {dl_label}*")
            lines.append(SEP)
            seen_titles = set()
            shown = 0
            for days, t in upcoming:
                title = t.get("title", "")
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = f"{dt.strftime('%d.%m')} {DAYS_SHORT[dt.weekday()]}"
                except Exception:
                    date_str = ""
                course = _short_course(t.get("course_name", ""))
                # Обрезаем длинные названия (до экранирования)
                if len(title) > 40:
                    title = title[:37] + "…"
                from bot.messages import _esc_md
                prefix = f"{course} — " if course else ""
                lines.append(f"❗️  {date_str}  —  {prefix}{_esc_md(title)}")
                shown += 1
                if shown >= 5:
                    break
        elif tasks:
            lines.append(f"*📚 ЗАДАНИЯ*")
            lines.append(SEP)
            lines.append(f"Всего: {len(tasks)}, срочных нет ✅")
            now_iso = now.isoformat()
            nearest = sorted(
                [t for t in tasks if t.get("deadline") and t["deadline"] >= now_iso],
                key=lambda x: x["deadline"]
            )
            if nearest:
                t = nearest[0]
                from bot.messages import _short_course
                course = _short_course(t.get("course_name", ""))
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = dt.strftime("%d.%m")
                except Exception:
                    date_str = ""
                from bot.messages import _esc_md
                prefix = f"{course} — " if course else ""
                lines.append(f"📌 Ближайшее: {prefix}{_esc_md(t['title'][:40])}  •  {date_str}")
        else:
            lines.append("✅ Все задания выполнены!")

        # Просроченное — не старше 2 недель (более старое явно уже неактуально,
        # не стоит пугать древними хвостами), самые просроченные — первыми.
        overdue_recent = sorted([(d, t) for d, t in overdue if d >= -14], key=lambda x: x[0])
        if overdue_recent:
            lines.append("")
            lines.append(f"*⚠️ ПРОСРОЧЕНО ({len(overdue_recent)})*")
            lines.append(SEP)
            for days, t in overdue_recent[:3]:
                title = t.get("title", "")
                if len(title) > 40:
                    title = title[:37] + "…"
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = f"{dt.strftime('%d.%m')} {DAYS_SHORT[dt.weekday()]}"
                except Exception:
                    date_str = ""
                course = _short_course(t.get("course_name", ""))
                prefix = f"{course} — " if course else ""
                lines.append(f"❗️  {date_str}  —  {prefix}{_esc_md(title)}")
            if len(overdue_recent) > 3:
                lines.append(f"  _...и ещё {len(overdue_recent) - 3}_")
        # Цитата из файла
        _quote = _get_quote()
        if _quote:
            lines.append(f"\n{'─' * 20}\n💬 {_quote}")

        await send_with_retry(bot, chat_id, "\n".join(lines))
        _mark_morning_sent()
    except Exception as e:
        print(f"Scheduler morning briefing error: {e!r}")


def _load_recent_since(path: str, since: datetime.datetime) -> list:
    """Читает data/*_recent.json (их наполняют check_mail_and_notify,
    check_messenger_and_notify, check_vk_and_notify, check_netology_notifications_and_notify
    при каждом форварде) и отдаёт только записи с "at" не раньше since."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for e in data:
        at = _safe_parse_iso(e.get("at"))
        if at and at >= since:
            out.append(e)
    return out


async def send_noon_review(bot, chat_id: int):
    """12:00 — разбор того, что пришло с утра (почта/мессенджер/ВК/Нетология):
    не упустил ли что-то важное. В отличие от send_morning_briefing (смотрит
    вперёд на день) эта сводка смотрит назад — на то, что уже реально пришло
    и было переслано отдельными сообщениями, но легко потерялось в потоке."""
    if _is_noon_sent():
        print("Scheduler: разбор дня (12:00) уже был сегодня — пропускаем")
        return
    try:
        print("Scheduler: разбор дня 12:00...")
        today_start = datetime.datetime.now(tz=UFA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

        mail = _load_recent_since("data/mail_recent.json", today_start)
        messenger = _load_recent_since("data/messenger_recent.json", today_start)
        vk = _load_recent_since("data/vk_recent.json", today_start)
        netology_notif = _load_recent_since("data/netology_notif_recent.json", today_start)
        grades = _load_recent_since("data/grades_recent.json", today_start)
        new_tasks = _load_recent_since("data/tasks_added_recent.json", today_start)

        now = datetime.datetime.now(tz=UFA_TZ)
        SEP = "┄" * 20
        header = f"🕛 *Разбор дня, {USER_NAME}*\n{DAYS_RU[now.weekday()].capitalize()}, {now.day} {MONTHS_RU[now.month-1]}\n"

        if not (mail or messenger or vk or netology_notif or grades or new_tasks):
            text = f"{header}\n{SEP}\nЗа утро ничего нового не приходило — почта, мессенджер, ВК, Нетология, оценки и задания молчат.\n{SEP}"
            await send_with_retry(bot, chat_id, text)
            _mark_noon_sent()
            return

        parts = []
        if mail:
            parts.append("Почта (с текстом письма, не только тема):\n" + "\n\n".join(
                f"От {e.get('sender', '')}, тема «{e.get('subject', '')}»:\n{(e.get('body') or '')[:600]}"
                for e in mail
            ))
        if messenger:
            parts.append("Мессенджер:\n" + "\n".join(
                f"- {e.get('sender', '')}: {(e.get('text') or '')[:400]}" for e in messenger
            ))
        if vk:
            parts.append("ВКонтакте:\n" + "\n".join(
                f"- {e.get('chat_label', '')}: {(e.get('text') or '')[:400]}" for e in vk
            ))
        if netology_notif:
            parts.append("Нетология (уведомления, с текстом):\n" + "\n\n".join(
                f"«{e.get('title', '')}»:\n{(e.get('text') or '')[:500]}" for e in netology_notif
            ))
        if grades:
            parts.append("Новые оценки:\n" + "\n".join(
                f"- [{e.get('source','')}] {e.get('course','')} — {e.get('title','')}: "
                f"{(str(e.get('old_value')) + ' → ') if e.get('old_value') else ''}{e.get('value','')}"
                for e in grades
            ))
        if new_tasks:
            parts.append("Новые задания:\n" + "\n".join(
                f"- {e.get('course','')} — {e.get('title','')}"
                + (f" (дедлайн {e.get('deadline','')[:10]})" if e.get("deadline") else "")
                for e in new_tasks
            ))
        raw = "\n\n".join(parts)

        memory_recap = get_ai_memory_recap()
        prompt = (
            (f"{memory_recap}\n\n" if memory_recap else "") +
            "Вот всё, что пришло студенту с утра — почта/мессенджер/ВК/Нетология, "
            "плюс новые оценки и новые задания за это же время (само содержание "
            "уже переслано отдельными сообщениями раньше — здесь только сводка "
            "для разбора):\n\n"
            f"{raw}\n\n"
            "Проверь, нет ли среди этого чего-то важного, что легко пропустить в потоке "
            "(письма от преподавателей/деканата, реальные изменения в расписании, "
            "срочные вопросы, тревожные оценки, новые задания с близким дедлайном). "
            "Если всё рутинное — так и скажи коротко, не выдумывай важность. Ответь "
            "связным текстом 2-4 предложения, по-русски, без markdown-разметки и без "
            "списка, без вступлений."
        )
        try:
            review = await _ask_claude_cli(prompt, timeout=60)
        except Exception as e:
            review = ""
            print(f"Noon review error: {e!r}")

        body = review if review else "Не удалось получить разбор — глянь форварды выше вручную."
        if review:
            await record_ai_memory("noon", review)

        stats = []
        if mail:
            stats.append(f"{len(mail)} писем")
        if messenger:
            stats.append(f"{len(messenger)} сообщений")
        if vk:
            stats.append(f"{len(vk)} в ВК")
        if netology_notif:
            stats.append(f"{len(netology_notif)} уведомлений")
        if grades:
            stats.append(f"{len(grades)} оценок")
        if new_tasks:
            stats.append(f"{len(new_tasks)} заданий")
        stats_line = f"\n{SEP}\n📊 За утро: {' · '.join(stats)}" if stats else ""

        text = f"{header}\n{SEP}\n{body}{stats_line}"

        await send_with_retry(bot, chat_id, text)
        _mark_noon_sent()
    except Exception as e:
        print(f"Scheduler noon review error: {e!r}")


async def send_midday_briefing(bot, chat_id: int):
    """14:00 — дневная сводка."""
    if _is_midday_sent():
        print("Scheduler: дневной брифинг уже был сегодня — пропускаем")
        return
    try:
        print("Scheduler: дневной брифинг 14:00...")
        await sync_all_tasks()
        from bot.messages import _lesson_emoji, _s, _short_course

        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        SEP = "┄" * 20
        border = "═" * 26
        pad = "   "

        lines = [
            f"🌞 *Добрый день, {USER_NAME}!*",
            f"{DAYS_RU[now.weekday()].capitalize()}, {now.day} {MONTHS_RU[now.month-1]}",
            "",
        ]

        # Расписание сегодня
        from bot.messages import _expand_and_sort, _lesson_emoji, _s
        schedule = await _retry(_fetch_schedule_fresh_or_cache) or []
        if schedule:
            lines.append(f"*📅 ПАРЫ СЕГОДНЯ*")
            lines.append(SEP)
            from bot.messages import lxp_tag
            for lesson in _expand_and_sort(schedule):
                emoji = _lesson_emoji(lesson)
                name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
                start_t = _s(lesson.get("start_time"))
                lines.append(f"{emoji}  {start_t}  {name}{lxp_tag(lesson)}")
            lines.append("")
        else:
            lines.append("📅 Пар сегодня нет 🎉")
            lines.append("")

        # Все просроченные — разбор от Claude: что из этого реально горит
        # и нужно закрыть быстрее всего (не формальный список всех подряд).
        overdue = []
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if days < 0:
                    overdue.append((days, t))
            except Exception:
                continue
        overdue.sort(key=lambda x: x[0])

        if overdue:
            lines.append("*⚠️ ПРОСРОЧЕНО — РАЗБОР*")
            lines.append(SEP)
            overdue_lines = "\n".join(
                f"- {_short_course(t.get('course_name',''))} — {t.get('title','')} "
                f"(просрочено {-d} дн.)"
                for d, t in overdue
            )
            memory_recap = get_ai_memory_recap()
            knowledge_facts = get_relevant_knowledge_facts([t.get("course_name", "") for _, t in overdue])
            prompt = (
                (f"{memory_recap}\n\n" if memory_recap else "") +
                (f"{knowledge_facts}\n\n" if knowledge_facts else "") +
                f"Вот все просроченные учебные задачи студента (не сделано, дедлайн прошёл):\n\n"
                f"{overdue_lines}\n\n"
                "Выбери из них то, что реально важно закрыть как можно быстрее (зачётные/итоговые "
                "работы, тесты с большим весом, то что блокирует другие темы) — а не формальные "
                "необязательные домашки. Если выше есть организационные факты с вебинаров по этим "
                "предметам (например про формат сдачи, вес балла, мягкость срока) — учти их при "
                "оценке важности, но не выдумывай того, чего там нет. Ответь связным текстом в "
                "2-3 предложения (НЕ список, НЕ нумерация, без markdown-разметки и звёздочек — "
                "только обычные слова и знаки препинания), по-русски, без вступлений типа 'вот "
                "разбор'. Прямо суть — что горит сильнее всего и почему, остальное можно не "
                "упоминать вообще."
            )
            try:
                review = await _ask_claude_cli(prompt, timeout=60)
            except Exception as e:
                review = ""
                print(f"Midday overdue review error: {e!r}")
            lines.append(review if review else f"Просроченных задач: {len(overdue)} — не удалось получить разбор, см. /tasks")
            lines.append("")
            if review:
                await record_ai_memory("midday", review)
        else:
            lines.append("✅ Просроченных задач нет")
            lines.append("")

        # Цитата из файла
        _quote = _get_quote()
        if _quote:
            lines.append(f"{'─' * 20}")
            lines.append(f"💬 {_quote}")

        await send_with_retry(bot, chat_id, "\n".join(lines))
        _mark_midday_sent()

    except Exception as e:
        print(f"Scheduler midday briefing error: {e!r}")


# ─── Вечерний брифинг 22:00 ───────────────────────────────────────────

async def send_evening_briefing(bot, chat_id: int):
    """22:00 — итоги дня (пары сегодня + сделано задач), весёлая мотивация
    от Claude на завтра, расписание на завтра. Отправляется на границе
    тихих часов (22:00-08:00) — намеренно с ignore_quiet_hours=True."""
    if _is_evening_sent():
        print("Scheduler: вечерний брифинг уже был сегодня — пропускаем")
        return
    try:
        print("Scheduler: вечерний брифинг 22:00...")
        await sync_all_tasks()
        from bot.messages import _lesson_emoji, _s

        now = datetime.datetime.now(tz=UFA_TZ)
        today = now.date()
        tomorrow = (now + datetime.timedelta(days=1)).date()
        tom_str = f"{tomorrow.day} {MONTHS_RU[tomorrow.month-1]}, {DAYS_RU[tomorrow.weekday()]}"

        schedule_tomorrow = await _fetch_tomorrow_schedule()

        all_tasks = get_tasks()
        done_today = []
        for t in all_tasks:
            if not t.get("done"):
                continue
            done_at = t.get("done_at")
            if done_at:
                try:
                    d = datetime.datetime.fromisoformat(done_at).astimezone(UFA_TZ).date()
                    if d == today:
                        done_today.append(t)
                except Exception:
                    pass

        schedule_today = await _retry(_fetch_schedule_fresh_or_cache) or []
        passed_today = schedule_today
        pending = [t for t in all_tasks if not t.get("done") and t.get("source") != "reminder_only"]

        # Статистика — тихая запись, не отображается в самом сообщении
        record_daily_stats(len(done_today), len(pending), len(passed_today))

        SEP = "┄" * 20
        lines = [
            f"🌙 *Добрый вечер, {USER_NAME}!*",
            f"{now.day} {MONTHS_RU[now.month-1]}, {DAYS_RU[now.weekday()]}",
            "",
        ]

        # Итоги дня — пары сегодня + сделано задач
        lines.append("*📊 ИТОГИ ДНЯ*")
        lines.append(SEP)
        from bot.messages import lxp_tag, _expand_and_sort
        if passed_today:
            # _expand_and_sort — как в утреннем/дневном: если Modeus отдал одну
            # запись на пару, растянутую на 2 слота, покажем её двумя строками,
            # а не одной (раньше здесь и в "завтра" ниже этого не было).
            for l in _expand_and_sort(passed_today):
                name = _s(l.get("course_name")) or _s(l.get("name", ""))
                start = _s(l.get("start_time"))
                lines.append(f"🎓  {start}  {name}{lxp_tag(l)}")
        else:
            lines.append("Пар сегодня не было")
        lines.append("")
        lines.append(f"✅ Выполнено задач: {len(done_today)}")
        lines.append("")

        # Весёлая мотивация от Claude на завтра
        tomorrow_str_for_ai = ", ".join(
            (_s(l.get("course_name")) or _s(l.get("name", "")))[:30] for l in schedule_tomorrow
        ) if schedule_tomorrow else "пар нет"
        memory_recap = get_ai_memory_recap()
        prompt = (
            (f"{memory_recap}\n\n" if memory_recap else "") +
            f"Заверши день {USER_NAME}а весёлым, тёплым напутствием на завтра ({tom_str}). "
            f"Сегодня выполнено задач: {len(done_today)}. Завтра по расписанию: {tomorrow_str_for_ai}. "
            "2-3 предложения, с лёгким юмором, по-русски, без пафоса и без нотаций. Только текст."
        )
        try:
            motivation = await _ask_claude_cli(prompt, timeout=60, model="claude-sonnet-5")
        except Exception as e:
            motivation = ""
            print(f"Evening motivation error: {e!r}")
        if motivation:
            lines.append(f"💬 {motivation}")
            lines.append("")
            await record_ai_memory("evening", motivation)

        # Расписание завтра
        lines.append(f"*📅 ЗАВТРА — {tom_str.upper()}*")
        lines.append(SEP)
        if schedule_tomorrow:
            for lesson in _expand_and_sort(schedule_tomorrow):
                emoji = _lesson_emoji(lesson)
                name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
                start = _s(lesson.get("start_time"))
                lines.append(f"{emoji}  {start}  {name}{lxp_tag(lesson)}")
        else:
            lines.append("  Пар нет 🎉")

        await send_with_retry(bot, chat_id, "\n".join(lines), ignore_quiet_hours=True)
        _mark_evening_sent()
    except Exception as e:
        print(f"Scheduler evening briefing error: {e!r}")


async def send_it_theory_job(bot, chat_id: int):
    try:
        from study_theory import send_it_theory
        await send_it_theory(bot, chat_id)
    except Exception as e:
        print(f"it theory error: {e!r}")


async def send_it_practice_job(bot, chat_id: int):
    try:
        from study_theory import send_it_practice
        await send_it_practice(bot, chat_id)
    except Exception as e:
        print(f"it practice error: {e!r}")


async def send_it_review_job(bot, chat_id: int):
    try:
        from study_theory import send_it_review
        await send_it_review(bot, chat_id)
    except Exception as e:
        print(f"it review error: {e!r}")


async def schedule_random_quote(bot, chat_id: int):
    """Каждый день в 09:01 планирует цитату на рандомное время между 09:00 и 15:00."""
    import random as _random
    import datetime as _dt
    now = _dt.datetime.now(tz=UFA_TZ)
    # Рандомное время: от текущего момента до 15:00
    earliest = now + _dt.timedelta(minutes=5)
    latest = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if earliest >= latest:
        return
    total_seconds = int((latest - earliest).total_seconds())
    delay_seconds = _random.randint(0, total_seconds)
    send_at = earliest + _dt.timedelta(seconds=delay_seconds)
    print(f"Scheduler: цитата запланирована на {send_at.strftime('%H:%M')}")
    await asyncio.sleep(delay_seconds)
    await send_quote(bot, chat_id)


async def send_english_chunk_job(bot, chat_id: int):
    try:
        from study_theory import send_english_chunk
        await send_english_chunk(bot, chat_id)
    except Exception as e:
        print(f"english chunk error: {e!r}")


async def send_english_pronunciation_job(bot, chat_id: int):
    try:
        from study_theory import send_english_pronunciation
        await send_english_pronunciation(bot, chat_id)
    except Exception as e:
        print(f"english pronunciation error: {e!r}")


async def send_english_dialog_job(bot, chat_id: int):
    try:
        from study_theory import send_english_dialog
        await send_english_dialog(bot, chat_id)
    except Exception as e:
        print(f"english dialog error: {e!r}")



# ─── ВК мониторинг ───────────────────────────────────────────────────

async def _fetch_it_news() -> str:
    """Получаем одну IT новость с Хабра через RSS."""
    try:
        import httpx as _httpx
        import xml.etree.ElementTree as ET
        async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(
                "https://www.cnews.ru/inc/rss/news.xml",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            root = ET.fromstring(r.text)
            items = root.findall(".//item")
            import random as _random
            _random.shuffle(items)
            for item in items[:10]:
                title = item.findtext("title", "").strip()
                if title and len(title) > 15:
                    return title
    except Exception as e:
        print(f"IT news Habr error: {e!r}")
    return ""


async def check_lms_grades_and_notify(bot, chat_id: int):
    """Каждый час — проверяем новые оценки в LMS."""
    from storage import is_quiz_active
    if is_quiz_active():
        print("check_lms_grades: пропуск — активен квиз")
        return
    try:
        from parsers.lms import fetch_lms_grades_changes
        from bot.messages import format_lms_grade_notification
        from storage import get_tasks, save_tasks

        import time as _time3, json as _json3, os as _os3
        _in_grace_lms = False
        try:
            if _os3.path.exists("data/startup_grace.json"):
                _grace3 = _json3.load(open("data/startup_grace.json"))
                if _time3.time() - _grace3.get("started_at", 0) < 1200:
                    _in_grace_lms = True
        except Exception:
            pass

        changes = await _retry(fetch_lms_grades_changes) or []

        if changes:
            for change in changes:
                sent_key = change.get("_sent_key", "")
                grade_key = f"lms_grade:{sent_key}"
                if _is_notification_sent(grade_key):
                    continue
                if _in_grace_lms:
                    _mark_notification_sent(grade_key)
                    try:
                        from parsers.lms import _load_lms_grades_sent, _save_lms_grades_sent
                        _sent = _load_lms_grades_sent()
                        _sent.add(sent_key)
                        _save_lms_grades_sent(_sent)
                    except Exception:
                        pass
                    continue
                text = format_lms_grade_notification(change)
                sent_ok = await send_with_retry(bot, chat_id, text)
                if sent_ok:
                    _mark_notification_sent(grade_key)
                    _log_grade_recent(change, "lms")
                    try:
                        from parsers.lms import _load_lms_grades_sent, _save_lms_grades_sent
                        _sent = _load_lms_grades_sent()
                        _sent.add(sent_key)
                        _save_lms_grades_sent(_sent)
                    except Exception:
                        pass

    except Exception as e:
        print(f"LMS grades notify error: {e!r}")


async def check_vk_and_notify(bot, chat_id: int):
    """Каждые 15 минут — проверяем новые сообщения в беседе ВК. Работаем
    круглосуточно как остальные источники (почта/мессенджер/нетология) —
    тихие часы 22:00-08:00 обрабатывает send_with_retry (кладёт в очередь
    и доставляет с 8 утра), а не отдельная проверка здесь."""
    if _playwright_lock.locked():
        print("VK: Playwright занят — пропускаем")
        return
    async with _playwright_lock:
        await _check_vk_and_notify_inner(bot, chat_id)

def _vk_chat_label(url: str) -> str:
    import re as _re
    m = _re.search(r'sel=(c\d+)', url)
    return m.group(1) if m else url


_VK_SCHEDULE_RE = None


def _is_vk_schedule_message(text: str) -> bool:
    """Объявления об изменениях в расписании — их наставник перескажет сам,
    в общий поток дословной пересылки они не идут."""
    import re as _re
    global _VK_SCHEDULE_RE
    if _VK_SCHEDULE_RE is None:
        _VK_SCHEDULE_RE = _re.compile(
            r'расписан|мероприят|занят(ие|ия|ий|ий)|отмен|перенес|перенос|замен'
            r'|лекци|лабораторн|семинар|консультаци|пара (перенесе|отмен|добавл)'
            # Ссылку на вебинар часто публикуют отдельным сообщением ПОЗЖЕ основного
            # анонса (её ещё нет с утра) — без этих слов такое сообщение не попадёт
            # в vk_schedule_updates.json и ссылка не появится в schedule_vk_digest.json.
            r'|вебинар|ссылк|mts-link',
            _re.IGNORECASE,
        )
    return bool(_VK_SCHEDULE_RE.search(text))


def _queue_vk_schedule_update(text: str, chat_label: str):
    # scripts/mentor_checkin.py (отдельный launchd-процесс) читает и очищает
    # этот же файл — без общего лока чтение-изменение-запись здесь могло
    # затереть объявление, добавленное почти одновременно с его очисткой там.
    from data_lock import atomic_write_json, file_lock
    file = "data/vk_schedule_updates.json"
    with file_lock(file):
        try:
            with open(file) as f:
                data = json.load(f)
        except Exception:
            data = []
        data.append({
            "text": text,
            "chat_label": chat_label,
            "at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
        })
        atomic_write_json(file, data)


async def _check_vk_and_notify_inner(bot, chat_id: int):
    try:
        from parsers.vk_browser import fetch_todays_vk_messages, _mark_hash_seen, _decode_vk_links

        # Хеши храним 3 дня — защита от дублей после перезапуска
        # Сброс не делаем, просто ограничиваем размер в _mark_hash_seen

        messages = []
        for chat_url in VK_CHAT_URLS:
            label = _vk_chat_label(chat_url)
            messages.extend(await fetch_todays_vk_messages(chat_url=chat_url, chat_label=label))
        if not messages:
            return

        for msg in messages:
            try:
                vk_text = msg["text"]
                msg_hash = msg["hash"]
                chat_label = msg.get("chat_label", "")

                # Объявления об изменениях в расписании — не форвардим дословно,
                # откладываем наставнику: он перескажет их понятным языком в своём чек-ине
                if _is_vk_schedule_message(vk_text):
                    _queue_vk_schedule_update(vk_text, chat_label)
                    _mark_hash_seen(msg_hash)
                    print(f"VK: сообщение о расписании отложено наставнику hash={msg_hash}")
                    continue

                # Дословно — только чистим служебные ссылки, без AI-переформулирования
                cleaned = _decode_vk_links(vk_text)
                import re as _re_vk
                cleaned = _re_vk.sub(r'https?://vk\.com/club\d+[^\s]*', '', cleaned)
                cleaned = _re_vk.sub(r'https?://vk\.com/away\.php[^\s]*', '', cleaned)
                cleaned = _re_vk.sub(r'\s{3,}', '\n\n', cleaned).strip()
                # Экранируем HTML-спецсимволы дословного текста — свои теги добавляем уже потом
                escaped = (cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

                from bot.messages import new_vk_message
                full_text = new_vk_message(escaped, chat_label)

                _log_vk_recent(chat_label, cleaned)

                # Помечаем виденным сразу — повторная проверка не найдёт дубль
                _mark_hash_seen(msg_hash)

                # Единый путь отправки — тихие часы, разбивка длинных
                # сообщений и HTML-фолбэк уже реализованы в send_with_retry.
                sent_ok = await send_with_retry(bot, chat_id, full_text, parse_mode="HTML", disable_web_page_preview=True)
                print(f"VK: {'отправлено' if sent_ok else 'отложено'} сообщение hash={msg_hash}")

            except Exception as e:
                print(f"VK: ошибка отправки сообщения: {e!r}")

    except Exception as e:
        print(f"VK check error: {e!r}")


MIDDAY_SENT_FILE = "data/midday_sent.json"
EVENING_SENT_FILE = "data/evening_sent.json"
NOON_SENT_FILE = "data/noon_sent.json"


def _is_noon_sent() -> bool:
    try:
        import json as _json
        with open(NOON_SENT_FILE) as f:
            data = _json.load(f)
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return data.get("date") == today
    except Exception:
        return False


def _mark_noon_sent():
    import json as _json
    os.makedirs("data", exist_ok=True)
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    with open(NOON_SENT_FILE, "w") as f:
        _json.dump({"date": today}, f)


def _is_midday_sent() -> bool:
    """Проверяем — была ли уже дневная сводка сегодня."""
    try:
        import json as _json
        with open(MIDDAY_SENT_FILE) as f:
            data = _json.load(f)
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return data.get("date") == today
    except Exception:
        return False


def _mark_midday_sent():
    """Помечаем что дневная сводка сегодня уже отправлена."""
    import json as _json
    os.makedirs("data", exist_ok=True)
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    with open(MIDDAY_SENT_FILE, "w") as f:
        _json.dump({"date": today}, f)


def _get_quote() -> str:
    """Берём случайную цитату из файла, каждая появляется раз за полный цикл."""
    import json as _json2, os as _os2, random as _random2
    quotes_file = "data/quotes.json"
    state_file = "data/quotes_state.json"
    try:
        with open(quotes_file, encoding="utf-8") as f:
            all_quotes = _json2.load(f)
    except Exception:
        return "Успех — это сумма небольших усилий, повторяемых день за днём."
    try:
        with open(state_file) as f:
            state = _json2.load(f)
    except Exception:
        state = {"remaining": []}
    remaining = state.get("remaining", [])
    if not remaining:
        remaining = list(range(len(all_quotes)))
        _random2.shuffle(remaining)
    idx = remaining.pop(0)
    _os2.makedirs("data", exist_ok=True)
    with open(state_file, "w") as f:
        _json2.dump({"remaining": remaining}, f)
    return all_quotes[idx]


async def send_quote(bot, chat_id: int):
    """13:00 — мотивационная цитата дня."""
    import datetime as _dt
    today = _dt.datetime.now(tz=UFA_TZ).date().isoformat()
    state_file = "data/quote_sent.json"
    try:
        with open(state_file) as f:
            if json.load(f).get("date") == today:
                print("Scheduler: цитата уже была сегодня")
                return
    except Exception:
        pass
    try:
        quote = _get_quote()
        msg = f"💬 *Цитата дня*\n\n_{quote}_"
        await send_with_retry(bot, chat_id, msg, parse_mode="Markdown")
        os.makedirs("data", exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"date": today}, f)
        print("Scheduler: цитата дня отправлена")
    except Exception as e:
        print(f"Scheduler quote error: {e!r}")


# ─── Напоминалка 17:00 ───────────────────────────────────────────────

_REMINDERS_17 = [
    "🔥 Эй, ещё не вечер! Закрой хотя бы одну задачу до брифинга.",
    "⏰ 17:00 — самое время сделать то, что откладывал с утра.",
    "📚 Через 4 часа вечерний брифинг. Есть что отметить выполненным?",
    "🎯 Одна задача сейчас = спокойный вечер. Давай.",
    "⚡️ Рабочее время ещё идёт. Не трать его на мемы.",
    "📌 Напоминаю: дедлайны сами себя не выполнят.",
    "🚀 До конца дня 4 часа. Используй хотя бы один.",
    "😤 Ты ещё не сделал то что планировал утром. Пора.",
    "💡 Сейчас самый продуктивный момент дня. Не пропусти.",
    "🏃 Финишная прямая дня — осталось немного. Закрой одну задачу.",
    "😴 Не засыпай! До вечера ещё куча времени.",
    "🎓 Будущий ты скажет спасибо если сделаешь это сейчас.",
    "📝 5 минут чтобы начать. Начни.",
    "🤔 Что важнее — очередной ролик или закрытый дедлайн?",
    "🏆 Чемпионы не ждут вдохновения. Они просто делают.",
    "📅 Завтра будет легче если сделать сегодня.",
    "🔔 Дедлайн не спит. А ты?",
    "💪 Одно задание. Прямо сейчас. Го.",
    "🎯 Фокус! Телефон в сторону, задача перед тобой.",
    "⏳ Время идёт в любом случае. Пусть идёт с пользой.",
    "🧠 Мозг разогрет с утра. Используй пока не остыл.",
    "😎 Сделай сейчас — вечером будешь собой гордиться.",
    "🚨 Внимание: обнаружены незакрытые задачи. Требуется вмешательство.",
    "📖 Открой задание. Просто открой. Дальше само пойдёт.",
    "🌅 День ещё не закончился. Сделай его продуктивным.",
    "💥 Взрыв продуктивности через 3... 2... 1... Давай!",
    "🤖 ДедЛайнер напоминает: ты ещё не сделал домашку.",
    "🎪 Шоу называется \"Я точно сделаю это потом\". Занавес пора закрывать.",
    "😏 Дедлайн смотрит на тебя. Что скажешь?",
    "⚡️ Зарядка кончается? Нет — это продуктивность. Подзарядись делом.",
]

async def send_afternoon_reminder(bot, chat_id: int):
    """17:00 — случайная напоминалка + 2 ближайших задачи."""
    import datetime as _dt
    today = _dt.datetime.now(tz=UFA_TZ).date().isoformat()
    state_file = "data/reminder17_sent.json"
    try:
        with open(state_file) as f:
            if json.load(f).get("date") == today:
                print("Scheduler: напоминалка 17:00 уже была сегодня")
                return
    except Exception:
        pass
    try:
        import random
        msg = random.choice(_REMINDERS_17)

        # Добавляем 2 ближайших задачи
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        with_deadline = []
        for t in tasks:
            try:
                d = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (d - now).days
                with_deadline.append((days, t))
            except Exception:
                continue
        with_deadline.sort(key=lambda x: x[0])

        upcoming = [(d, t) for d, t in with_deadline if d >= 0]
        if upcoming:
            _days_short = ["пн","вт","ср","чт","пт","сб","вс"]
            deadline_lines = "───────────────────\n📚 *БЛИЖАЙШИЕ ДЕДЛАЙНЫ*"
            for days, t in upcoming[:3]:
                title = t.get("title", "")
                if len(title) > 40:
                    title = title[:37] + "…"
                from bot.messages import _short_course, _esc_md
                title = _esc_md(title)
                course = _short_course(t.get("course_name", ""))
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = f"{dt.strftime('%d.%m')} {_days_short[dt.weekday()]}"
                except Exception:
                    date_str = ""
                prefix = f"{course} — " if course else ""
                deadline_lines += f"\n❗️  {date_str}  —  {prefix}{title}"
            deadline_lines += "\n───────────────────"
            msg = deadline_lines + "\n\n" + msg

        await send_with_retry(bot, chat_id, msg, parse_mode="Markdown")
        os.makedirs("data", exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"date": today}, f)
        print("Scheduler: напоминалка 17:00 отправлена")
    except Exception as e:
        print(f"Scheduler reminder 17 error: {e!r}")

# ─── Встречная проверка launchd-задач наставника ──────────────────────
# scripts/error_watchdog.py следит за ботом (main.py) и остальными launchd-
# задачами наставника, но сам он тоже launchd-задача — если пропадёт именно
# он (см. 2026-09-20: mentor_checkin пропала из launchctl list на 3 дня
# незамеченной), следить за ним больше некому. Бот сам продолжает жить
# (APScheduler-джобы работают) даже когда всё остальное молчит — этим и
# пользуемся: раз в час бот сам сверяет com.ilnursafin.errorwatchdog
# (и заодно остальные launchd-задачи наставника, на случай если пропали
# сразу несколько) и тихо перезагружает то, что отвалилось.
_LAUNCHD_JOBS_TO_WATCH = {
    "com.ilnursafin.errorwatchdog": "~/Library/LaunchAgents/com.ilnursafin.errorwatchdog.plist",
    "com.ilnursafin.mentorcheckin": "~/Library/LaunchAgents/com.ilnursafin.mentorcheckin.plist",
    "com.ilnursafin.mentorhourlywatch": "~/Library/LaunchAgents/com.ilnursafin.mentorhourlywatch.plist",
    "com.ilnursafin.yaclinks": "~/Library/LaunchAgents/com.ilnursafin.yaclinks.plist",
}
_LAUNCHD_ALERT_FILE = "data/launchd_watch_state.json"


async def check_launchd_health(bot, chat_id: int):
    try:
        proc = await asyncio.create_subprocess_exec(
            "launchctl", "list",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        loaded_labels = {
            line.split("\t")[-1].strip()
            for line in stdout.decode("utf-8", "ignore").splitlines() if line.strip()
        }
    except Exception as e:
        print(f"check_launchd_health: launchctl list не удался: {e!r}")
        return

    try:
        with open(_LAUNCHD_ALERT_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}

    now = datetime.datetime.now(tz=UFA_TZ)
    changed = False
    for label, plist_rel in _LAUNCHD_JOBS_TO_WATCH.items():
        if label in loaded_labels:
            continue
        plist_path = os.path.expanduser(plist_rel)
        if not os.path.exists(plist_path):
            continue
        print(f"check_launchd_health: {label} не зарегистрирована — перезагружаю")
        try:
            load_proc = await asyncio.create_subprocess_exec(
                "launchctl", "load", plist_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(load_proc.wait(), timeout=15)
        except Exception as e:
            print(f"check_launchd_health: не удалось перезагрузить {label}: {e!r}")
            continue

        last_alert = state.get(label)
        alert_cooldown_ok = True
        if last_alert:
            mins = (now - datetime.datetime.fromisoformat(last_alert)).total_seconds() / 60
            alert_cooldown_ok = mins > 180
        if alert_cooldown_ok:
            await send_with_retry(
                bot, chat_id,
                f"⚙️ Задача {label} пропала из launchd — перезагрузил обратно (проверка со стороны бота).",
                ignore_quiet_hours=True,
            )
            state[label] = now.isoformat()
            changed = True

    if changed:
        try:
            with open(_LAUNCHD_ALERT_FILE, "w") as f:
                json.dump(state, f)
        except Exception:
            pass


def setup_scheduler(bot, chat_id: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=UFA_TZ,
        job_defaults={"misfire_grace_time": 7200}  # для фоновых джобов
    )

    # ──────────────────────────────────────────────────────────────
    # Проактивные push-уведомления бота ОТКЛЮЧЕНЫ (брифинги, теория,
    # английский, цитаты, авто-уведомления о почте/мессенджере/ВК/
    # оценках, недельный отчёт, случайная мотивация) — их роль теперь
    # выполняет отдельный наставник-агент (scripts/mentor_checkin.py).
    # Job'ы оставлены закомментированными, чтобы легко вернуть при желании.
    # ──────────────────────────────────────────────────────────────

    # ── Утренний брифинг — включён на 08:30 (2026-09-17): формат подтверждён
    #    пользователем (погода/пары/дедлайны 3 дня/топ-3 просроченных/цитата) ──
    scheduler.add_job(send_morning_briefing, trigger="cron", hour=8, minute=30,
                      args=[bot, chat_id], id="morning_830", misfire_grace_time=3600)
    # scheduler.add_job(send_english_chunk_job, trigger="cron", hour=10, minute=0,
    #                   args=[bot, chat_id], id="english_chunk_1000", misfire_grace_time=3600)
    # ── Разбор дня — включён на 12:00 (2026-09-17): что пришло с утра по почте/
    #    мессенджеру/ВК/Нетологии, не упущено ли важное ──
    scheduler.add_job(send_noon_review, trigger="cron", hour=12, minute=0,
                      args=[bot, chat_id], id="noon_1200", misfire_grace_time=3600)
    # ── Дневной брифинг — включён на 14:00 (2026-09-17): пары + разбор
    #    просрочек от Claude вместо задачи дня/мотивации от Groq ──
    scheduler.add_job(send_midday_briefing, trigger="cron", hour=14, minute=0,
                      args=[bot, chat_id], id="midday_14", misfire_grace_time=3600)
    # scheduler.add_job(send_it_theory_job, trigger="cron", hour=11, minute=0,
    #                   args=[bot, chat_id], id="theory_it_1100", misfire_grace_time=3600)
    # scheduler.add_job(send_it_practice_job, trigger="cron", hour=13, minute=0,
    #                   args=[bot, chat_id], id="theory_it_practice_1300", misfire_grace_time=3600)
    # scheduler.add_job(send_english_pronunciation_job, trigger="cron", hour=15, minute=30,
    #                   args=[bot, chat_id], id="english_pronun_1530", misfire_grace_time=3600)
    # scheduler.add_job(send_afternoon_reminder, trigger="cron", hour=17, minute=0,
    #                   args=[bot, chat_id], id="reminder_17", misfire_grace_time=3600)
    # scheduler.add_job(send_it_review_job, trigger="cron", hour=19, minute=0,
    #                   args=[bot, chat_id], id="theory_it_review_1900", misfire_grace_time=3600)
    # ── Вечерний брифинг — включён на 22:00 (2026-09-17): итоги дня + весёлая
    #    мотивация от Claude на завтра + расписание завтра. Граница тихих
    #    часов — send_evening_briefing сам шлёт с ignore_quiet_hours=True ──
    scheduler.add_job(send_evening_briefing, trigger="cron", hour=22, minute=0,
                      args=[bot, chat_id], id="evening_22", misfire_grace_time=3600)
    # scheduler.add_job(send_english_dialog_job, trigger="cron", hour=22, minute=0,
    #                   args=[bot, chat_id], id="english_dialog_2200", misfire_grace_time=3600)
    # scheduler.add_job(schedule_random_quote, trigger="cron", hour=9, minute=1,
    #                   args=[bot, chat_id], id="quote_random_scheduler", misfire_grace_time=3600)
    # scheduler.add_job(send_weekly_report, trigger="cron", day_of_week="sun", hour=20, minute=0,
    #                   args=[bot, chat_id], id="weekly_report", misfire_grace_time=3600)
    # ── Почта/Gmail/Мессенджер/ВК/Нетология — единый интервал 15 мин и единый
    #    стиль уведомлений (заголовок/подзаголовок/разделитель/цитата) ──
    scheduler.add_job(check_vk_and_notify, trigger="interval", minutes=15,
                      args=[bot, chat_id], id="vk_monitor", max_instances=1, coalesce=True)
    # ── Оценки — раз в час (Modeus и LMS вместе, единая частота) ──
    scheduler.add_job(check_grades_and_notify, trigger="interval", minutes=60,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=3),
                      args=[bot, chat_id], id="grades_check", max_instances=1, coalesce=True)
    scheduler.add_job(check_lms_grades_and_notify, trigger="interval", minutes=60,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=2),
                      args=[bot, chat_id], id="lms_grades_check", max_instances=1, coalesce=True)
    scheduler.add_job(check_mail_and_notify, trigger="interval", minutes=15,
                      args=[bot, chat_id], id="mail_check", max_instances=1, coalesce=True)
    scheduler.add_job(check_messenger_and_notify, trigger="interval", minutes=15,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=2),
                      args=[bot, chat_id], id="messenger_check", max_instances=1, coalesce=True)
    scheduler.add_job(check_netology_notifications_and_notify, trigger="interval", minutes=15,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=4),
                      args=[bot, chat_id], id="netology_notif_check", max_instances=1, coalesce=True)
    # ── Gmail (второй почтовый ящик) — 2026-09-17 ──
    scheduler.add_job(check_gmail_and_notify, trigger="interval", minutes=15,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=1),
                      args=[bot, chat_id], id="gmail_check", max_instances=1, coalesce=True)
    # ── Ночной полный обход Мессенджера (00:00) — читает все чаты целиком
    #    для контекста наставника, ничего не шлёт в Telegram напрямую ──
    scheduler.add_job(messenger_nightly_full_sweep, trigger="cron", hour=0, minute=0,
                      args=[bot, chat_id], id="messenger_nightly_sweep", max_instances=1, coalesce=True)
    # scheduler.add_job(check_random_reminder, trigger="interval", minutes=5,
    #                   args=[bot, chat_id], id="random_reminder")
    # Уведомления "скоро пара" за 60 и 15 минут — включены 2026-10-01 по
    # просьбе пользователя (раньше функция была отключена и знала только Modeus).
    scheduler.add_job(check_lesson_reminders, trigger="interval", minutes=2,
                      args=[bot, chat_id], id="lesson_reminders", max_instances=1, coalesce=True)
    # check_deadline_reminders (за 7/3/1 день до дедлайна) — рабочая функция,
    # но НИКОГДА не была зарегистрирована здесь (найдено аудитом 2026-09-17,
    # не то же самое, что осознанно отключённые джобы выше). Оставлена
    # закомментированной как и соседи, а не включена — сейчас напоминания
    # о дедлайнах идёт через наставника (scripts/mentor_checkin.py).
    # scheduler.add_job(check_deadline_reminders, trigger="interval", hours=6,
    #                   args=[bot, chat_id], id="deadline_reminders")

    # ── Оставлено: доставка пользовательских напоминаний/задач ──
    scheduler.add_job(retry_pending_notifications, trigger="interval", minutes=10,
                      args=[bot], id="retry_notifications")
    scheduler.add_job(check_user_reminders, trigger="interval", minutes=3,
                      args=[bot, chat_id], id="user_reminders")
    scheduler.add_job(sync_all_tasks, trigger="interval", minutes=60,
                      args=[bot, chat_id], id="sync_tasks", max_instances=1, coalesce=True,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=5))
    # ── Тихое обновление schedule_cache.json (без Telegram) — раньше было
    #    побочным эффектом отключённых брифингов, без него кэш не обновлялся
    #    сам вообще (баг обнаружен 2026-09-10 — не видно было английского) ──
    scheduler.add_job(refresh_schedule_cache_silent, trigger="interval", hours=4,
                      id="schedule_cache_refresh", max_instances=1, coalesce=True,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=1))
    # ── Встречная проверка launchd-задач наставника (error_watchdog и др.) —
    #    бот сам переживает то, что роняет остальное (см. комментарий у
    #    check_launchd_health) ──
    scheduler.add_job(check_launchd_health, trigger="interval", hours=1,
                      args=[bot, chat_id], id="launchd_health", max_instances=1, coalesce=True,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=2))

    print("Scheduler: настроен ✅ (проактивные уведомления отключены, наставник — отдельным процессом)")
    return scheduler

