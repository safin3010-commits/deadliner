"""
Долговременная память агента — data/agent.db (SQLite). Шаги 5–7 плана
design/mentor_agent_architecture.md (раздел 6, решение после ревью).

Что здесь и почему так:
  raw_events     — неизменяемый журнал входящего (почта, мессенджер, ВК,
                   уведомления Нетологии, оценки, новые задачи, сообщения
                   пользователя боту). Раньше всё это жило в *_recent.json со
                   скользящим окном 48 ч / 30 записей и терялось навсегда.
                   Четыре времени (occurred/observed/effective/ingested) —
                   чтобы снова не смешать «когда случилось» и «когда напомнить».
                   Дедупликация UNIQUE(source, kind, external_id, payload_hash):
                   изменение того же объекта — новая версия, а не дубль.
  knowledge      — оргфакты вебинаров (data/study_knowledge.json) для поиска.
  commitments    — обещания пользователя; создаются ТОЛЬКО как candidate,
                   в open переводятся кнопкой подтверждения в боте.
  dialog_messages— последние реплики разговора с ботом (вместо --resume сессии).
  notification_state / trigger_log — политика «говорить или молчать».

Это НЕ замена tasks.json/reminders.json и т.п. — состояние остаётся там
(бот, окно и наставник на нём работают); база — история и поиск.

Поиск — FTS5 (unicode61 без стемминга), русские окончания покрываем
префиксным поиском по основе слова. Эмбеддинги сознательно не используем,
пока FTS не провалится на реальных вопросах (решение ревью).

SQLite: WAL + busy_timeout — пишут несколько процессов (бот, launchd-скрипты,
окно); короткие транзакции; версия схемы в PRAGMA user_version; файл 0600 —
там личная переписка.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3

from config import UFA_TZ

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "data", "agent.db")
SCHEMA_VERSION = 2

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS raw_events (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,      -- mail|messenger|vk|netology_notif|grade|task|user|widget|system
    kind          TEXT NOT NULL,      -- message|mail|notification|grade|task_new|user_said|...
    external_id   TEXT NOT NULL,      -- id во внешней системе или стабильный ключ
    occurred_at   TEXT,               -- когда произошло (дата письма/сообщения)
    observed_at   TEXT NOT NULL,      -- когда увидел парсер
    effective_at  TEXT,               -- когда вступает в силу (дедлайн, перенос)
    ingested_at   TEXT NOT NULL,
    course        TEXT,
    sender        TEXT,
    title         TEXT,
    body          TEXT,
    url           TEXT,
    payload_hash  TEXT NOT NULL,
    meta_json     TEXT,
    UNIQUE(source, kind, external_id, payload_hash)
);
CREATE INDEX IF NOT EXISTS ix_events_time ON raw_events(observed_at);
CREATE INDEX IF NOT EXISTS ix_events_course ON raw_events(course, observed_at);

CREATE VIRTUAL TABLE IF NOT EXISTS raw_events_fts USING fts5(
    title, body, course, sender, content='raw_events', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS raw_events_ai AFTER INSERT ON raw_events BEGIN
  INSERT INTO raw_events_fts(rowid, title, body, course, sender)
  VALUES (new.id, new.title, new.body, new.course, new.sender);
END;
CREATE TRIGGER IF NOT EXISTS raw_events_ad AFTER DELETE ON raw_events BEGIN
  INSERT INTO raw_events_fts(raw_events_fts, rowid, title, body, course, sender)
  VALUES ('delete', old.id, old.title, old.body, old.course, old.sender);
END;

CREATE TABLE IF NOT EXISTS knowledge (
    id        TEXT PRIMARY KEY,
    course    TEXT,
    text      TEXT NOT NULL,
    status    TEXT,
    source    TEXT,               -- откуда (вебинар, таймкод)
    updated_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    text, course, content='knowledge', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS commitments (
    id               INTEGER PRIMARY KEY,
    action           TEXT NOT NULL,     -- что сделать (нормализовано)
    quote            TEXT NOT NULL,     -- дословно, как сказал пользователь
    due_at           TEXT,              -- к какому сроку (≠ время напоминания)
    course           TEXT,
    status           TEXT NOT NULL,     -- candidate|open|done|cancelled|rejected
    confidence       REAL,
    evidence_event_id INTEGER,
    created_at       TEXT NOT NULL,
    confirmed_at     TEXT,
    closed_at        TEXT,
    task_id          TEXT
);

CREATE TABLE IF NOT EXISTS dialog_messages (
    id    INTEGER PRIMARY KEY,
    at    TEXT NOT NULL,
    role  TEXT NOT NULL,              -- user|assistant
    text  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_state (
    fingerprint   TEXT PRIMARY KEY,
    last_sent     TEXT,
    times_sent    INTEGER DEFAULT 0,
    acknowledged  INTEGER DEFAULT 0,
    dismissed     INTEGER DEFAULT 0,
    snoozed_until TEXT
);

CREATE TABLE IF NOT EXISTS trigger_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    reason      TEXT NOT NULL,
    score       REAL NOT NULL,
    decision    TEXT NOT NULL,        -- send_now|to_brief|silent
    shadow      INTEGER NOT NULL,     -- 1 = теневой режим: только записали
    details     TEXT
);
"""


