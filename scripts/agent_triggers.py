#!/usr/bin/env python3
"""
Триггеры «говорить или молчать» — шаг 8 плана (design/mentor_agent_architecture.md,
раздел 6). Считает поводы и решение, пишет их в data/agent.db → trigger_log.
Отправка включена 2026-10-02 по решению пользователя, с жёсткими рамками:
только «send_now», не больше MAX_PER_DAY инициативных сообщений в сутки, не
чаще раза в MIN_GAP_H часов, все поводы склеиваются в одно сообщение; каждый
👎 под таким сообщением за 7 дней поднимает порог на DISLIKE_PENALTY
(самокалибровка). SHADOW = True — снова только журнал.

Скоринг (стартовые веса из ревью, калибровать по журналу):
  score = base + urgency(0..25) + importance(0..15) + actionability(0..10)
          + novelty(0..10) − repetition(0..50) − quiet_hours(0|100)
  ≥ 75 → сразу · 45–74 → в ближайший бриф · < 45 → молчать
Шаблонные уведомления (скоро пара, напоминания) сюда не входят — их шлёт
бот кодом, без LLM.
"""
import datetime
import hashlib
import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
os.chdir(PROJECT_DIR)

from config import UFA_TZ

SHADOW = False
MAX_PER_DAY = 2
MIN_GAP_H = 3
SEND_THRESHOLD = 75
DISLIKE_PENALTY = 10
QUIET_FROM, QUIET_TO = 23, 8
REPEAT_WINDOW_H = 24

BASE = {
    "grade_low": 50,          # новая низкая оценка
    "grade_new": 30,          # любая новая оценка
    "deadline_risk": 40,      # оцениваемое задание ≤ 48 ч
    "commitment_overdue": 45, # его подтверждённое обещание просрочено
    "schedule_change": 40,    # перенос/отмена пары
}


def _now():
    return datetime.datetime.now(tz=UFA_TZ)


