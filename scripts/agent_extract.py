#!/usr/bin/env python3
"""
Разбор входящих писем и сообщений — пункт 3 плана улучшений.

1. Код отсеивает шум (рассылки, автоуведомления Нетологии о начале вебинара,
   «18:02», короткие реплики, сообщения без учебных слов) — им не платим.
2. Остаток ОДНИМ пакетным вызовом дешёвой модели (Haiku): есть ли срок,
   нужно ли ответить, короткая суть. Результат кэшируется в agent.db
   (extractions) по событию и версии разборщика — повторно не платим.
3. Найденный будущий срок → бот предлагает завести задачу кнопкой
   (сам ничего не создаёт). «Ждут ответа» попадают в сводки наставника.
Запускается из почасового сторожа.
"""
import datetime
import difflib
import json
import os
import re
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
os.chdir(PROJECT_DIR)

from config import UFA_TZ

VERSION = "x3"
BATCH = 25
MAX_PROPOSALS_PER_RUN = 3
LOOKBACK_DAYS = 7

NOISE_SENDERS = re.compile(r"кинопоиск|яндекс\s*(плюс|маркет|музык)|ozon|wildberries|avito|спортмастер|"
                           r"no-?reply|newsletter|рассылк", re.I)
NOISE_SUBJECTS = re.compile(r"запись загружена|через \d+ минут начинаем|занятие скоро начн|доступ к занятию|"
                            r"митап|скидк|акци[яи]|промокод|вебинар начнётся", re.I)
STUDY_WORDS = re.compile(r"срок|дедлайн|до \d{1,2}[.\s]|сдать|сдач|перенос|отмен|экзамен|зач[её]т|контрольн|"
                         r"ответ|прошу|просьба|необходимо|нужно|задани|лаборатор|долг|пересдач|оценк|"
                         r"домашн|тест|проект|защит|отч[её]т|присылайте|отправьте|загрузите|"
                         r"\bhw\b|homework|deadline|\bdue\b|assignment|\bact\s*\d|\bp\.?\s*\d{2,3}\b|unit\s*\d|"
                         # повелительные формы и сроки словами (ревью Codex: «Пришлите работу
                         # завтра» уходило в шум)
                         r"пришл|сдай|сдайте|сделай|загруз|отправ|принес|подготов|напиш|ответь|скинь|"
                         r"завтра|послезавтра|до (понедельн|вторн|сред|четверг|пятниц|суббот|воскресен|конца)", re.I)


def _now():
    return datetime.datetime.now(tz=UFA_TZ)


def is_noise(e) -> bool:
    text = f"{e['title'] or ''} {e['body'] or ''}".strip()
    if len(re.sub(r"[\d:\s]", "", text)) < 15:          # «18:02», «yes», «5»
        return True
    if e["source"] == "mail":
        # Писем мало и они важнее: в шум — только явные рассылки и
        # автоуведомления, остальное всегда смотрит модель.
        return bool(NOISE_SENDERS.search(e["sender"] or "") or NOISE_SUBJECTS.search(e["title"] or ""))
    return not STUDY_WORDS.search(text)


PROMPT = """Сегодня {today}. Ниже входящие письма и сообщения студента (вуз ТюмГУ + онлайн-курсы Нетологии).
Для КАЖДОГО пункта верни объект JSON. Ответ — только JSON-массив, без пояснений:
[{{"id": <номер>, "relevant": true|false, "needs_reply": true|false, "deadline_iso": "YYYY-MM-DD" или null,
  "task_title": "что сделать, коротко" или "", "course": "предмет" или "", "summary": "суть в 1 фразу"}}]
Правила:
- relevant=false для рассылок, флуда в чатах, вопросов одногруппников между собой.
- needs_reply=true только если ЕМУ лично нужно ответить/отреагировать (преподаватель, деканат, куратор просят).
- deadline_iso — только явный срок сдачи/выполнения для НЕГО из текста; относительные даты считай от сегодня.
  Не выдумывай срок. Срок вебинара/начала занятия — не дедлайн.
- task_title — только если есть deadline_iso.

{items}"""