_SCHEMA_V2 = """
-- Разбор писем/сообщений дешёвой моделью: результат кэшируется по событию
-- и версии экстрактора (повторно не платим за то же письмо).
CREATE TABLE IF NOT EXISTS extractions (
    event_id    INTEGER PRIMARY KEY REFERENCES raw_events(id),
    version     TEXT NOT NULL,
    result_json TEXT NOT NULL,
    proposal_status TEXT,             -- NULL|proposed|accepted|rejected — предложение завести задачу
    created_at  TEXT NOT NULL
);
-- Дневник: сжатый итог дня (ночью, дешёвая модель) — долгая память без
-- пересылки всей истории.
CREATE TABLE IF NOT EXISTS diary (
    date       TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Что наставник «понял о тебе»: только после твоего подтверждения кнопкой.
CREATE TABLE IF NOT EXISTS profile_claims (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL,        -- candidate|confirmed|rejected
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
-- Инициативные сообщения по поводу (триггеры): учёт для лимитов.
CREATE TABLE IF NOT EXISTS initiative_sent (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    fingerprints TEXT NOT NULL,
    text        TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.datetime.now(tz=UFA_TZ).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    fresh = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    if fresh:
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        if version >= 1:
            # Бэкап перед миграцией (решение ревью).
            import shutil
            try:
                shutil.copy2(DB_PATH, f"{DB_PATH}.v{version}.bak")
            except OSError:
                pass
        with conn:
            if version < 1:
                conn.executescript(_SCHEMA_V1)
            if version < 2:
                conn.executescript(_SCHEMA_V2)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


# ─── запись ─────────────────────────────────────────────────────────

def normalize_ts(value) -> str | None:
    """Время из любого источника → ISO с поясом (письма приходят как
    «01.10.2026 18:42» — строковые сравнения по датам иначе врут)."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in (None, "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            dt = datetime.datetime.fromisoformat(value) if fmt is None else datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UFA_TZ)
        return dt.astimezone(UFA_TZ).isoformat(timespec="seconds")
    return None