def _fp(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def collect(now) -> list[dict]:
    """Кандидаты-поводы. Всё — детерминированно из данных, без LLM."""
    from agent_db import connect, list_commitments
    from mentor_dashboard import build_model
    out = []
    since = (now - datetime.timedelta(hours=3)).isoformat(timespec="seconds")
    # Оценки ищем за 12 ч: двойка, пришедшая ночью, дождётся утра (тихие
    # часы), а не выпадет из 3-часового окна. Повтор гасит notification_state.
    grades_since = (now - datetime.timedelta(hours=12)).isoformat(timespec="seconds")

    conn = connect()
    try:
        # COALESCE: событие, залитое задним числом, не должно выглядеть свежим.
        for e in conn.execute("SELECT * FROM raw_events WHERE kind='grade' "
                              "AND COALESCE(occurred_at, observed_at) >= ?", (grades_since,)):
            meta = json.loads(e["meta_json"] or "{}")
            value = str(meta.get("value", "")).strip()
            try:
                num = float(value.replace(",", "."))
                # 0 в Modeus — реальный ноль (подтвердил пользователь 2026-10-02).
                # Низкая — 0..3 по пятибалльной.
                low = 0 <= num <= 3
            except ValueError:
                low = False
            what = e["title"] or e["course"] or "работа"
            out.append({"reason": "grade_low" if low else "grade_new", "fp": _fp("grade", e["id"]),
                        "urgency": 10, "importance": 15 if low else 5, "actionability": 5,
                        "text": f"Оценка {value} — {what}" + (f" ({e['course']})" if e["title"] and e["course"] else "")})

    finally:
        conn.close()

    # Переносы/отмены — только уже извлечённые факты (mentor_checkin пишет их
    # в schedule_overrides.json из беседы ВК) и только на сегодня-завтра:
    # просто слово «перенос» в общем чате — не повод писать.
    try:
        with open(os.path.join(PROJECT_DIR, "data", "schedule_overrides.json"), encoding="utf-8") as f:
            overrides = json.load(f)
    except Exception:
        overrides = []
    days = {now.strftime("%d.%m"), (now + datetime.timedelta(days=1)).strftime("%d.%m")}
    for o in overrides:
        added = o.get("added_at") or ""
        if added >= since and any(d in o.get("note", "") for d in days):
            out.append({"reason": "schedule_change", "fp": _fp("override", o.get("note")),
                        "urgency": 20, "importance": 10, "actionability": 5,
                        "text": f"Изменение расписания: {o['note']}"})

    model = build_model(now)
    for i in model["deadlines"]:
        if i["kind"] != "task" or i["soft"]:
            continue
        hours = (i["date"] - now).total_seconds() / 3600
        if 0 < hours <= 48:
            out.append({"reason": "deadline_risk", "fp": _fp("deadline", i["id"], i["date"].date()),
                        "urgency": 25 if hours <= 24 else 15, "importance": 10, "actionability": 10,
                        "text": f"Дедлайн {i['date'].strftime('%d.%m %H:%M')}: {i['title']} ({i['course']})"})

    today = now.date().isoformat()
    for c in list_commitments("open", 50):
        if c.get("due_at") and c["due_at"][:10] < today:
            # Отпечаток по id обещания, без даты: иначе одно и то же
            # просроченное обещание считалось новым поводом каждый день.
            out.append({"reason": "commitment_overdue", "fp": _fp("commit", c["id"]),
                        "urgency": 15, "importance": 10, "actionability": 10,
                        "text": f"Обещание «{c['action']}» — срок {c['due_at'][:10]} прошёл"})
    return out


def score(c: dict, state: dict | None, now) -> float:
    s = BASE[c["reason"]] + c["urgency"] + c["importance"] + c["actionability"]
    novelty = 10
    repetition = 0
    if state and state["last_sent"]:
        hours = (now - datetime.datetime.fromisoformat(state["last_sent"])).total_seconds() / 3600
        if hours < REPEAT_WINDOW_H:
            repetition = 50
        novelty = 0
        # После игнора (не подтвердил/не отреагировал) интервал растёт.
        repetition += min(30, 10 * (state["times_sent"] or 0))
    if state and state["dismissed"]:
        repetition = 100
    # Тихие часы — для всех поводов, включая двойку: она дождётся утра.
    quiet = 100 if (now.hour >= QUIET_FROM or now.hour < QUIET_TO) else 0
    return s + novelty - repetition - quiet


def _threshold() -> float:
    """Порог растёт на DISLIKE_PENALTY за каждый 👎 под инициативным
    сообщением за 7 дней (feedback_id из initiative_sent)."""
    from agent_db import connect
    since = (_now() - datetime.timedelta(days=7)).isoformat()
    conn = connect()
    try:
        ids = set()
        for r in conn.execute("SELECT fingerprints FROM initiative_sent WHERE at >= ?", (since,)):
            try:
                ids.add(json.loads(r["fingerprints"]).get("feedback_id"))
            except Exception:
                pass
    finally:
        conn.close()
    dislikes = 0
    try:
        with open(os.path.join(PROJECT_DIR, "data", "mentor_feedback.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue   # одна битая строка не должна обнулять учёт 👎
                dislikes += r.get("feedback_id") in ids and r.get("rating") == "down"
    except FileNotFoundError:
        pass
    return min(SEND_THRESHOLD + DISLIKE_PENALTY * dislikes, 150)


def decide(s: float, threshold: float = SEND_THRESHOLD) -> str:
    return "send_now" if s >= threshold else "to_brief" if s >= 45 else "silent"


def run() -> list[dict]:
    from agent_db import connect
    now = _now()
    cands = collect(now)
    threshold = _threshold()
    results = []
    conn = connect()
    try:
        with conn:
            for c in cands:
                state = conn.execute("SELECT * FROM notification_state WHERE fingerprint=?", (c["fp"],)).fetchone()
                sc = score(c, state, now)
                d = decide(sc, threshold)
                # Один и тот же повод логируем раз в сутки, а не на каждом прогоне.
                seen = conn.execute("SELECT 1 FROM trigger_log WHERE fingerprint=? AND at >= ?",
                                    (c["fp"], (now - datetime.timedelta(hours=REPEAT_WINDOW_H)).isoformat())).fetchone()
                if not seen:
                    conn.execute("INSERT INTO trigger_log(at, fingerprint, reason, score, decision, shadow, details) "
                                 "VALUES (?,?,?,?,?,?,?)",
                                 (now.isoformat(timespec="seconds"), c["fp"], c["reason"], sc, d, int(SHADOW), c["text"]))
                results.append({**c, "score": sc, "decision": d, "logged": not seen})
    finally:
        conn.close()
    if not SHADOW:
        send_initiative([r for r in results if r["decision"] == "send_now"], now)
    return results


def _can_send(now) -> bool:
    from agent_db import connect
    conn = connect()
    try:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        today = conn.execute("SELECT COUNT(*) FROM initiative_sent WHERE at >= ?", (day_start,)).fetchone()[0]
        last = conn.execute("SELECT MAX(at) FROM initiative_sent").fetchone()[0]
    finally:
        conn.close()
    if today >= MAX_PER_DAY:
        return False
    if last and (now - datetime.datetime.fromisoformat(last)).total_seconds() < MIN_GAP_H * 3600:
        return False
    return True


INITIATIVE_SYSTEM = (
    "Ты — личный наставник студента: пишешь в Telegram по-русски, на «ты», живо и коротко "
    "(2–4 предложения), Telegram HTML (только <b>/<i>). Пишешь не по расписанию, а потому что "
    "появился повод — сразу к делу: что случилось и что конкретно сделать. Без паники, без "
    "морали, факты бери только из сообщения, даты не меняй."
)


def send_initiative(items: list[dict], now) -> bool:
    """Все поводы — одним сообщением, в рамках лимитов."""
    if not items or not _can_send(now):
        return False
    import uuid
    from agent_db import connect
    from claude_session import run_claude_oneshot
    from mentor_checkin import send_telegram
    from mentor_context import build_checkin_pack, record_sent
    reasons = "\n".join(f"- {i['text']}" for i in items)
    pack = build_checkin_pack("initiative")
    text, err = run_claude_oneshot(
        f"ПОВОДЫ НАПИСАТЬ СЕЙЧАС:\n{reasons}\n\nКонтекст (код, точное):\n{pack}",
        INITIATIVE_SYSTEM, 180, "claude-sonnet-5", "initiative")
    if err or not text:
        print(f"agent_triggers: не сформулировал ({err})")
        return False
    fid = uuid.uuid4().hex[:10]
    # Резервируем ДО отправки (лимиты и повторы считаются по этим записям):
    # падение после отправки не приведёт к дублю. Если отправка не удалась —
    # резерв снимаем (ревью Codex).
    conn = connect()
    try:
        with conn:
            row_id = conn.execute("INSERT INTO initiative_sent(at, fingerprints, text) VALUES (?,?,?)",
                                  (now.isoformat(timespec="seconds"),
                                   json.dumps({"fps": [i["fp"] for i in items], "feedback_id": fid}), text)).lastrowid
            for i in items:
                conn.execute("INSERT INTO notification_state(fingerprint, last_sent, times_sent) VALUES (?,?,1) "
                             "ON CONFLICT(fingerprint) DO UPDATE SET last_sent=excluded.last_sent, "
                             "times_sent=times_sent+1", (i["fp"], now.isoformat()))
    finally:
        conn.close()
    try:
        send_telegram(text, feedback_id=fid)
    except Exception as e:
        print(f"agent_triggers: не отправил: {e!r}")
        conn = connect()
        try:
            with conn:
                conn.execute("DELETE FROM initiative_sent WHERE id=?", (row_id,))
                for i in items:
                    conn.execute("UPDATE notification_state SET last_sent=NULL, times_sent=MAX(times_sent-1, 0) "
                                 "WHERE fingerprint=?", (i["fp"],))
        finally:
            conn.close()
        return False
    record_sent("initiative", text, fid)
    return True


def report(days: int = 7) -> str:
    from agent_db import connect
    since = (_now() - datetime.timedelta(days=days)).isoformat()
    conn = connect()
    try:
        rows = conn.execute("SELECT at, reason, score, decision, details FROM trigger_log WHERE at >= ? "
                            "ORDER BY at DESC LIMIT 60", (since,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return "Теневых срабатываний пока нет."
    lines = [f"{r['at'][5:16]} {r['decision']:8} {r['score']:5.0f} {r['reason']}: {r['details'][:90]}" for r in rows]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        print(report())
    else:
        for r in run():
            print(f"{r['decision']:8} {r['score']:5.0f} {r['reason']}: {r['text']}")