def run(propose: bool = True) -> dict:
    from agent_db import connect
    from claude_session import run_claude_oneshot
    now = _now()
    since = (now - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat(timespec="seconds")
    conn = connect()
    try:
        rows = conn.execute(
            # Новые события + те, что старая версия фильтра отсеяла как шум:
            # при смене VERSION они перепроверяются (раньше версия ни на что
            # не влияла — ревью Codex).
            "SELECT e.* FROM raw_events e LEFT JOIN extractions x ON x.event_id = e.id "
            "WHERE (x.event_id IS NULL OR (x.version != ? AND x.result_json LIKE '%\"skipped\"%')) "
            "AND e.source IN ('mail','messenger','vk','netology_notif') "
            "AND e.observed_at >= ? ORDER BY e.id LIMIT 200", (VERSION, since)).fetchall()
        noise, todo = [], []
        for e in rows:
            (noise if is_noise(e) else todo).append(e)
        with conn:
            for e in noise:
                conn.execute("INSERT OR REPLACE INTO extractions(event_id, version, result_json, created_at) "
                             "VALUES (?,?,?,?)", (e["id"], VERSION, json.dumps({"skipped": "noise"}),
                                                  now.isoformat(timespec="seconds")))
    finally:
        conn.close()

    stats = {"noise": len(noise), "analyzed": 0, "proposals": 0}
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        items = "\n\n".join(
            f"#{e['id']} [{e['source']}, от: {(e['sender'] or '—')[:60]}, {(e['occurred_at'] or e['observed_at'])[:16]}]\n"
            f"{(e['title'] or '')[:200]}\n{(e['body'] or '')[:1200]}" for e in batch)
        text, err = run_claude_oneshot(
            PROMPT.format(today=now.strftime("%Y-%m-%d (%A)"), items=items),
            "Ты извлекаешь факты из писем. Отвечай только валидным JSON.",
            180, "claude-haiku-4-5-20251001", "extract_inbox")
        if err:
            print(f"agent_extract: ошибка модели: {err}")
            break
        m = re.search(r"\[.*\]", text, re.S)
        try:
            results = {int(r["id"]): r for r in json.loads(m.group(0) if m else text)}
        except Exception as ex:
            print(f"agent_extract: не разобрал ответ: {ex!r}")
            break
        conn = connect()
        try:
            with conn:
                for e in batch:
                    res = results.get(e["id"])
                    if res is None:
                        # Модель пропустила пункт (обрезанный ответ) — не
                        # записываем «нерелевантно», разберём в следующий раз.
                        stats.setdefault("missing", 0)
                        stats["missing"] += 1
                        continue
                    conn.execute("INSERT OR REPLACE INTO extractions(event_id, version, result_json, created_at) "
                                 "VALUES (?,?,?,?)", (e["id"], VERSION, json.dumps(res, ensure_ascii=False),
                                                      now.isoformat(timespec="seconds")))
        finally:
            conn.close()
        stats["analyzed"] += len(batch)

    if propose:
        stats["proposals"] = propose_tasks(now)
    return stats


def _already_tracked(title: str, deadline: str) -> bool:
    from storage import get_tasks
    t_low = (title or "").lower()
    for t in get_tasks():
        if t.get("done"):
            continue
        same_day = (t.get("deadline") or "")[:10] == deadline
        sim = difflib.SequenceMatcher(None, t_low, (t.get("title") or "").lower()).ratio()
        if sim > 0.6 or (same_day and sim > 0.35):
            return True
    return False


def propose_tasks(now) -> int:
    """Будущий срок из письма/сообщения → кнопка «завести задачу» в Telegram."""
    import requests
    from html import escape
    from agent_db import connect
    from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT x.event_id, x.result_json, e.source, e.sender, e.title FROM extractions x "
            "JOIN raw_events e ON e.id = x.event_id WHERE x.proposal_status IS NULL").fetchall()
    finally:
        conn.close()
    sent = 0
    today = now.date().isoformat()
    for r in rows:
        res = json.loads(r["result_json"])
        dl, title = res.get("deadline_iso"), (res.get("task_title") or "").strip()
        if not (res.get("relevant") and dl and title and dl >= today):
            continue
        status = "duplicate" if _already_tracked(title, dl) else None
        if status is None and sent < MAX_PROPOSALS_PER_RUN:
            course = f" ({escape(res['course'])})" if res.get("course") else ""
            text = (f"📬 Нашёл срок в {'письме' if r['source'] == 'mail' else 'сообщении'} "
                    f"от {escape((r['sender'] or '—')[:50])}:\n<b>{escape(title)}</b>{course} — до "
                    f"<b>{dl[8:10]}.{dl[5:7]}</b>\n<i>{escape(res.get('summary', '')[:200])}</i>\nЗавести задачей?")
            kb = {"inline_keyboard": [[{"text": "✅ Завести", "callback_data": f"xt:{r['event_id']}:add"},
                                       {"text": "✖️ Не надо", "callback_data": f"xt:{r['event_id']}:no"}]]}
            # Резерв ДО отправки: если процесс упадёт после отправки, но до
            # записи статуса, предложение не уйдёт второй раз (ревью Codex).
            _set_proposal(r["event_id"], "sending")
            try:
                resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                     json={"chat_id": MY_TELEGRAM_ID, "text": text, "parse_mode": "HTML",
                                           "reply_markup": kb}, timeout=20)
                if resp.ok:
                    status = "proposed"
                    sent += 1
            except Exception as e:
                print(f"agent_extract: не отправил предложение: {e!r}")
            if status is None:
                _set_proposal(r["event_id"], None)
        if status:
            _set_proposal(r["event_id"], status)
    return sent


