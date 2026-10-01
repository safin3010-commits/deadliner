"""
Единая точка входа для свободного текста в боте — задача / напоминание /
повторяющееся напоминание / вопрос / реплика без действия. Один вызов Клода
вместо прежней цепочки: обязательный вопрос "задача/напоминание/вопрос?" →
слой regex-предфильтров → Groq (ask_claude_fast, несмотря на название — не
Клод) → часто ещё одно уточнение кнопкой → отдельный мастер параметров
повтора (bot/reminder_wizard.py). См. обсуждение в сессии 2026-09-28.

Использует общую внутридневную сессию (claude_session.py) — тот же файл
data/claude_session.json, что и у наставника/сводок, так что бот реально
помнит контекст разговора в течение дня, а не начинает с нуля каждый раз.
Клоду даётся только чтение (Read/Glob/Grep) — записывает в tasks.json/
reminders.json всегда код здесь, а не сам Клод, это сознательная граница
безопасности (см. обсуждение с пользователем).
"""
from __future__ import annotations

import datetime
import json
import re

from config import UFA_TZ, USER_NAME
from storage import add_task

# "пока сам не остановлю" — уже существующая в проекте конвенция для
# бессрочных напоминаний (scheduler.py и bot/keyboards.py уже трактуют
# times_left >= 9999 как "бессрочно"/"каждый день" в отображении).
UNLIMITED_TIMES = 9999
MAX_TIMES = 9999          # разумный потолок даже для явно названного числа
MIN_INTERVAL_MINUTES = 1  # защита от interval=0 с times>1 (не должно прийти от модели, но на всякий случай)

_DAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

