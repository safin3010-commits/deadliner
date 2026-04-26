"""
AI клиент для ДедЛайнер — Groq (llama-3.3-70b-versatile).
"""
import httpx
import datetime

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

from config import GROQ_KEYS
_groq_key_idx = 0


def _build_system_prompt() -> str:
    from config import UFA_TZ, USER_NAME, USER_CITY
    now = datetime.datetime.now(tz=UFA_TZ)
    MONTHS = ["январе","феврале","марте","апреле","мае","июне","июле","августе","сентябре","октябре","ноябре","декабре"]
    month_name = MONTHS[now.month - 1]
    year = now.year
    semester = 1 if now.month >= 9 or now.month == 1 else 2
    session_month = "январе" if semester == 1 else "июне"
    city_str = f", {USER_CITY}" if USER_CITY else ""
    return (
        f"Ты — персональный антилень-ассистент студента {USER_NAME}.\n"
        f"Контекст: 1 курс, {semester} семестр, направление ИСиТ, ТюмГУ совместно с Нетологией, онлайн-обучение{city_str}.\n"
        f"Сейчас {month_name} {year} года — середина семестра. Сессия будет только в {session_month}. Не упоминай сессию раньше времени.\n"
        f"Твоя задача — мотивировать и помогать не лениться.\n"
        f"Пиши по-русски, неформально, как умный друг. Без воды. Максимум 2-3 коротких предложения.\n"
        f"Используй эмодзи умеренно. Опирайся только на реальные задачи и дедлайны которые тебе передают.\n"
        f"Никаких выдуманных фактов. Никаких скобок с пояснениями. Только русские слова."
    )


async def ask_grok(prompt: str, system: str = None, smart: bool = False) -> str:
    """Запрос к Groq API."""
    global _groq_key_idx
    if system is None:
        system = _build_system_prompt()
    if not GROQ_KEYS:
        print("AI: нет ключей Groq")
        return ""
    for attempt in range(len(GROQ_KEYS)):
        key = GROQ_KEYS[_groq_key_idx % len(GROQ_KEYS)]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": GROQ_MODEL,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 1200,
                        "temperature": 0.7,
                    }
                )
                if r.status_code in (429, 402, 403):
                    print(f"Groq: ключ {_groq_key_idx + 1} исчерпан, переключаемся...")
                    _groq_key_idx = (_groq_key_idx + 1) % len(GROQ_KEYS)
                    continue
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"] or ""
                return content.strip()
        except Exception as e:
            print(f"Groq error: {e}")
            _groq_key_idx = (_groq_key_idx + 1) % len(GROQ_KEYS)
            continue
    print("Groq: все ключи исчерпаны")
    return ""


async def grok_morning_plan(schedule: list, tasks: list, streak: int) -> str:
    import datetime
    from config import UFA_TZ
    now = datetime.datetime.now(tz=UFA_TZ)
    schedule_text = "Сегодня пар нет."
    if schedule:
        pairs = [f"{l.get('start_time','')} — {l.get('course_name','')}" for l in schedule]
        schedule_text = "Сегодня пары:\n" + "\n".join(pairs)
    urgent_tasks = []
    for t in tasks[:8]:
        deadline = t.get("deadline", "")
        title = t.get("title", "")[:50]
        course = t.get("course_name", "")[:25]
        if deadline:
            try:
                dt = datetime.datetime.fromisoformat(deadline).astimezone(UFA_TZ)
                days = (dt - now).days
                urgent_tasks.append(f"• {course}: {title} (через {days} дн.)")
            except Exception:
                urgent_tasks.append(f"• {course}: {title}")
        else:
            urgent_tasks.append(f"• {course}: {title}")
    tasks_text = "Незакрытые задания:\n" + "\n".join(urgent_tasks) if urgent_tasks else "Заданий нет!"
    streak_text = f"Текущий стрик: {streak} дн." if streak > 0 else "Стрик пока не начат."
    prompt = f"Сегодня {now.strftime('%d.%m.%Y')}.\n{schedule_text}\n{tasks_text}\n{streak_text}\nКороткий утренний план."
    return await ask_grok(prompt)


async def grok_evening_analysis(report: str, tasks_done_today: int, tasks_total: int, streak: int) -> str:
    prompt = f'''Пользователь отчитался: "{report}". Выполнено: {tasks_done_today}, осталось: {tasks_total}, стрик: {streak} дн. Оцени продуктивность (1-10), что улучшить. Коротко.'''
    return await ask_grok(prompt)


async def parse_date_with_groq(text: str) -> str | None:
    import datetime, re
    from config import UFA_TZ
    now = datetime.datetime.now(tz=UFA_TZ)
    prompt = f'''Сегодня {now.strftime("%d.%m.%Y")}. Пользователь написал дату: "{text}". Преобразуй в DD.MM.YYYY. Год {now.year} если не указан. Ответь ТОЛЬКО датой DD.MM.YYYY или словом "нет".'''
    result = await ask_grok(prompt, system="Ты конвертер дат. Отвечай только датой DD.MM.YYYY или словом 'нет'.")
    if not result:
        return None
    result = result.strip().rstrip(".")
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', result):
        try:
            datetime.datetime.strptime(result, "%d.%m.%Y")
            return result
        except ValueError:
            return None
    return None


async def normalize_task_title(title: str, course: str = "") -> str:
    if len(title) <= 40:
        return title
    prompt = f'Задача: "{title}". Предмет: {course}. Сократи до 3-6 слов, сохрани суть. Только название без кавычек.'
    result = await ask_grok(prompt, system="Ты редактор названий задач. Отвечай только кратким названием.")
    if result and len(result) < 80:
        return result.strip().strip('"').strip("'")
    return title


async def beautify_message(sender: str, content: str, source: str = "letter") -> str:
    if not content or len(content) < 20:
        return content
    prompt = f'Отправитель: {sender}. Текст: {content[:800]}. Убери лишнее, сохрани смысл. Максимум 300 символов. Только текст.'
    result = await ask_grok(prompt, system="Ты редактор текста. Отвечай только отредактированным текстом без форматирования.")
    return result if result else content
