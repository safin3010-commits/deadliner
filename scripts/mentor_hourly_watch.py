#!/usr/bin/env python3
"""
Почасовой сторож изменений — полностью отдельный от чек-инов процесс.
Раз в час сверяет data/*.json с прошлым снимком: новые задачи, изменённые
дедлайны, новые оценки, изменения в расписании, новые письма. Выполненные
пользователем задачи НЕ считаются изменением (не тревожим по закрытым делам).
Списки в окне на столе (пары, дедлайны, просрочки, напоминания) окно
пересобирает само при любом изменении data/*.json (scripts/mentor_dashboard.py),
поэтому здесь при изменении расписания/дедлайнов обновляются только короткие
выводы Claude к блокам. Красный баннер "Изменения: ..." убран — списки и так
всегда актуальны. В Telegram НИЧЕГО не шлём — там и так уже приходят
форварды почты/мессенджера/ВК.
"""
import datetime
import hashlib
import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
# launchd запускает нас с cwd=/ — переходим в проект, чтобы относительные
# "data/..." пути резолвились туда же, куда смотрит сам бот.
os.chdir(PROJECT_DIR)

from config import UFA_TZ
from data_lock import atomic_write_json, file_lock

DATA_DIR = os.path.join(PROJECT_DIR, "data")
SNAPSHOT_FILE = os.path.join(DATA_DIR, "mentor_watch_snapshot.json")
LOG_FILE = os.path.join(DATA_DIR, "mentor_checkin.log")


def log(msg: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{datetime.datetime.now().isoformat()}] [hourly] {msg}\n")


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None


def _schedule_content_hash(path):
    """Хэш только реального содержимого schedule_cache.json ("data"/"netology"
    по каждой неделе) — БЕЗ поля "cached_at". refresh_schedule_cache_silent в
    scheduler.py обновляет "cached_at" каждые 4ч независимо от того, изменились
    ли реально пары — простой _file_hash() ловил это как "расписание
    изменилось" почти каждый цикл и гонял дорогой перегенерацию окна на столе
    вхолостую, даже когда пары были теми же самыми."""
    data = _load_json(path, None)
    if not isinstance(data, dict):
        return None
    content = {
        week: {"data": entry.get("data"), "netology": entry.get("netology")}
        for week, entry in data.items()
        if isinstance(entry, dict)
    }
    return hashlib.md5(repr(sorted(content.items())).encode()).hexdigest()


def _dashboard_tasks_hash(tasks: list) -> str:
    """Хэш всего, что реально может отобразиться на столе (категории
    Дедлайны/Срочно/Просрочено): любая задача с дедлайном, включая
    reminder_only (они тоже попадают в "Дедлайны (3 дня)"), и включая
    done — переход в done должен убрать задачу из отображаемых списков,
    в отличие от active_tasks ниже, который намеренно её не видит (это
    верно только для баннера в Telegram, не для содержимого окна)."""
    import hashlib
    relevant = sorted(
        (str(t.get("id")), t.get("deadline"), bool(t.get("done")))
        for t in tasks
        if t.get("deadline")
    )
    return hashlib.md5(repr(relevant).encode()).hexdigest()


def build_snapshot() -> dict:
    tasks = _load_json(os.path.join(DATA_DIR, "tasks.json"), [])
    # Только активные (не выполненные, не служебные reminder_only) — id -> дедлайн.
    # Переход в done (кем бы ни был закрыт) сознательно НЕ считаем изменением
    # для БАННЕРА (не спамим "задача закрыта") — но для самого содержимого
    # окна на столе это должно быть изменением, см. _dashboard_tasks_hash.
    active_tasks = {
        str(t.get("id")): t.get("deadline")
        for t in tasks
        if not t.get("done") and t.get("source") != "reminder_only"
    }
    return {
        "active_tasks": active_tasks,
        "dashboard_tasks_hash": _dashboard_tasks_hash(tasks),
        "lms_grades_hash": _file_hash(os.path.join(DATA_DIR, "lms_grades_cache.json")),
        "modeus_grades_hash": _file_hash(os.path.join(DATA_DIR, "seen_modeus_grades.json")),
        "schedule_hash": _schedule_content_hash(os.path.join(DATA_DIR, "schedule_cache.json")),
        "knowledge_hash": _file_hash(os.path.join(DATA_DIR, "study_knowledge.json")),
        "mail_count": len(_load_json(os.path.join(DATA_DIR, "mail_recent.json"), [])),
        "vk_schedule_count": len(_load_json(os.path.join(DATA_DIR, "vk_schedule_updates.json"), [])),
    }