INTENT_PROMPT_TEMPLATE = """Ты — модуль разбора входящих сообщений в Telegram-боте студента {user_name}.
Сейчас: {now_str} ({weekday}), время {time_str}.

Сообщение пользователя: "{text}"

Определи, что нужно сделать, и верни СТРОГО валидный JSON без markdown-обёртки, ровно такой схемы:
{{
  "items": [
    {{
      "type": "task | reminder | recurring_reminder | complete | commitment | question | none",
      "title": "суть дела, без дат и служебных слов вроде 'напомни'/'сделай'",
      "deadline_iso": "YYYY-MM-DDTHH:MM:SS без часового пояса, или null",
      "reminder_at_iso": "YYYY-MM-DDTHH:MM:SS без часового пояса, или null",
      "interval_minutes": число_или_null,
      "times": число_или_строка_"unlimited"_или_null,
      "start_at_iso": "YYYY-MM-DDTHH:MM:SS без часового пояса, или null",
      "event_date_iso": "YYYY-MM-DD — дата САМОГО СОБЫТИЯ, если она отличается от времени напоминания, или null",
      "task_id": "id найденной активной задачи/напоминания из блока АКТИВНЫЕ ЗАДАЧИ, или null",
      "quote": "для commitment — дословная фраза пользователя с обещанием, иначе пустая строка",
      "answer_text": "прямой ответ пользователю или пустая строка",
      "confidence": "high или low"
    }}
  ]
}}

ПРАВИЛА:
1. type="task" — есть дело со сроком, без явного "напомни" ("сдать лабу до пятницы"). Заполни title и deadline_iso (или null, если срока нет вообще).
2. type="reminder" — разовое "напомни мне X [в такое-то время]". Заполни title и reminder_at_iso.
3. type="recurring_reminder" — "напоминай каждые/каждый ... N раз" или "пока сам не остановлю"/"постоянно"/без ограничения. Заполни title, interval_minutes, и times: конкретное число, если названо явно, иначе строку "unlimited". start_at_iso — только если пользователь явно назвал время/день старта; если ничего не сказано про время начала (например просто "каждые 3 часа") — start_at_iso=null (первое сработает через interval_minutes от текущего момента), НЕ выдумывай время сам.
4. type="complete" (answer_text пустой — закрытие подтверждается кнопкой, не пиши «закрыл») — пользователь либо говорит, что дело уже сделано ("купил маску", "сделал лабу по БД", "готово с отчётом"), ЛИБО явно просит закрыть/завершить/отметить выполненной конкретную задачу ("заверши это задание", "закрой", "отметь выполненным", "можешь закрыть эту задачу") — второе тоже "complete", даже если сформулировано как команда/будущее время, а не как рассказ о свершившемся факте. Отдельно: "хватит напоминать про X"/"останови напоминание" — тоже "complete" (снимает напоминание, не обязательно означает "уже сделано"). Часто перед такой командой пользователь копирует/пересылает сам текст задачи из списка (с эмодзи-статусом, обрезанным "…", датой и т.п.) — это не новое дело и не дедлайн для type="task", это просто способ точно указать, о какой записи речь. В блоке «АКТИВНЫЕ ЗАДАЧИ» ниже найди запись, что по смыслу/названию совпадает с тем, что процитировал или описал пользователь, и верни её id (первое поле строки) в task_id как строку. Если ни одна не подходит уверенно — task_id=null и confidence="low". НЕ путай с type="task" — если пользователь сообщает о завершении или просит закрыть существующее дело, а не заводит новое, это всегда "complete".
5. type="question" — обычный вопрос про дедлайны/расписание/вебинары/оценки и т.п. Ответь на него ПРЯМО в answer_text — ТОЛЬКО по блокам данных ниже (их собрал код: задачи, расписание, дедлайны, оргфакты с вебинаров и переписка, найденные поиском по вопросу). Если ответа в блоках нет — честно скажи, что в данных этого не видно, и подскажи, где посмотреть; не выдумывай даты и факты. Пиши как живой человек по-русски (без английских вставок), на "ты", без канцелярита, Telegram HTML (только теги <b>/<i>, без markdown-звёздочек).
6. type="none" — реплика без действия (спасибо/ок/привет и т.п.) — в answer_text короткий дружеский ответ, можно пустую строку, если ответ не нужен вовсе. ВАЖНО: type="none" реально ничего не сохраняет и не меняет — НИКОГДА не пиши в ответе "принято"/"запомнил"/"учту" или что-то ещё, что звучит как будто ты только что что-то сохранил или создал — это неправда и вводит в заблуждение. Если сообщение — пересланный/скопированный фрагмент (например текст уведомления о задании с эмодзи-статусом, обрезанным "…" и датой) с комментарием вроде "заверши это завтра" — это НЕ новая информация для тебя и не команда что-то менять, просто констатируй факт по данным (например реальный дедлайн из tasks.json), без наигранного тона "я запомнил".
7. ПРО ВРЕМЯ: если в сообщении указан только день без точного часа ("завтра", "через 3 дня", "в пятницу", "послезавтра") — время = 09:00 того дня. Если есть точное время или чистый оффсет от текущего момента ("через 20 минут", "через 3 часа", "в 15:00") — используй его буквально от текущего момента. Все даты — в будущем; если получилось в прошлом, это ближайшее будущее вхождение (пятница = ближайшая будущая).
8. Несколько дел в одном сообщении ("купи молоко и позвони маме завтра") — несколько элементов items, у каждого свой type.
9. ПРО ПРОДОЛЖЕНИЯ РАЗГОВОРА: если в сообщении не хватает сути (нет названия/темы — например "сделай каждые 3 часа напоминания" без указания о чём), сначала проверь, не продолжение ли это того, что только что обсуждалось в этой же сессии (ты помнишь предыдущие сообщения этого разговора) — если пользователь только что просил напомнить о чём-то конкретном, а этим сообщением уточняет частоту/срок/детали того же самого — возьми title из того предыдущего сообщения, не создавай второй пустой пункт и не спрашивай "что за напоминание". confidence="low" ставь только если ДАЖЕ с учётом истории разговора непонятно, к чему это относится.
10. ПРО ДАТУ СОБЫТИЯ (только для reminder/recurring_reminder): если напоминание — о событии с собственной датой, которая НЕ совпадает с моментом напоминания ("напомни сегодня вечером, что 2 октября приём у ортодонта" → reminder_at_iso = сегодня вечер, event_date_iso = 2 октября), заполни event_date_iso датой события. Если дата события совпадает с днём напоминания или события как такового нет ("напоминай пить воду") — null. Дату события в title не дублируй.
11. type="commitment" — пользователь САМ обещает что-то сделать к сроку, в первом лице и утвердительно: "сделаю лабу в пятницу", "завтра сдам тест", "напишу преподу вечером". title = что сделать (коротко), deadline_iso = к какому сроку (если назван; день без часа → 23:59 того дня), quote = дословная фраза. НЕ commitment: "надо бы", "может, в пятницу", "постараюсь", "подумаю", дедлайн курса ("лабу нужно сдать к пятнице" — это факт, не обещание), просьба напомнить (это reminder). Сомневаешься — confidence="low". Для commitment answer_text оставь ПУСТЫМ: пользователь сам подтвердит запись кнопкой, не пиши «записал». Обещание не заменяет другие пункты: если в том же сообщении есть вопрос — верни оба элемента.
12. Только JSON, без пояснений до или после.

ДАННЫЕ (собраны кодом, файлы читать не нужно):
{pack}"""


