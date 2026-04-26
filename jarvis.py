#!/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python
"""
jarvis.py — голосовой ассистент Джарвис
"""

import asyncio
import datetime
import hashlib
import json
import os
import re
import tempfile
import random
import httpx
import urllib.request
import urllib.parse



from config import GROQ_KEYS, YANDEX_TTS_KEY, TELEGRAM_TOKEN, MY_TELEGRAM_ID

YANDEX_API_KEY = YANDEX_TTS_KEY
YANDEX_VOICE = "ermil"
SPOKEN_DB = "data/jarvis_spoken.json"
MISSED_DB = "data/jarvis_missed.json"
INTERVAL_BETWEEN_MESSAGES = 15
YANDEX_CHARS_FILE = "data/yandex_chars.json"
YANDEX_CHARS_LIMIT = 1_000_000
YANDEX_WARN_THRESHOLD = 5000
TELEGRAM_BOT_TOKEN = TELEGRAM_TOKEN
TELEGRAM_CHAT_ID = MY_TELEGRAM_ID

def _load_chars() -> int:
    try:
        with open(YANDEX_CHARS_FILE) as f:
            return json.load(f).get("used", 0)
    except Exception:
        return 0

def _save_chars(used: int):
    os.makedirs("data", exist_ok=True)
    with open(YANDEX_CHARS_FILE, "w") as f:
        json.dump({"used": used}, f)

def _add_chars(n: int):
    used = _load_chars() + n
    _save_chars(used)
    remaining = YANDEX_CHARS_LIMIT - used
    print(f"Jarvis: Яндекс использовано {used} симв., осталось {remaining}")
    if remaining <= YANDEX_WARN_THRESHOLD:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_warn_chars_low(remaining))
        except Exception:
            pass

async def _warn_chars_low(remaining: int):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ Jarvis: заканчиваются символы Яндекс TTS! Осталось {remaining} из {YANDEX_CHARS_LIMIT}."}
            )
    except Exception as e:
        print(f"Jarvis: не удалось отправить предупреждение: {e}")
UFA_TZ = datetime.timezone(datetime.timedelta(hours=5))

REBOOT_PHRASES = [
    "Сэр, система легла отдохнуть. Видимо, снова Роскомнадзор балуется.",
    "Ильнур, я ненадолго отключался. Плановая медитация завершена.",
    "Сэр, был небольшой технический перерыв. Серверы решили поиграть в молчанку.",
    "Ильнур, я вернулся. Кто-то явно дёрнул не тот рубильник.",
    "Сэр, система перезагружена. Питон решил взять незапланированный выходной.",
    "Ильнур, небольшой краш — ничего серьёзного. Баги побеждены, хотя некоторые ещё сопротивляются.",
    "Сэр, я снова в строю. Роскомнадзор проигрывает со счётом один-ноль.",
]

