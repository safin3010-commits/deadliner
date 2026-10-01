#!/usr/bin/env python3
"""
Триггеры «говорить или молчать» — шаг 8 плана (design/mentor_agent_architecture.md,
раздел 6). Сейчас работает в ТЕНЕВОМ режиме: считает поводы и решение, пишет
их в data/agent.db → trigger_log, но ничего не отправляет. После недели
наблюдения по журналу (`agent_triggers.py report`) калибруем веса и только
потом включаем отправку (SHADOW = False).

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

SHADOW = True
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

    conn = connect()
    try:
        # COALESCE: событие, залитое задним числом, не должно выглядеть свежим.
        for e in conn.execute("SELECT * FROM raw_events WHERE kind='grade' "
                              "AND COALESCE(occurred_at, observed_at) >= ?", (since,)):
            meta = json.loads(e["meta_json"] or "{}")
            value = str(meta.get("value", "")).strip()
            try:
                num = float(value.replace(",", "."))
                # 0 в Modeus — обычно «не выставлено», не двойка. Низкая — 1..3 по пятибалльной.
                low = 1 <= num <= 3
            except ValueError:
                low = False
            what = e["title"] or e["course"] or "работа"
            out.append({"reason": "grade_low" if low else "grade_new", "fp": _fp("grade", e["id"]),
                        "urgency": 10, "importance": 15 if low else 5, "actionability": 5,
                        "text": f"Оценка {value} — {what}" + (f" ({e['course']})" if e["title"] and e["course"] else "")})
        for e in conn.execute("SELECT * FROM raw_events WHERE source='vk' AND COALESCE(occurred_at, observed_at) >= ? "
                              "AND (body LIKE '%перенос%' OR body LIKE '%отмен%')", (since,)):
            out.append({"reason": "schedule_change", "fp": _fp("vk", e["id"]),
                        "urgency": 20, "importance": 10, "actionability": 5,
                        "text": f"ВК: {(e['body'] or '')[:120]}"})
    finally:
        conn.close()

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
            out.append({"reason": "commitment_overdue", "fp": _fp("commit", c["id"], today),
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
    quiet = 100 if (now.hour >= QUIET_FROM or now.hour < QUIET_TO) and c["reason"] != "grade_low" else 0
    return s + novelty - repetition - quiet


def decide(s: float) -> str:
    return "send_now" if s >= 75 else "to_brief" if s >= 45 else "silent"


def run() -> list[dict]:
    from agent_db import connect
    now = _now()
    cands = collect(now)
    results = []
    conn = connect()
    try:
        with conn:
            for c in cands:
                state = conn.execute("SELECT * FROM notification_state WHERE fingerprint=?", (c["fp"],)).fetchone()
                sc = score(c, state, now)
                d = decide(sc)
                # Один и тот же повод логируем раз в сутки, а не на каждом прогоне.
                seen = conn.execute("SELECT 1 FROM trigger_log WHERE fingerprint=? AND at >= ?",
                                    (c["fp"], (now - datetime.timedelta(hours=REPEAT_WINDOW_H)).isoformat())).fetchone()
                if not seen:
                    conn.execute("INSERT INTO trigger_log(at, fingerprint, reason, score, decision, shadow, details) "
                                 "VALUES (?,?,?,?,?,?,?)",
                                 (now.isoformat(timespec="seconds"), c["fp"], c["reason"], sc, d, int(SHADOW), c["text"]))
                if not SHADOW and d == "send_now":
                    conn.execute("INSERT INTO notification_state(fingerprint, last_sent, times_sent) VALUES (?,?,1) "
                                 "ON CONFLICT(fingerprint) DO UPDATE SET last_sent=excluded.last_sent, "
                                 "times_sent=times_sent+1", (c["fp"], now.isoformat()))
                results.append({**c, "score": sc, "decision": d, "logged": not seen})
    finally:
        conn.close()
    return results


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
