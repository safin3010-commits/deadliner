"""
Клиент OpenRouter для ДедЛайнер.
"""

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_FAST = "google/gemini-2.0-flash-exp:free"   # автовыбор лучшей доступной модели
MODEL_FAST_FALLBACK = "google/gemini-2.0-flash-exp:free"        # запасная бесплатная
MODEL_SMART = "google/gemini-2.0-flash-exp:free"  # для теории и сложных задач

from config import OPENROUTER_KEYS
_current_key_idx = 0

def _build_system_prompt() -> str:
    import datetime
    from config import UFA_TZ
    now = datetime.datetime.now(tz=UFA_TZ)
    MONTHS = ["январе","феврале","марте","апреле","мае","июне","июле","августе","сентябре","октябре","ноябре","декабре"]
    month_name = MONTHS[now.month - 1]
    year = now.year
    # Определяем семестр: сентябрь-январь = 1й, февраль-июнь = 2й
    semester = 1 if now.month >= 9 or now.month == 1 else 2
    # Сессия: 1й семестр — январь, 2й семестр — июнь
    session_month = "январе" if semester == 1 else "июне"
    from config import USER_NAME, USER_CITY
    user_name = USER_NAME
    city_str = f", {USER_CITY}" if USER_CITY else ""
    return (
        f"Ты — персональный антилень-ассистент студента {user_name}.\n"
        f"Контекст: 1 курс, {semester} семестр, направление ИСиТ, ТюмГУ совместно с Нетологией, онлайн-обучение{city_str}.\n"
        f"Сейчас {month_name} {year} года — середина семестра. Сессия будет только в {session_month}. Не упоминай сессию раньше времени.\n"
        f"Твоя задача — мотивировать и помогать не лениться.\n"
        f"Пиши по-русски, неформально, как умный друг. Без воды. Максимум 2-3 коротких предложения.\n"
        f"Используй эмодзи умеренно. Опирайся только на реальные задачи и дедлайны которые тебе передают.\n"
        f"Никаких выдуманных фактов. Никаких скобок с пояснениями. Никаких вариантов в конце. Только русские слова."
    )

SYSTEM_PROMPT = _build_system_prompt()


async def ask_grok(prompt: str, system: str = None, smart: bool = False) -> str:
    """Сначала пробуем Groq (быстро), потом OpenRouter как фоллбэк."""
    if system is None:
        system = _build_system_prompt()

    # Сначала Groq
    result = await _ask_groq_fallback(prompt, system)
    if result:
        return result

    # Фоллбэк — OpenRouter
    global _current_key_idx
    model = MODEL_SMART if smart else MODEL_FAST
    max_tokens = 4000 if smart else 1200
    print(f"OpenRouter: модель {model}")

    for attempt in range(len(OPENROUTER_KEYS)):
        key = OPENROUTER_KEYS[_current_key_idx]
        try:
            async with httpx.AsyncClient(timeout=120 if smart else 30) as client:
                r = await client.post(
                    OPENROUTER_URL,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": 0.6 if smart else 0.7,
                    }
                )

                if r.status_code in (429, 402, 403):
                    print(f"OpenRouter: ключ {_current_key_idx + 1} исчерпан ({r.status_code}), переключаемся...")
                    _current_key_idx = (_current_key_idx + 1) % len(OPENROUTER_KEYS)
                    continue

                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"] or ""
                return content.strip()

        except Exception as e:
            if any(code in str(e) for code in ["429", "402", "403"]):
                print(f"OpenRouter: ключ {_current_key_idx + 1} ошибка, переключаемся...")
                _current_key_idx = (_current_key_idx + 1) % len(OPENROUTER_KEYS)
                continue
            print(f"OpenRouter API error: {e}")

    print("Все AI исчерпаны")
    return ""


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_FAST = "llama-3.3-70b-versatile"
_groq_key_idx = 0

async def _ask_groq_fallback(prompt: str, system: str) -> str:
    """Фоллбэк на Groq когда все OpenRouter ключи исчерпаны."""
    global _groq_key_idx
    from config import GROQ_KEYS
    if not GROQ_KEYS:
        return ""
    for attempt in range(len(GROQ_KEYS)):
        key = GROQ_KEYS[_groq_key_idx % len(GROQ_KEYS)]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": GROQ_MODEL_FAST,
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
                print(f"Groq: ответ получен ✅")
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
    schedule_text = ""
    if schedule:
        pairs = []
        for lesson in schedule:
            name = lesson.get("course_name") or lesson.get("name", "")
            time = lesson.get("start_time", "")
            pairs.append(f"{time} — {name}")
        schedule_text = "Сегодня пары:\n" + "\n".join(pairs)
    else:
        schedule_text = "Сегодня пар нет."
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
    prompt = f"""Сегодня {now.strftime('%d.%m.%Y')}, {['понедельник','вторник','среда','четверг','пятница','суббота','воскресенье'][now.weekday()]}.

{schedule_text}

{tasks_text}

{streak_text}

Напиши короткий утренний план: что делать в первую очередь, на что обратить внимание, мотивирующая фраза в конце. Будь конкретным."""
    return await ask_grok(prompt)


async def grok_evening_analysis(report: str, tasks_done_today: int, tasks_total: int, streak: int) -> str:
    prompt = f"""Пользователь отчитался о дне: "{report}"
Задач выполнено сегодня: {tasks_done_today}
Всего незакрытых: {tasks_total}
Стрик: {streak} дн.
Дай честную оценку продуктивности (1-10), прокомментируй что хорошо и что улучшить. Мотивирующая фраза на завтра. Коротко."""
    return await ask_grok(prompt)


async def grok_weekly_report(tasks_done: int, tasks_total: int, streak: int, best_day: str, completed_courses: list) -> str:
    completed_text = ", ".join(completed_courses[:5]) if completed_courses else "ничего"
    prompt = f"""Недельный отчёт:
- Выполнено: {tasks_done} из {tasks_total} задач ({int(tasks_done/max(tasks_total,1)*100)}%)
- Стрик: {streak} дн.
- Лучший день: {best_day}
- Закрытые предметы: {completed_text}
Краткий итог недели: что хорошо, что плохо, один совет на следующую неделю. 3-4 предложения."""
    return await ask_grok(prompt)


async def parse_date_with_groq(text: str) -> str | None:
    import datetime, re
    from config import UFA_TZ
    now = datetime.datetime.now(tz=UFA_TZ)
    today_str = now.strftime("%d.%m.%Y")
    weekday = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"][now.weekday()]
    prompt = f"""Сегодня {today_str} ({weekday}). Пользователь написал дату: "{text}"
Преобразуй в формат DD.MM.YYYY. Год {now.year} если не указан.
Ответь ТОЛЬКО датой DD.MM.YYYY или словом "нет"."""
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
    prompt = f"Задача: \"{title}\"\nПредмет: {course}\nСократи до 3-6 слов, сохрани суть. Только название без кавычек."
    result = await ask_grok(prompt, system="Ты редактор названий задач. Отвечай только кратким названием.")
    if result and len(result) < 80:
        return result.strip().strip('"').strip("'")
    return title


async def beautify_message(sender: str, content: str, source: str = "letter") -> str:
    if not content or len(content) < 20:
        return content
    prompt = f"Отправитель: {sender}\nТекст: {content[:800]}\nУбери лишнее, сохрани смысл. Максимум 300 символов. Только текст."
    result = await ask_grok(prompt, system="Ты редактор текста. Отвечай только отредактированным текстом. Никакого Markdown, никаких звёздочек, никаких символов форматирования.")
    return result if result else content