def diff_snapshots(old: dict, new: dict) -> list:
    changes = []
    old_tasks = old.get("active_tasks", {})
    new_tasks = new.get("active_tasks", {})

    added = set(new_tasks) - set(old_tasks)
    if added:
        word = "задача" if len(added) == 1 else "задачи" if len(added) < 5 else "задач"
        changes.append(f"новые {word} ({len(added)})")

    changed_deadline = [
        tid for tid in (set(old_tasks) & set(new_tasks))
        if old_tasks[tid] != new_tasks[tid]
    ]
    if changed_deadline:
        changes.append(f"изменился дедлайн у {len(changed_deadline)} задач(и)")

    if old.get("lms_grades_hash") != new.get("lms_grades_hash"):
        changes.append("новые оценки LMS")
    if old.get("modeus_grades_hash") != new.get("modeus_grades_hash"):
        changes.append("новые оценки Modeus")
    if old.get("schedule_hash") != new.get("schedule_hash"):
        changes.append("изменилось расписание")
    if old.get("knowledge_hash") != new.get("knowledge_hash"):
        changes.append("обновились организационные сведения из вебинаров")
    if new.get("mail_count", 0) > old.get("mail_count", 0):
        changes.append("новые письма")
    if new.get("vk_schedule_count", 0) > old.get("vk_schedule_count", 0):
        changes.append("объявление об изменении расписания в ВК")

    # Изменения, которые не видны через active_tasks (напоминания reminder_only
    # появились/пропали, или задача завершена/переоткрыта) — сама по себе
    # такая смена не спамит банером "задача закрыта", но окно на столе должно
    # перерисоваться, поэтому обязательно отмечаем как обнаруженное изменение.
    if (not added and not changed_deadline
            and old.get("dashboard_tasks_hash") != new.get("dashboard_tasks_hash")):
        changes.append("обновились напоминания/выполненные задачи")

    return changes


def _is_desktop_relevant(old: dict, new: dict) -> bool:
    """Окно на столе показывает только 4 фиксированные категории: расписание,
    дедлайны, срочно сегодня, просрочено. Оценки и почта в него не попадают —
    полная перегенерация (отдельный дорогой вызов claude -p) имеет смысл
    только для изменений, которые реально видны на экране; для остальных
    изменений хватает записи в логе — списки окно и так пересобирает само."""
    old_tasks = old.get("active_tasks", {})
    new_tasks = new.get("active_tasks", {})
    if set(new_tasks) - set(old_tasks):
        return True
    if any(old_tasks[tid] != new_tasks[tid] for tid in (set(old_tasks) & set(new_tasks))):
        return True
    if old.get("schedule_hash") != new.get("schedule_hash"):
        return True
    if old.get("knowledge_hash") != new.get("knowledge_hash"):
        return True
    if new.get("vk_schedule_count", 0) > old.get("vk_schedule_count", 0):
        return True
    if old.get("dashboard_tasks_hash") != new.get("dashboard_tasks_hash"):
        return True
    return False


YAC_SCHEDULE_FILE = os.path.join(DATA_DIR, "yac_schedule.json")


def refresh_yac_schedule():
    """Раз в проход (~2ч) — свежие ссылки на вебинары (my.mts-link.ru) и
    явный is_lxp от yetanothercalendar.ru, для перекрёстной проверки и ссылок
    в окне на столе (см. mentor_dashboard.py). Деградирует тихо,
    если сессия протухла — старый файл остаётся, никого не тревожим."""
    try:
        import asyncio
        from parsers.yetanothercalendar import fetch_week_events
        result = asyncio.run(fetch_week_events())
        if result:
            atomic_write_json(YAC_SCHEDULE_FILE, {
                "fetched_at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
                **result,
            })
            log(f"yac: обновлено ({len(result['modeus_events'])} modeus, {len(result['netology_webinars'])} netology)")
        else:
            log("yac: fetch_week_events вернул None — сессия протухла или сайт недоступен, старый файл не трогаем")
    except Exception as e:
        log(f"yac: ошибка обновления: {e!r}")