def _strip_json_wrapper(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


INTENT_SYSTEM_PROMPT = (
    "Ты — модуль разбора сообщений и личный наставник студента в Telegram-боте. "
    "Отвечаешь строго валидным JSON по схеме из сообщения. Все данные уже в сообщении — "
    "файлы не читаешь. Блоки с пометкой «код, точное» — факты: не меняй даты и не "
    "выдумывай того, чего в них нет."
)


async def _call_and_parse(prompt: str, timeout: int = 90, purpose: str = "bot_intent") -> tuple[dict | None, str]:
    """Вызов Клода + разбор JSON с автопочинкой формата — модель иногда не
    экранирует кавычку внутри строки и ломает структуру.

    Разовый вызов без дневной сессии и без инструментов: контекст собирает
    код (scripts/mentor_context.build_chat_pack), связность разговора — через
    data/agent.db dialog_messages. Раньше каждое сообщение шло через --resume
    сессии дня с Read/Grep по файлам — весь разговор дня пересылался заново."""
    import asyncio
    from claude_session import run_claude_oneshot

    text, err = await asyncio.to_thread(
        run_claude_oneshot, prompt, INTENT_SYSTEM_PROMPT, timeout, "claude-sonnet-5", purpose,
    )
    if err:
        return None, err

    raw = _strip_json_wrapper(text)
    try:
        return json.loads(raw, strict=False), ""
    except json.JSONDecodeError:
        pass

    # Починка формата отдельным дешёвым запросом, без пересчёта содержания.
    repair_prompt = (
        "Следующий текст должен быть валидным JSON, но не парсится (вероятно, "
        "неэкранированная кавычка внутри строки). Верни РОВНО ТОТ ЖЕ контент "
        "синтаксически корректным JSON, ничего не меняя по смыслу. Ответь "
        "строго JSON, без markdown-обёртки, без пояснений.\n\n" + raw
    )
    repaired_text, repair_err = await asyncio.to_thread(
        run_claude_oneshot, repair_prompt, "Ты чинишь синтаксис JSON. Отвечай только JSON.",
        60, "claude-haiku-4-5-20251001", "bot_intent_json_repair",
    )
    if repair_err:
        return None, f"JSON невалиден, починка не удалась: {repair_err}"
    try:
        return json.loads(_strip_json_wrapper(repaired_text), strict=False), ""
    except json.JSONDecodeError as e:
        return None, f"JSON невалиден даже после починки: {e!r}"


def _chat_pack(text: str) -> str:
    import os
    import sys
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from mentor_context import build_chat_pack
    return build_chat_pack(text)


async def extract_intent(text: str) -> tuple[dict | None, str]:
    import asyncio
    now = datetime.datetime.now(tz=UFA_TZ)
    pack = await asyncio.to_thread(_chat_pack, text)
    prompt = INTENT_PROMPT_TEMPLATE.format(
        user_name=USER_NAME,
        now_str=now.strftime("%d.%m.%Y"),
        weekday=_DAYS_RU[now.weekday()],
        time_str=now.strftime("%H:%M"),
        text=text,
        pack=pack,
    )
    return await _call_and_parse(prompt, timeout=90)


def _parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
        # Всегда переприменяем UFA_TZ, а не доверяем часовому поясу от
        # модели — обнаружено вживую 2026-09-28: модель вернула +03:00
        # (московский) вместо реального +05:00 (Asia/Yekaterinburg), хотя
        # "сейчас" в промпте дано локальным временем без указания оффсета.
        # Сами числа даты/времени модель считает верно, ошибается только
        # подпись пояса — поэтому просто берём "голые" числа и сами
        # прикрепляем правильный пояс, а не то, что написала модель.
        return dt.replace(tzinfo=UFA_TZ)
    except Exception:
        return None


def _clamp_times(times_raw) -> int:
    if times_raw in ("unlimited", "unlimited_times", None):
        return UNLIMITED_TIMES
    try:
        n = int(times_raw)
    except (TypeError, ValueError):
        return UNLIMITED_TIMES
    return max(1, min(n, MAX_TIMES))


def create_task(title: str, deadline_iso: str | None) -> dict:
    dl = _parse_iso(deadline_iso)
    return add_task(title, dl.isoformat() if dl else None, "manual")


def _event_date(event_date_iso: str | None) -> str | None:
    """Дата самого события (YYYY-MM-DD), отдельно от времени напоминания —
    иначе окно на столе показывало "01.10 — Приём ортодонта 2 октября":
    deadline у reminder_only — это момент первого срабатывания, а не событие."""
    if not event_date_iso:
        return None
    try:
        return datetime.date.fromisoformat(str(event_date_iso)[:10]).isoformat()
    except ValueError:
        return None


def create_one_off_reminder(title: str, reminder_at_iso: str | None, event_date_iso: str | None = None) -> tuple[dict, datetime.datetime]:
    from reminders import add_reminder
    now = datetime.datetime.now(tz=UFA_TZ)
    rem_dt = _parse_iso(reminder_at_iso) or (now + datetime.timedelta(minutes=10))
    task = add_task(title, rem_dt.isoformat(), "reminder_only", event_date=_event_date(event_date_iso))
    add_reminder(str(task["id"]), title, 0, 1, start_at=rem_dt.isoformat())
    return task, rem_dt


def create_recurring_reminder(title: str, interval_minutes, times_raw, start_at_iso: str | None, event_date_iso: str | None = None) -> tuple[dict, dict]:
    from reminders import add_reminder
    now = datetime.datetime.now(tz=UFA_TZ)
    start_dt = _parse_iso(start_at_iso) or now
    try:
        interval = max(MIN_INTERVAL_MINUTES, int(interval_minutes))
    except (TypeError, ValueError):
        interval = 60
    times = _clamp_times(times_raw)
    task = add_task(title, start_dt.isoformat(), "reminder_only", event_date=_event_date(event_date_iso))
    reminder = add_reminder(str(task["id"]), title, interval, times, start_at=start_dt.isoformat())
    return task, reminder


def complete_item(task_id: str) -> tuple[bool, str]:
    """Завершить задачу или остановить напоминание по id, найденному моделью
    в data/tasks.json. Для reminder_only — та же логика, что уже была у
    кнопки "🗑 Не напоминать" (снять напоминание(я) + убрать служебную
    задачу); для обычной задачи — обычное mark_task_done. Возвращает
    (успех, название) — название для сообщения пользователю.

    Сама логика — в task_actions.complete_task (общая с окном наставника на
    столе): там же снимаются напоминания и у обычных задач, не только у
    reminder_only, и всё под локами tasks.json/reminders.json."""
    from task_actions import complete_task

    result = complete_task(task_id, origin="bot")
    return result["ok"], result.get("title", "")


def find_candidate_tasks(query: str, limit: int = 5) -> list[dict]:
    """Локальный запасной поиск (без нового вызова Клода) для случая, когда
    модель сама не уверена (confidence=low, task_id=null) — простое
    нечёткое совпадение по подстроке/словам, чтобы предложить кнопками
    вместо голого "не нашёл"."""
    import difflib
    from storage import get_tasks
    tasks = [t for t in get_tasks() if not t.get("done")]
    ql = query.lower()
    scored = []
    for t in tasks:
        title_l = (t.get("title") or "").lower()
        ratio = difflib.SequenceMatcher(None, ql, title_l).ratio()
        if ql in title_l or title_l in ql:
            ratio = max(ratio, 0.6)
        if ratio > 0.3:
            scored.append((ratio, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:limit]]


REMINDER_PARAMS_PROMPT_TEMPLATE = """Пользователь настраивает напоминание для уже существующей задачи «{task_title}».
{deadline_hint}
Сейчас: {now_str} ({weekday}), время {time_str}.

Текст пользователя: "{text}"

Верни СТРОГО валидный JSON без markdown-обёртки:
{{"interval_minutes": число, "times": число_или_строка_"unlimited", "start_at_iso": "YYYY-MM-DDTHH:MM:SS без часового пояса, или null"}}

ПРАВИЛА:
- "каждый час 3 раза" → interval_minutes=60, times=3, start_at_iso=null (первое через interval_minutes от сейчас)
- "каждые 30 минут 5 раз" → interval_minutes=30, times=5
- "пока сам не остановлю"/"постоянно"/без числа повторов → times="unlimited"
- "за неделю до дедлайна раз в день" → interval_minutes=1440, times=7, start_at_iso=дедлайн минус 7 дней
- "каждое утро в 9:00 до дедлайна" → interval_minutes=1440, times=дней_до_дедлайна, start_at_iso=завтра 09:00
- "напомни 3 мая в 10:00" → interval_minutes=0, times=1, start_at_iso=03.05.{year}T10:00:00
- день без точного часа ("с пятницы", "с завтра") → время 09:00 того дня
- Только JSON, без пояснений."""


async def extract_reminder_params(task_title: str, task_deadline_iso: str | None, text: str) -> tuple[dict | None, str]:
    """Для сценария «выбрал существующую задачу в меню → пишет, как часто
    напоминать» (remind_command). Раньше — Groq (ask_claude_fast, несмотря
    на название), теперь тот же надёжный путь, что и весь остальной модуль."""
    now = datetime.datetime.now(tz=UFA_TZ)
    deadline_hint = "Дедлайн задачи не указан."
    if task_deadline_iso:
        dl = _parse_iso(task_deadline_iso)
        if dl:
            days = (dl.date() - now.date()).days
            deadline_hint = f"Дедлайн задачи: {dl.strftime('%d.%m.%Y %H:%M')} (через {days} дн.)."
    prompt = REMINDER_PARAMS_PROMPT_TEMPLATE.format(
        task_title=task_title, deadline_hint=deadline_hint,
        now_str=now.strftime("%d.%m.%Y"), weekday=_DAYS_RU[now.weekday()],
        time_str=now.strftime("%H:%M"), text=text, year=now.year,
    )
    return await _call_and_parse(prompt, timeout=60, purpose="bot_reminder_params")


async def handle_free_text(update, context, text: str) -> None:
    """Единая точка входа для свободного текста — заменяет прежнюю цепочку
    (обязательный вопрос "задача/напоминание/вопрос?" → регексы → Groq →
    ещё одно уточнение кнопкой). Один вызов Клода, действие сразу, без
    обязательных промежуточных кликов — уточняем кнопкой только если сама
    модель не уверена."""
    from bot.messages import _esc_md

    # Долговременная память разговора (data/agent.db) — вместо --resume сессии.
    from agent_db import add_dialog_message, record_event
    user_event_id = record_event("user", "user_said", None, body=text)
    add_dialog_message("user", text)

    placeholder = await update.message.reply_text("🤔 секунду...")
    data, err = await extract_intent(text)
    if err or not data or not data.get("items"):
        await placeholder.edit_text(
            f"❌ Не понял, попробуй переформулировать{f' ({err})' if err else ''}."
        )
        return

    replies = []
    for item in data["items"]:
        itype = item.get("type")
        title = (item.get("title") or text).strip()
        if title:
            title = title[0].upper() + title[1:]
        confidence = item.get("confidence", "high")

        if itype == "complete":
            # Завершение необратимо (для reminder_only удаляет напоминания
            # целиком) — поэтому, в отличие от создания задач/напоминаний
            # (где спрашиваем только при низкой уверенности), здесь ВСЕГДА
            # подтверждение кнопкой, даже если модель уверена: показываем
            # найденную задачу и её дедлайн, чтобы по дедлайну можно было
            # на глаз проверить "это точно то самое" — сам ИИ не закрывает
            # ничего без явного клика "Да".
            if item.get("task_id"):
                await _confirm_completion(update, context, item["task_id"])
            else:
                await _ask_which_to_complete(update, context, title)
            continue

        if itype == "commitment":
            # Обещание — только кандидат: в «открытые» попадает лишь после
            # явного «Да» (решение ревью — модель не создаёт фактов сама).
            # Предлагаем при любой уверенности модели: подтверждение всё равно
            # ручное, а молча пропустить настоящее обещание хуже лишней кнопки.
            # answer_text модели не показываем — там бывает «Записал», хотя
            # ничего ещё не записано.
            await _propose_commitment(update, title, item.get("quote") or text,
                                      item.get("deadline_iso"), user_event_id)
            continue

        if confidence == "low":
            await _ask_clarification(update, context, item, text)
            continue

        if itype == "task":
            task = create_task(title, item.get("deadline_iso"))
            dl = _parse_iso(item.get("deadline_iso"))
            dl_str = f" — {dl.strftime('%d.%m.%Y')}" if dl else ""
            replies.append(f"✅ Задача: <b>{_esc_md(task['title'])}</b>{dl_str}")

        elif itype == "reminder":
            task, rem_dt = create_one_off_reminder(title, item.get("reminder_at_iso"), item.get("event_date_iso"))
            time_fmt = rem_dt.strftime("%H:%M") if rem_dt.date() == datetime.datetime.now(tz=UFA_TZ).date() else rem_dt.strftime("%d.%m %H:%M")
            replies.append(f"🔔 Напомню в {time_fmt}: <b>{_esc_md(title)}</b>")

        elif itype == "recurring_reminder":
            task, reminder = create_recurring_reminder(
                title, item.get("interval_minutes"), item.get("times"), item.get("start_at_iso"),
                item.get("event_date_iso"),
            )
            from reminders import format_interval
            times_left = reminder.get("times_left", 1)
            times_str = "бессрочно" if times_left >= UNLIMITED_TIMES else f"{times_left} раз"
            interval_str = format_interval(reminder.get("interval_minutes", 0), times_left)
            replies.append(f"🔔 <b>{_esc_md(title)}</b> — {interval_str}, {times_str}")

        elif itype in ("question", "none"):
            answer = (item.get("answer_text") or "").strip()
            if answer:
                replies.append(answer)

    if replies:
        await placeholder.edit_text("\n\n".join(replies), parse_mode="HTML")
        add_dialog_message("assistant", "\n\n".join(replies))
    else:
        await placeholder.delete()


async def _propose_commitment(update, action: str, quote: str, due_iso: str | None, event_id) -> None:
    from html import escape
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from agent_db import add_commitment_candidate
    due = _parse_iso(due_iso)
    cid = add_commitment_candidate(action, quote, due.isoformat() if due else None, None, 0.9, event_id)
    due_str = f" до <b>{due.strftime('%d.%m')}</b>" if due else ""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📌 Да, записать", callback_data=f"cm:{cid}:open"),
        InlineKeyboardButton("✖️ Нет", callback_data=f"cm:{cid}:rejected"),
    ]])
    await update.message.reply_text(
        f"Записать как твоё обещание: <b>{escape(action)}</b>{due_str}?\n<i>«{escape(quote[:200])}»</i>\n"
        "Наставник будет за ним следить и спросит, если срок пройдёт.",
        reply_markup=kb, parse_mode="HTML",
    )