SYSTEM_PROMPT = """Ты — голосовой ассистент Джарвис (JARVIS из Iron Man). Преобразуй входящее сообщение в живую, человечную голосовую фразу. ВАЖНО: твой ответ будет озвучен через Яндекс SpeechKit TTS голосом Ermil. Яндекс хорошо читает обычный русский текст. Строго соблюдай правила ниже чтобы речь звучала естественно и без заиканий.

ЖЁСТКИЕ ПРАВИЛА:
- Ответ содержит ТОЛЬКО саму фразу. Никаких пояснений, комментариев, вариантов.
- ЗАПРЕЩЕНО начинать с: "Конечно", "Вот", "Понял", "Хорошо", "Рад помочь".
- ЗАПРЕЩЕНО добавлять советы или вопросы в конце.
- ЗАПРЕЩЕНО использовать символы: *, _, #, `, ~, №
- От 2 до 5 предложений. Максимум 400 символов в ответе — Яндекс режет длинные тексты.
- Говори живо и по-человечески, не как робот читающий список.
- ЗАПРЕЩЕНО использовать кавычки-ёлочки «» — Яндекс читает их как отдельные звуки.

ИМЯ:
- Хозяина зовут Ильнур (ударение на У — пиши именно "Ильнур" с заглавной У).
- Чередуй "сэр" и "Ильнур" естественно по ситуации.

УДАРЕНИЯ — КРИТИЧЕСКИ ВАЖНО ДЛЯ TTS:
- Ставь ударение заглавной буквой на ударном слоге: "программИрование", "матемАтика", "дедлАйн", "расписАние", "дискрЕтная", "алгорИтмы", "сетЕй", "прошлА", "началАсь", "закОнчилась".
- Ставь ударения во всех словах длиннее 3 слогов и во всех словах где возможна неоднозначность.

ЧИСЛА И СПЕЦИАЛЬНЫЕ КОНСТРУКЦИИ — обязательно переводи в слова:
- Дроби с точкой: "22.5" → "двадцать два с половиной", "3.5" → "три с половиной"
- Оценки: "4" → "четвёрку", "5" → "пятёрку", "3" → "тройку"
- Время: "9:30" → "в девять тридцать", "15:55" → "в без пяти четыре", "13:00" → "в час дня", "14:30" → "в два часа тридцать минут", "2:30" → "в два часа тридцать минут дня"
- Даты: "17 апреля" → "семнадцатого апреля"
- Проценты: "85%" → "восемьдесят пять процентов"
- Сокращения: "лаб." → "лабораторная", "С++" → "си плюс плюс", "ЛР" → "лабораторная работа"
- Ссылки (http/https/vk.com) → не читай вслух, замени на "ссылка в боте"
- МСК время (UTC+3) → ОБЯЗАТЕЛЬНО прибавь 2 часа для Уфы (UTC+5). Примеры: 15:40 МСК = 17:40 Уфа, 19:00 МСК = 21:00 Уфа.
- АЛГОРИТМ: сначала переведи ВСЕ времена из МСК в Уфу, ПОТОМ сравнивай с текущим временем.
- Пара длится 90 минут. Статус: если время_начала_уфа > текущее → БУДЕТ. Если текущее > время_начала_уфа + 90 мин → ПРОШЛА. Иначе → ИДЁТ.
- Пример: сейчас 16:20 Уфа, пара в 15:40 МСК = 17:40 Уфа → пара ЕЩЁ БУДЕТ через час двадцать.

ПАРЫ И РАСПИСАНИЕ:
- Тебе передаётся точное текущее время в Уфе (UTC+5). Используй его.
- Время пары < текущего → ПРОШЛА: "сегодня прошлА пара по..."
- Время пары ±15 минут → ИДЁТ СЕЙЧАС: "прямо сейчАс у тебя..."
- Время пары > текущего → БУДЕТ: "в ... у тебя будет..."
- НИКОГДА не говори просто "в 15:55 пара" — только с контекстом.
- ВАЖНО: если в расписании время указано как МСК (московское) — прибавь 2 часа чтобы перевести в Уфу (UTC+5). Например 15:40 МСК = 17:40 Уфа.
- Пара длится 90 минут. Если пара началась меньше 90 минут назад — она ИДЁТ СЕЙЧАС.

ССЫЛКИ И ВК РАСПИСАНИЕ:
- НИКОГДА не читай ссылки вслух (http, https, vk.com и т.д.)
- НИКОГДА не упоминай аудитории и кабинеты (П-12, П-02 и т.д.) — все занятия онлайн.
- НИКОГДА не перечисляй преподавателей если пар несколько.
- Для ВК расписания: просто перечисли названия пар и время по Уфе, статус (будет/идёт/прошла).
- В конце скажи один раз: "ссылки на занятия отправлены в бот".

УТРЕННИЙ БРИФИНГ (если в тексте есть "Доброе утро, Ильнур"):
- Начни с приветствия: "Доброе утро, Ильнур!"
- Кратко озвучь погоду одним предложением.
- Перечисли пары с их статусом (будет/идёт/прошла).
- Если есть дедлайны — обязательно упомяни.
- Скажи ближайшее задание если есть.
- Закончи мотивирующей фразой. Максимум 5-6 предложений.
- ИГНОРИРУЙ символы рамки: ╔ ╗ ╚ ╝ ═ и разделители ┄

ВЕЧЕРНИЙ БРИФИНГ (если в тексте есть "ВЕЧЕР" или "Итоги дня"):
- Начни с: "Добрый вечер, Ильнур" или "Сэр, итоги дня."
- Кратко: что сделано, что осталось, что завтра.
- Максимум 4-5 предложений.

СТИЛЬ:
- Начинай с фирменной реплики: "Сэр,", "Ильнур,", "Позвольте заметить, сэр,", "Рад сообщить, Ильнур,", "Как всегда, сэр,"
- Лёгкий британский сарказм уместен.
- Упоминай конкретные данные из текста — оценки, предметы, задачи, даты.
- Говори тепло и дружески, как умный друг.
- ИГНОРИРУЙ все символы форматирования: ╔ ╗ ╚ ╝ ═ ┄ * _ # и заглавные заголовки типа "ПАРЫ СЕГОДНЯ" — читай только данные под ними.

КРИТИЧЕСКИ ВАЖНО: используй ТОЛЬКО данные из входящего сообщения. Примеры ниже — только для понимания формата. НИКОГДА не используй предметы, оценки и задачи из примеров в реальных ответах.

Примеры формата:
Текущее время: 23:10. Вход: "Расписание: 15:55 — [ПРЕДМЕТ]"
Выход: Сэр, расписание на сегодня несложное — прошла одна пара по предмету, она была без пяти четыре. Больше пар не предвидится.

Текущее время: 09:00. Вход: "Пары: [ПРЕДМЕТ А] 9:30, [ПРЕДМЕТ Б] 13:00. Срочно: [ПРЕДМЕТ В] до пятницы"
Выход: Доброе утро, Ильнур! Сегодня две пары: первая в девять тридцать и вторая в час дня. Главное не забудь — дедлайн по третьему предмету в пятницу.

Текущее время: 23:10. Вход: "Новая оценка. [ПРЕДМЕТ] — Лаб. №3. Оценка: 4"
Выход: Рад сообщить, Ильнур — по предмету выставили четвёрку за третью лабораторную. Неплохо, хотя могло быть и пятёрка.
"""