def _set_proposal(event_id: int, status):
    from agent_db import connect
    conn = connect()
    try:
        with conn:
            conn.execute("UPDATE extractions SET proposal_status=? WHERE event_id=?", (status, event_id))
    finally:
        conn.close()


def accept_proposal(event_id: int) -> dict | None:
    """Кнопка «✅ Завести» в боте — создаём задачу (source=manual)."""
    from agent_db import connect
    from storage import add_task
    # Сначала атомарно «захватываем» предложение (двойное нажатие не создаст
    # две задачи), потом создаём задачу, и только потом accepted; при
    # ошибке — откат статуса (ревью Codex: раньше accepted ставился до
    # создания задачи).
    conn = connect()
    try:
        with conn:
            cur = conn.execute("UPDATE extractions SET proposal_status='accepting' WHERE event_id=? "
                               "AND proposal_status IN ('proposed','sending')", (event_id,))
            if cur.rowcount != 1:
                return None
            row = conn.execute("SELECT result_json FROM extractions WHERE event_id=?", (event_id,)).fetchone()
    finally:
        conn.close()
    try:
        res = json.loads(row["result_json"])
        title = res["task_title"] + (f" ({res['course']})" if res.get("course") else "")
        d = datetime.date.fromisoformat(res["deadline_iso"][:10])
        deadline = datetime.datetime.combine(d, datetime.time(23, 59), tzinfo=UFA_TZ)
        task = add_task(title, deadline.isoformat(), "manual")
    except Exception:
        _set_proposal(event_id, "proposed")
        raise
    _set_proposal(event_id, "accepted")
    return task


def reject_proposal(event_id: int) -> bool:
    from agent_db import connect
    conn = connect()
    try:
        with conn:
            cur = conn.execute("UPDATE extractions SET proposal_status='rejected' "
                               "WHERE event_id=? AND proposal_status IN ('proposed','sending')", (event_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


if __name__ == "__main__":
    print(run(propose="--dry" not in sys.argv))
