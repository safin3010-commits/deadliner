"""
Быстрый Q&A через headless Claude Code — используется кнопкой "💬 Вопрос" в боте.
Тот же принцип, что у scripts/mentor_checkin.py: отдельный процесс claude -p,
только чтение data/*.json, никакого Bash. Не требует отдельного API-ключа —
использует локальную подписку Claude Code.
"""
import asyncio
import datetime
import json
import os
import re
import shutil

from config import USER_NAME, UFA_TZ

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
QA_LOG_FILE = os.path.join(PROJECT_DIR, "data", "mentor_qa_log.json")
QA_LOG_MAX = 20


def _find_claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _load_qa_log() -> list:
    try:
        with open(QA_LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _append_qa_log(question: str, answer: str):
    log = _load_qa_log()
    log.append({
        "at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
        "q": question,
        "a": answer,
    })
    log = log[-QA_LOG_MAX:]
    os.makedirs(os.path.dirname(QA_LOG_FILE), exist_ok=True)
    with open(QA_LOG_FILE, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


async def ask_mentor(question: str) -> str:
    claude_bin = _find_claude_bin()
    if not os.path.isfile(claude_bin):
        return "❌ Не нашёл claude CLI на машине."

    _now = datetime.datetime.now(tz=UFA_TZ)
    _days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    now_str = _now.strftime("%d.%m.%Y") + f" ({_days_ru[_now.weekday()]})"

    # Последние вопросы-ответы — сразу в промпте, без отдельного Read
    # (дёшево и часто избавляет от необходимости лезть в файлы вообще)
    recent = _load_qa_log()[-8:]
    history_block = ""
    if recent:
        lines = [f'- Вопрос: "{r["q"]}" → Ответ: {r["a"][:200]}' for r in recent]
        history_block = (
            "Недавние вопросы и твои ответы — используй ТОЛЬКО для контекста разговора "
            "(чтобы понимать о чём речь, не повторяться в тоне, помнить уточнения):\n"
            + "\n".join(lines) + "\n\n"
            "ВАЖНО: история может быть устаревшей — с тех пор дедлайн могли продлить, задачу "
            "закрыть, оценка могла появиться. НИКОГДА не бери из истории сам факт (дату, статус, "
            "балл, тему пары) — для любого факта всегда проверяй актуальный файл заново, даже "
            "если точно такой же вопрос уже был. История — про \"о чём говорили\", а не \"что там "
            "было\".\n\n"
        )

    prompt = (
        f"Ты — личный наставник студента {USER_NAME} в проекте anti_laziness_bot. "
        f"Сегодня {now_str}. Пользователь задал вопрос в Telegram: \"{question}\"\n\n"
        f"{history_block}"
        "ВАЖНО про data/tasks.json: записи с \"source\": \"reminder_only\" — это НЕ настоящие "
        "дедлайны, а служебная привязка для повторяющихся напоминаний (см. data/reminders.json). "
        "Их поле \"deadline\" не значит \"просрочено\" — напоминание живёт пока не нажата \"Сделано\", "
        "это нормально. Никогда не называй такие записи просроченными дедлайнами.\n\n"
        "Для самого факта, который спрашивают, — используй Grep, чтобы найти нужный кусок "
        "в конкретном файле data/*.json (например по названию предмета), а не читай файл "
        "целиком Read'ом без необходимости — так экономим токены. К полному Read файла "
        "переходи только если Grep недостаточно для ответа. Для вопросов о том, что преподаватель "
        "говорил на вебинаре, обязательно проверь data/study_knowledge.json: там находятся "
        "организационные факты с названием вебинара и таймкодом. Используй только status=active; "
        "needs_review обозначай как неподтверждённое и не превращай в дедлайн. "
        "Если вопрос относится к объяснению SQL, дополнительно проверь data/study_subject_knowledge.json; "
        "не смешивай предметные правила с организационными объявлениями. "
        "Если вопрос относится к содержанию вебинара или видео, сначала найди нужную запись в "
        "data/video_manifest.json, затем читай соответствующий файл из data/video_transcripts/ "
        "(это расшифровки субтитров). Используй их как первичный материал для обучения: объясняй "
        "по порядку, отмечай, где в видео был пример или дополнительное пояснение, и не выдавай "
        "неразобранный большой транскрипт целиком. Если у записи status не downloaded, сообщи, "
        "что субтитры пока недоступны, и не додумывай содержание. "
        "Если пользователь просит начать или продолжить обучение по курсу, перед ответом "
        "обязательно сверь порядок тем в соответствующем файле netology_conspekty/3 семестр ...txt, "
        "в data/video_manifest.json и в обучение_3_семестр/05_прогресс/Карта_прогресса.md. "
        "Сначала назови текущую, последнюю пройденную и следующую тему; не перескакивай через "
        "непроверенную тему. "
        "Для полноценного объяснения сверяй презентацию/силлабус и расшифровку видео: "
        "включай определения, примеры, ограничения, типичные ошибки и дополнительные пояснения "
        "преподавателя. Не выдавай субтитры целиком и не ограничивайся кратким пересказом слайдов; "
        "если тема большая, дели её на последовательные блоки и отмечай, что уже разобрано. "
        "Ответь по существу, коротко и по-человечески, на \"ты\", без канцелярита. "
        "Если в данных нет ответа — так и скажи, не выдумывай.\n\n"
        "Ответь ТОЛЬКО текстом сообщения для Telegram в формате Telegram HTML "
        "(разрешены только теги <b> и <i>, обычные переводы строк для абзацев — "
        "никакого markdown-звёздочек/подчёркиваний). Без вступлений, без описания своих действий. "
        "Не пиши себе под нос фразы вроде \"все данные есть\", \"нашёл файл\", \"отвечаю\" — "
        "сразу начинай с самого ответа по существу."
    )

    # Всё общение с подпроцессом claude в одном try/except — эта функция
    # вызывается прямо из хендлера кнопки в боте, и не должна поднимать
    # исключение наружу ни при каких условиях (нет сети, лимит подписки
    # исчерпан, неожиданная ОС-ошибка запуска процесса и т.п.) — иначе
    # пользователь вместо ответа в чате получит только общий алерт об ошибке.
    import time as _time
    started = _time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "-p", prompt,
            "--allowedTools", "Read,Glob,Grep",
            "--permission-prompts", "none",
            "--model", "claude-sonnet-5",
            "--output-format", "json",
            cwd=PROJECT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return f"❌ Не смог запустить claude ({e})"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        # Не даём процессу повиснуть/доработать в фоне после того, как мы уже
        # сдались — иначе копятся зомби-claude и тратится лимит подписки на
        # ответ, который никто не увидит.
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return "❌ Не успел ответить за 3 минуты, попробуй ещё раз."
    except Exception as e:
        return f"❌ Наставник не смог ответить (ошибка: {e})"

    # Здесь инструменты чтения оставлены сознательно: режим обучения читает
    # расшифровки вебинаров и конспекты — их не уложить заранее в контекст.
    # Вызывается только по запросу пользователя; расход — в agent_calls.jsonl.
    from claude_session import _parse_cli_json, log_agent_call
    text, meta = _parse_cli_json((stdout or b"").decode("utf-8", "ignore"))
    log_agent_call("mentor_qa", "tools", "claude-sonnet-5", meta,
                   bool(text) and proc.returncode == 0, started)
    if not text or proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "ignore")[:300]
        if re.search(r"limit|usage|quota|429", err, re.IGNORECASE):
            reason = "лимит подписки исчерпан"
        elif re.search(r"network|connection|resolve|dns|econnrefused|enotfound|unreachable", err, re.IGNORECASE):
            reason = "нет соединения с сетью"
        else:
            reason = err
        return f"❌ Наставник не смог ответить ({reason})" if reason else "❌ Наставник не смог ответить."

    _append_qa_log(question, text)
    return text