SUMMARY_PROMPT = """Ты — голосовой ассистент Джарвис. Пока система была недоступна, накопились сообщения. Составь краткое живое резюме в стиле Джарвиса — что произошло. Максимум шесть предложений. Только конкретные факты. Никаких символов. Обращайся "сэр" или "Ильнур"."""

IGNORED_PHRASES = [
    "Привет, Ильнур", "Anti-Laziness Bot", "Слежу за дедлайнами", "Используй кнопки меню",
    "Английский —", "Теория дня —",
    "ТЕОРИЯ ДНЯ —", "Pre-Intermediate",
    "Спокойной ночи, Ильнур", "Анекдот на ночь",
]

# Фразы которые озвучиваем коротко (не через DeepSeek)
SHORT_BRIEFING_PHRASES = {
    "Доброе утро, Ильнур": "Доброе утро, сэр. Утренний брифинг готов.",
    "Вечер —": "Сэр, вечерний брифинг.",
    "Итоги дня": "Итоги дня готовы, сэр.",
    "МОТИВАЦИЯ": "Сэр, минута мотивации.",
    "ЗАДАНИЯ": "Сэр, сводка по заданиям.",
}

MESSENGER_PHRASES = [
    "Сэр, у вас новое сообщение в Яндекс Мессенджере. Кто-то явно соскучился.",
    "Ильнур, пришло сообщение в мессенджер. Надеюсь, это не очередное задание.",
    "Ильнур, вам написали в мессенджер. Возможно, что-то важное. Или нет.",
    "Сэр, новое сообщение в мессенджере. Игнорировать не рекомендую.",
    "Ильнур, кто-то пишет в Яндекс Мессенджер. Это явно не спам — или всё-таки спам?",
    "Сэр, входящее сообщение в мессенджере. Ваш ход.",
    "Ильнур, мессенджер требует внимания. Всего одно сообщение, обещаю.",
    "Сэр, поступило сообщение. Яндекс не даст вам скучать.",
    "Ильнур, новое сообщение в мессенджере. Не заставляйте человека ждать.",
    "Сэр, входящее. Возможно, это важно. Возможно, нет. Но лучше проверить.",
    "Сэр, вас вызывают в мессенджер. Явка обязательна.",
]

MAIL_PHRASES = [
    "Сэр, пришло новое письмо на Яндекс Почту. Возможно, важное.",
    "Ильнур, у вас новое письмо. Надеюсь, не от деканата.",
    "Сэр, входящая почта. Кто-то потрудился написать вам письмо.",
    "Сэр, новое письмо в почтовом ящике. Рекомендую не откладывать.",
    "Ильнур, почта пришла. Это не счёт за электричество — точно.",
    "Ильнур, вам написали на почту. Классика жанра.",
    "Ильнур, почтальон не подвёл — письмо доставлено.",
    "Ильнур, почта не дремлет — новое письмо уже ждёт.",
    "Сэр, почтовый ящик пополнился. Рекомендую не затягивать.",
    "Ильнур, письмо пришло. Надеюсь, не очередная рассылка.",
    "Сэр, Яндекс доставил письмо. Осталось только прочитать.",
    "Ильнур, почта принесла весточку. Проигнорировать будет неловко.",
    "Сэр, письмо в ящике. Отправитель старался — оцените.",
]

PHRASES_INDEX_FILE = "data/jarvis_phrases_index.json"