async def _ask_clarification(update, context, item: dict, original_text: str) -> None:
    """Только когда модель сама пометила confidence=low — не гадаем молча,
    но и не спрашиваем по умолчанию на каждое сообщение, как было раньше."""
    import time as _time
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.messages import _esc_md

    token = f"si_{int(_time.time() * 1000) & 0xFFFFFF}"
    context.user_data[token] = {"item": item, "text": original_text}
    title = item.get("title") or original_text[:60]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Задача", callback_data=f"si_task:{token}"),
        InlineKeyboardButton("🔔 Напоминание", callback_data=f"si_reminder:{token}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"si_cancel:{token}"),
    ]])
    await update.message.reply_text(
        f"🤔 Не уверен, что нужно — <b>{_esc_md(title)}</b>?",
        reply_markup=kb, parse_mode="HTML",
    )


def _deadline_suffix(task: dict) -> str:
    """Короткая подпись дедлайна для кнопки/подтверждения — по ней и
    предлагается на глаз проверить, та ли это задача, прежде чем жать Да."""
    dl = _parse_iso(task.get("deadline"))
    if not dl:
        return ""
    return f" ({dl.strftime('%d.%m')})"


async def _ask_which_to_complete(update, context, query_title: str) -> None:
    """Модель не нашла уверенно, какую именно задачу/напоминание завершить —
    локальный нечёткий поиск по названию (без нового вызова Клода) и выбор
    кнопками, с дедлайном на кнопке — чтобы не закрыть не то по ошибке."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.messages import _esc_md

    candidates = find_candidate_tasks(query_title)
    if not candidates:
        await update.message.reply_text(
            f"🤔 Не нашёл активную задачу или напоминание «{_esc_md(query_title)}» — "
            "может, уже завершено или называется иначе?",
            parse_mode="HTML",
        )
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"✅ {c['title'][:35]}{_deadline_suffix(c)}", callback_data=f"si_done:{c['id']}")] for c in candidates]
        + [[InlineKeyboardButton("❌ Ничего из этого", callback_data="si_done_none")]]
    )
    await update.message.reply_text("🤔 Какую именно завершить?", reply_markup=kb)


async def _confirm_completion(update, context, task_id: str) -> None:
    """Модель уверенно нашла задачу/напоминание — но само завершение
    необратимо (для reminder_only удаляет напоминания целиком), поэтому
    всегда спрашиваем подтверждение с названием и дедлайном, а не закрываем
    молча. Действие выполняется только по клику Да (si_done:<id>)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.messages import _esc_md
    from storage import get_tasks

    task = next((t for t in get_tasks() if str(t["id"]) == str(task_id)), None)
    if not task or task.get("done"):
        await update.message.reply_text("🤔 Не нашёл эту задачу — возможно, уже завершена.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, закрыть", callback_data=f"si_done:{task_id}"),
        InlineKeyboardButton("❌ Нет", callback_data="si_done_none"),
    ]])
    await update.message.reply_text(
        f"Закрыть «<b>{_esc_md(task['title'])}</b>»{_esc_md(_deadline_suffix(task))}?",
        reply_markup=kb, parse_mode="HTML",
    )