def record_event(source: str, kind: str, external_id: str | None = None, *,
                 title: str = "", body: str = "", sender: str = "", course: str = "",
                 url: str = "", occurred_at: str | None = None, effective_at: str | None = None,
                 meta: dict | None = None) -> int | None:
    """Записать входящее событие. Никогда не роняет вызывающий код (парсеры
    и бот важнее журнала) — ошибка только печатается. Возвращает id или None,
    если такое событие (та же версия) уже было."""
    try:
        payload = json.dumps([title, body, sender, course, url, effective_at, meta],
                             ensure_ascii=False, sort_keys=True)
        payload_hash = hashlib.sha1(payload.encode()).hexdigest()
        occurred_at = normalize_ts(occurred_at)
        effective_at = normalize_ts(effective_at) or effective_at
        ext = external_id or payload_hash
        now = _now_iso()
        conn = connect()
        try:
            with conn:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO raw_events
                       (source, kind, external_id, occurred_at, observed_at, effective_at, ingested_at,
                        course, sender, title, body, url, payload_hash, meta_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source, kind, str(ext), occurred_at, now, effective_at, now,
                     course or None, sender or None, title or None, body or None, url or None,
                     payload_hash, json.dumps(meta, ensure_ascii=False) if meta else None),
                )
                return cur.lastrowid if cur.rowcount else None
        finally:
            conn.close()
    except Exception as e:
        print(f"agent_db.record_event error: {e!r}")
        return None


def add_dialog_message(role: str, text: str, keep: int = 400):
    try:
        conn = connect()
        try:
            with conn:
                conn.execute("INSERT INTO dialog_messages(at, role, text) VALUES (?,?,?)",
                             (_now_iso(), role, text[:4000]))
                conn.execute("DELETE FROM dialog_messages WHERE id NOT IN "
                             "(SELECT id FROM dialog_messages ORDER BY id DESC LIMIT ?)", (keep,))
        finally:
            conn.close()
    except Exception as e:
        print(f"agent_db.add_dialog_message error: {e!r}")