def _get_next_phrase(phrases: list, key: str) -> str:
    """Возвращает фразы по очереди, не рандомно."""
    try:
        with open(PHRASES_INDEX_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    idx = data.get(key, 0)
    phrase = phrases[idx % len(phrases)]
    data[key] = (idx + 1) % len(phrases)
    os.makedirs("data", exist_ok=True)
    with open(PHRASES_INDEX_FILE, "w") as f:
        json.dump(data, f)
    return phrase

def _get_short_phrase(text: str) -> str:
    """Для почты и мессенджера возвращаем фразу по очереди."""
    # Почта и мессенджер — только если явно указан заголовок
    if "Яндекс Почта" in text:
        return _get_next_phrase(MAIL_PHRASES, "mail")
    if "Яндекс Мессенджер" in text:
        return _get_next_phrase(MESSENGER_PHRASES, "messenger")
    return None

def _is_quiet_hours() -> bool:
    now = datetime.datetime.now(UFA_TZ)
    return now.hour >= 22 or now.hour < 8

def _is_ignored(text):
    return any(p in text for p in IGNORED_PHRASES)

def _convert_msk_to_ufa(text: str) -> str:
    """Находим все времена с пометкой МСК и конвертируем в Уфу (+2 часа)."""
    import re
    def replace_time(m):
        h, minute = int(m.group(1)), int(m.group(2))
        h_ufa = (h + 2) % 24
        return f"{h_ufa:02d}:{minute:02d} (Уфа)"
    # Заменяем форматы: 15:40 (мск), 15:40(мск), 15:40 мск
    text = re.sub(r'(\d{1,2}):(\d{2})\s*\(?мск\)?', replace_time, text, flags=re.IGNORECASE)
    return text

def _ufa_now_str():
    now = datetime.datetime.now(datetime.timezone.utc).astimezone(UFA_TZ)
    months = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
    return f"{now.strftime('%H:%M')}, {now.day} {months[now.month-1]} {now.year}г, Уфа UTC+5"

def _msg_hash(text):
    return hashlib.md5(text.strip().encode()).hexdigest()

def _load_spoken():
    try:
        with open(SPOKEN_DB) as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_spoken(spoken):
    os.makedirs("data", exist_ok=True)
    with open(SPOKEN_DB, "w") as f:
        json.dump(list(spoken)[-500:], f)

def _already_spoken(text):
    return _msg_hash(text) in _load_spoken()

def _mark_spoken(text):
    spoken = _load_spoken()
    spoken.add(_msg_hash(text))
    _save_spoken(spoken)

def _load_missed():
    try:
        with open(MISSED_DB) as f:
            return json.load(f)
    except Exception:
        return []

def _save_missed(missed):
    os.makedirs("data", exist_ok=True)
    with open(MISSED_DB, "w") as f:
        json.dump(missed, f, ensure_ascii=False)

def _clear_missed():
    _save_missed([])

def _remove_stress_caps(text: str) -> str:
    """Убираем заглавные буквы-ударения внутри слов — Яндекс читает их как новое слово."""
    import re
    # Сначала фиксим имя
    text = re.sub(r'Ильнур', 'Ильнур', text)
    text = re.sub(r'ильнУр', 'ильнур', text)
    # Затем убираем заглавные внутри слов начинающихся со строчной
    result = []
    for word in text.split():
        clean = re.sub(r'(?<=[а-яёa-z])[А-ЯЁA-Z]', lambda m: m.group().lower(), word)
        result.append(clean)
    return ' '.join(result)

def _preprocess_for_tts(text):
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)
    text = re.sub(r"_+([^_]+)_+", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"~([^~]+)~", r"\1", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r'[^\w\s\.,!?;:\-\(\)«»"\'ёЁа-яА-ЯіІїЇєЄ—–]', ' ', text)
    text = re.sub(r'[—–]', ', ', text)
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\n{2,}', '. ', text)
    text = text.replace('\n', ', ')
    return text.strip()

_key_index = 0

async def _ask_deepseek(text, system=None):
    global _key_index
    if system is None:
        system = SYSTEM_PROMPT
    for attempt in range(len(GROQ_KEYS)):
        key = GROQ_KEYS[_key_index % len(GROQ_KEYS)]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": "deepseek/deepseek-chat",
                        "max_tokens": 350,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"Текущее время в Уфе (UTC+5): {_ufa_now_str()}\n\n{text}"},
                        ],
                    },
                )
            if resp.status_code in (429, 402):
                _key_index += 1
                continue
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            print(f"Jarvis: DeepSeek → {result[:120]}")
            return result
        except httpx.HTTPStatusError:
            _key_index += 1
            continue
        except Exception as e:
            print(f"Jarvis: ошибка DeepSeek: {e}")
            break
    return _preprocess_for_tts(text[:500])