def refresh_study_analysis_daily():
    """Баллы/посещаемость Modeus раньше обновлял только чек-ин-слот "evening",
    которого нет в расписании launchd (08:30/14:00/20:33) — файл застыл на
    21.09. Теперь сторож обновляет его сам раз в ~сутки, днём."""
    path = os.path.join(DATA_DIR, "study_analysis_latest.txt")
    try:
        age_h = (datetime.datetime.now().timestamp() - os.path.getmtime(path)) / 3600
    except OSError:
        age_h = 1e9
    hour = datetime.datetime.now(tz=UFA_TZ).hour
    if age_h < 20 or not (9 <= hour < 22):
        return
    try:
        import mentor_checkin as mc
        mc.refresh_study_analysis()
        log("study_analysis обновлён (раз в сутки)")
    except Exception as e:
        log(f"study_analysis: ошибка обновления: {e!r}")


def agent_maintenance(old: dict | None, new: dict):
    """Память агента (data/agent.db): поиск по оргфактам пересобирается, когда
    изменился study_knowledge.json; триггеры «говорить/молчать» считаются в
    теневом режиме (scripts/agent_triggers.py — только журнал, без отправки)."""
    try:
        import agent_db
        if old is None or old.get("knowledge_hash") != new.get("knowledge_hash"):
            log(f"agent_db: оргфакты для поиска пересобраны ({agent_db.sync_knowledge()})")
    except Exception as e:
        log(f"agent_db: ошибка синхронизации знаний: {e!r}")
    try:
        import agent_triggers
        res = agent_triggers.run()
        logged = [r for r in res if r["logged"]]
        if logged:
            log("triggers (shadow): " + "; ".join(f"{r['decision']} {r['score']:.0f} {r['reason']}" for r in logged))
    except Exception as e:
        log(f"triggers: ошибка: {e!r}")


def main():
    refresh_yac_schedule()
    refresh_study_analysis_daily()
    old = _load_json(SNAPSHOT_FILE, None)
    new = build_snapshot()

    if old is not None:
        changes = diff_snapshots(old, new)
        if changes:
            log(f"обнаружены изменения: {changes}")

            relevant = _is_desktop_relevant(old, new)
            try:
                import mentor_checkin as mc
                if new.get("vk_schedule_count", 0) > old.get("vk_schedule_count", 0):
                    # Читаем снимок и сразу вычищаем ТОЛЬКО обработанные записи —
                    # иначе следующий чек-ин наставника обработал бы их ещё раз
                    # (дублирующиеся факты в schedule_overrides.json).
                    vk_entries = mc.load_vk_schedule_updates()
                    if vk_entries:
                        mc.extract_and_store_schedule_overrides(vk_entries)
                        mc.extract_and_store_vk_daily_digest(vk_entries)
                        mc.remove_consumed_vk_schedule_updates(vk_entries)
                        # Обновляем счётчик в снимке под то, что реально осталось
                        # в файле после очистки — иначе next-run diff сравнит
                        # новый (маленький) count с устаревшим большим и
                        # пропустит объявление, добавленное между запусками.
                        new["vk_schedule_count"] = len(mc.load_vk_schedule_updates())

                # Окно на столе пересобирается само (следит за data/*.json),
                # Claude для него больше не вызывается.
            except Exception as e:
                log(f"ошибка перегенерации виджета: {e!r}")

            if relevant:
                try:
                    import mentor_checkin as mc
                    mc.render_feed_html()
                except Exception as e:
                    log(f"ошибка перерисовки виджета: {e!r}")
        else:
            log("изменений нет")
    else:
        log("первый запуск — снимок ещё не с чем сравнивать")

    agent_maintenance(old, new)
    atomic_write_json(SNAPSHOT_FILE, new)


if __name__ == "__main__":
    main()