def recent_dialog(limit: int = 10, hours: int = 36) -> list[dict]:
    since = (datetime.datetime.now(tz=UFA_TZ) - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = connect()
    try:
        rows = conn.execute("SELECT at, role, text FROM dialog_messages WHERE at >= ? "
                            "ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


# ─── знания ─────────────────────────────────────────────────────────

def sync_knowledge(path: str | None = None) -> int:
    """Перелить активные оргфакты из data/study_knowledge.json в таблицу с
    поиском. Дёшево и идемпотентно — полная пересборка (их ~2 тыс.)."""
    path = path or os.path.join(PROJECT_DIR, "data", "study_knowledge.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = []
    for fact in data.get("facts", []):
        # Только подтверждённые (active): needs_review не выдаём за факт,
        # not_organization — не оргинформация (решение ревью: кандидаты ≠ факты).
        if fact.get("status") != "active":
            continue
        text = fact.get("text") or ""
        if not text:
            continue
        src = " ".join(str(fact.get(k)) for k in ("webinar", "source_timestamp") if fact.get(k))
        rows.append((str(fact.get("id") or hashlib.sha1(text.encode()).hexdigest()[:16]),
                     fact.get("course") or fact.get("course_name") or "", text, fact.get("status") or "active",
                     src, data.get("updated_at")))
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM knowledge")
            conn.executemany("INSERT OR REPLACE INTO knowledge(id, course, text, status, source, updated_at) "
                             "VALUES (?,?,?,?,?,?)", rows)
            conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')")
    finally:
        conn.close()
    return len(rows)


# ─── поиск ──────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[\wё]+", re.IGNORECASE)
_STOP = {"и", "в", "во", "на", "по", "к", "ко", "о", "об", "что", "как", "это", "у", "с", "со",
         "за", "из", "для", "не", "ли", "а", "но", "мне", "меня", "мой", "моя", "мои", "есть",
         "был", "была", "было", "бы", "же", "то", "там", "тут", "про", "когда", "где", "какой", "какие"}


def fts_query(text: str) -> str:
    """Запрос пользователя → FTS5 MATCH. Русская морфология без стеммера:
    берём основу слова (обрезаем окончание) и ищем по префиксу —
    «лабораторную» найдёт «лабораторная», «лабораторной»."""
    terms = []
    for w in _WORD_RE.findall(text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        stem = w[: max(3, len(w) - 2)] if len(w) > 4 else w
        terms.append(f'"{stem}"*')
    return " OR ".join(dict.fromkeys(terms))


def search_events(query: str, *, course: str | None = None, since: str | None = None,
                  limit: int = 8) -> list[dict]:
    q = fts_query(query)
    if not q:
        return []
    sql = ("SELECT e.id, e.source, e.kind, e.occurred_at, e.observed_at, e.course, e.sender, e.title, "
           "substr(e.body, 1, 600) AS body, e.url FROM raw_events_fts f JOIN raw_events e ON e.id = f.rowid "
           "WHERE raw_events_fts MATCH ?")
    args: list = [q]
    if course:
        sql += " AND e.course LIKE ?"
        args.append(f"%{course}%")
    if since:
        sql += " AND e.observed_at >= ?"
        args.append(since)
    sql += " ORDER BY bm25(raw_events_fts) LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def search_knowledge(query: str, *, course: str | None = None, limit: int = 8) -> list[dict]:
    q = fts_query(query)
    if not q:
        return []
    sql = ("SELECT k.id, k.course, k.text, k.source FROM knowledge_fts f "
           "JOIN knowledge k ON k.rowid = f.rowid WHERE knowledge_fts MATCH ?")
    args: list = [q]
    if course:
        sql += " AND k.course LIKE ?"
        args.append(f"%{course}%")
    sql += " ORDER BY bm25(knowledge_fts) LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ─── обещания ───────────────────────────────────────────────────────

def add_commitment_candidate(action: str, quote: str, due_at: str | None, course: str | None,
                             confidence: float, evidence_event_id: int | None = None) -> int:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO commitments(action, quote, due_at, course, status, confidence, "
                "evidence_event_id, created_at) VALUES (?,?,?,?, 'candidate', ?,?,?)",
                (action, quote, due_at, course, confidence, evidence_event_id, _now_iso()))
            return cur.lastrowid
    finally:
        conn.close()


def set_commitment_status(cid: int, status: str) -> dict | None:
    """candidate→open (подтвердил), candidate→rejected, open→done/cancelled."""
    allowed = {"candidate": {"open", "rejected"}, "open": {"done", "cancelled"}}
    conn = connect()
    try:
        with conn:
            row = conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
            if not row or status not in allowed.get(row["status"], set()):
                return None
            field = "confirmed_at" if status == "open" else "closed_at"
            conn.execute(f"UPDATE commitments SET status=?, {field}=? WHERE id=?", (status, _now_iso(), cid))
            return dict(row) | {"status": status}
    finally:
        conn.close()


def list_commitments(status: str = "open", limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM commitments WHERE status=? ORDER BY COALESCE(due_at, '9999') LIMIT ?",
            (status, limit)).fetchall()]
    finally:
        conn.close()


# ─── дневник и профиль ──────────────────────────────────────────────

def save_diary(date: str, text: str):
    conn = connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO diary(date, text, created_at) VALUES (?,?,?)",
                         (date, text, _now_iso()))
    finally:
        conn.close()


def get_diary(days: int = 3) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT date, text FROM diary ORDER BY date DESC LIMIT ?", (days,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


def has_diary(date: str) -> bool:
    conn = connect()
    try:
        return conn.execute("SELECT 1 FROM diary WHERE date=?", (date,)).fetchone() is not None
    finally:
        conn.close()


def add_profile_claim(text: str) -> int:
    conn = connect()
    try:
        with conn:
            return conn.execute("INSERT INTO profile_claims(text, status, created_at) VALUES (?, 'candidate', ?)",
                                (text, _now_iso())).lastrowid
    finally:
        conn.close()


def decide_profile_claim(cid: int, status: str) -> dict | None:
    if status not in ("confirmed", "rejected"):
        return None
    conn = connect()
    try:
        with conn:
            row = conn.execute("SELECT * FROM profile_claims WHERE id=? AND status='candidate'", (cid,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE profile_claims SET status=?, decided_at=? WHERE id=?", (status, _now_iso(), cid))
            return dict(row) | {"status": status}
    finally:
        conn.close()


def confirmed_profile() -> list[str]:
    conn = connect()
    try:
        return [r["text"] for r in conn.execute(
            "SELECT text FROM profile_claims WHERE status='confirmed' ORDER BY id").fetchall()]
    finally:
        conn.close()