async def _speak(text):
    text = _preprocess_for_tts(text)
    text = _remove_stress_caps(text)
    for attempt in range(3):
        tmp_path = None
        try:
            url = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
            data = urllib.parse.urlencode({
                "text": text,
                "lang": "ru-RU",
                "voice": YANDEX_VOICE,
                "format": "mp3",
                "speed": "1.0",
            }).encode()
            req = urllib.request.Request(url, data=data, headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"})
            loop = asyncio.get_event_loop()
            audio = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15).read())
            _add_chars(len(text))
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
                f.write(audio)
            # Воспроизводим через pygame — не трогаем системную громкость
            import pygame.mixer as _mixer
            loop = asyncio.get_event_loop()
            def _play():
                try:
                    _mixer.init(frequency=44100)
                    _mixer.music.load(tmp_path)
                    _mixer.music.set_volume(0.85)
                    _mixer.music.play()
                    import time
                    while _mixer.music.get_busy():
                        time.sleep(0.1)
                    _mixer.music.stop()
                    _mixer.quit()
                except Exception as _e:
                    print(f"pygame play error: {_e}")
            await loop.run_in_executor(None, _play)
            os.unlink(tmp_path)
            return
        except Exception as e:
            print(f"Jarvis: ошибка воспроизведения (попытка {attempt+1}): {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if attempt < 2:
                await asyncio.sleep(3)

_queue = asyncio.Queue()
_worker_started = False

async def _worker():
    global _worker_started
    if _worker_started:
        print("Jarvis: worker уже запущен, пропускаю дубль")
        return
    _worker_started = True
    print("Jarvis: worker запущен")
    while True:
        text = await _queue.get()
        try:
            if _already_spoken(text):
                print("Jarvis: дубликат, пропускаю")
                continue
            short = _get_short_phrase(text)
            if short:
                await _speak(short)
                _mark_spoken(text)
            else:
                if "ВКонтакте" in text or "(мск)" in text.lower():
                    text_for_ai = _convert_msk_to_ufa(text)
                else:
                    text_for_ai = text
                jarvis_text = await _ask_deepseek(text_for_ai)
                if jarvis_text:
                    await _speak(jarvis_text)
                    _mark_spoken(text)
            if not _queue.empty():
                await asyncio.sleep(INTERVAL_BETWEEN_MESSAGES)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Jarvis worker error: {e}")
        finally:
            _queue.task_done()

async def _handle_missed(missed):
    print(f"Jarvis: озвучиваю {len(missed)} пропущенных")
    await _speak(random.choice(REBOOT_PHRASES))
    await asyncio.sleep(3)
    combined = "\n---\n".join(missed)
    summary = await _ask_deepseek(f"Пока система была недоступна, пришли сообщения:\n\n{combined}", system=SUMMARY_PROMPT)
    if summary:
        await _speak(summary)
    for m in missed:
        _mark_spoken(m)
    _clear_missed()

JARVIS_QUEUE_FILE = "data/jarvis_queue.json"


def _read_jarvis_queue() -> list:
    """Читаем очередь сообщений от бота."""
    try:
        with open(JARVIS_QUEUE_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _clear_jarvis_queue():
    """Очищаем очередь после прочтения."""
    try:
        with open(JARVIS_QUEUE_FILE, "w") as f:
            json.dump([], f)
    except Exception:
        pass


async def _poll_queue():
    """Читаем файл-очередь каждые 2 секунды."""
    print("Jarvis: слушаю бота... (Ctrl+C для остановки)")
    while True:
        try:
            queue = _read_jarvis_queue()
            if queue:
                _clear_jarvis_queue()
                for item in queue:
                    text = item.get("text", "")
                    if not text or len(text.strip()) < 5:
                        continue
                    if _is_ignored(text):
                        continue
                    if _is_quiet_hours():
                        print("Jarvis: тихий режим, пропускаю")
                        continue
                    if _already_spoken(text):
                        print("Jarvis: дубликат, пропускаю")
                        continue
                    print(f"Jarvis: новое сообщение ({len(text)} симв.) → в очередь")
                    await _queue.put(text)
        except Exception as e:
            print(f"Jarvis: poll error: {e}")
        await asyncio.sleep(2)


async def main():
    print("Jarvis: запуск...")
    os.makedirs("data", exist_ok=True)
    asyncio.create_task(_worker())
    await _poll_queue()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nJarvis: выключаюсь.")
