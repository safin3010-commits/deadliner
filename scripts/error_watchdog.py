"""
Сторож ошибок бота: раз в N минут (launchd) смотрит НОВЫЙ кусок bot.log
(с прошлой прочитанной позиции — файл огромный, целиком не перечитываем)
и делает одно из двух:

1. АВТО-ФИКС (перезапуск через stop.sh/start.sh) — только для узкого
   списка сигнатур, которые заведомо лечатся перезапуском процесса:
   - бот вообще не жив (процесс из /tmp/deadliner.pid не найден)
   - "playwright/driver/node ... No such file" — протухший путь к venv
     (баг, который уже был при переносе папки проекта)
   Не перезапускаем чаще чем раз в RESTART_COOLDOWN_MINUTES, чтобы не
   зациклиться, если причина не в протухшем пути, а в чём-то другом.

2. УВЕДОМЛЕНИЕ в Telegram — для внешних сбоев, которые кодом не лечатся
   (LMS 403, Modeus SSO не отвечает и т.п.): если ошибки идут, а успешных
   попыток нет дольше UNHEALTHY_MINUTES — шлём ОДНО сообщение и не повторяем
   его чаще чем раз в ALERT_COOLDOWN_MINUTES, пока проблема не решится сама
   (тогда success-сигнал сбросит таймер) или не пройдёт кулдаун.

Запуск: venv/bin/python3 scripts/error_watchdog.py
"""
import datetime
import json
import os
import re
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

import requests

from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID, UFA_TZ
from data_lock import atomic_write_json, file_lock

BOT_LOG = os.path.join(PROJECT_DIR, "bot.log")
STATE_FILE = os.path.join(PROJECT_DIR, "data", "error_watchdog_state.json")
PID_FILE = "/tmp/deadliner.pid"
LOG_FILE = os.path.join(PROJECT_DIR, "data", "error_watchdog.log")

RESTART_COOLDOWN_MINUTES = 15
UNHEALTHY_MINUTES = 90          # сколько без единого успеха, чтобы забить тревогу
ALERT_COOLDOWN_MINUTES = 180    # не чаще, чем раз в 3 часа — launchd-починки (см. check_launchd_jobs)
# Отдельный, более редкий кулдаун для категорий внешних сервисов (LMS/Modeus/почта
# и т.п.) — обнаружено 2026-09-21: блокировка LMS по IP (см. WATCH_CATEGORIES)
# может держаться много часов, и повтор раз в 3 часа за это время успевает
# надоесть, хотя сама причина (внешняя блокировка) за это время не меняется.
SERVICE_ALERT_COOLDOWN_MINUTES = 480  # 8 часов

# Другие launchd-задачи этого проекта — сверяем, что они реально
# зарегистрированы в launchd (обнаружено 2026-09-20: com.ilnursafin.mentorcheckin
# пропала из `launchctl list` минимум на 3 дня без единого сигнала — launchd
# не подхватывал её на положенные 08:30/14:00/20:33, и никто это не заметил,
# пока не спросили "почему стол не обновился"). Раз в прогон (каждые 15 мин)
# проверяем список и тихо перезагружаем любую пропавшую, один раз уведомляя
# в Telegram (не каждый прогон — сама починка тихая и мгновенная).
LAUNCHD_JOBS = {
    "com.ilnursafin.mentorcheckin": "~/Library/LaunchAgents/com.ilnursafin.mentorcheckin.plist",
    "com.ilnursafin.mentorhourlywatch": "~/Library/LaunchAgents/com.ilnursafin.mentorhourlywatch.plist",
    "com.ilnursafin.yaclinks": "~/Library/LaunchAgents/com.ilnursafin.yaclinks.plist",
    "com.ilnursafin.mentorwidget": "~/Library/LaunchAgents/com.ilnursafin.mentorwidget.plist",
}

# mentorcheckin/mentorhourlywatch/yaclinks запускаются по расписанию и выходят —
# для них "-" (нет PID) между прогонами нормально, LAUNCHD_JOBS выше только
# проверяет, что задача вообще зарегистрирована. mentorwidget — другое дело:
# RunAtLoad + KeepAlive=false, должен работать постоянно как окно на столе;
# если он упал (а не просто "ещё не запускался"), launchd НЕ перезапустит
# его сам (KeepAlive=false) — простого `launchctl load` тоже недостаточно
# (он уже "загружен", просто без живого процесса), нужен unload+load.
LAUNCHD_ALWAYS_RUNNING = {"com.ilnursafin.mentorwidget"}

# Опрос Telegram (getUpdates) — особый случай: при тихой смерти внутреннего
# цикла polling'а (обнаружено 2026-09-20 — процесс жив, apscheduler-джобы
# продолжают работать, а getUpdates просто перестаёт появляться в логе, без
# единой строки ошибки) WATCH_CATEGORIES не сработает вообще: там нужен
# fail_pattern, а тут сбой — это ОТСУТСТВИЕ строк, а не их появление. Кнопки
# и сообщения в боте при этом перестают работать полностью, а не просто
# "нет новых данных" как при сбое одного из внешних сервисов — поэтому чиним
# перезапуском (как is_bot_alive), а не просто уведомлением.
TELEGRAM_POLL_PATTERN = re.compile(r"api\.telegram\.org/bot[^/]+/getUpdates")
TELEGRAM_SILENT_MINUTES = 20

# ─── Категории для мониторинга внешних (неавтофиксимых) сбоев ─────────
# success_pattern сбрасывает таймер "нездоровья"; fail_pattern увеличивает
# счётчик ошибок в этом прогоне.
WATCH_CATEGORIES = {
    "lms": {
        "label": "LMS (lms.utmn.ru)",
        "fail_pattern": re.compile(r"LMS grades check error:|LMS fetch failed:|LMS: логин не удался"),
        "success_pattern": re.compile(r"LMS: авторизация успешна|LMS: сессия из кэша"),
    },
    "modeus": {
        "label": "Modeus (оценки/расписание)",
        "fail_pattern": re.compile(r"Modeus: не удалось авторизоваться"),
        "success_pattern": re.compile(r"Modeus: авторизация успешна|Modeus: используем кэшированный токен"),
    },
    "mail": {
        "label": "Почта (Яндекс)",
        # реальная строка лога — "Mail (Яндекс): найдено..." / "Mail (Яндекс): новых..."
        # (см. parsers/mail.py: label="Яндекс" передаётся в _fetch_new_emails_sync).
        # 2026-09-17: старый паттерн без метки вообще не матчил ни одной строки —
        # success никогда не засчитывался, что давало ложные тревоги при первом же сбое.
        "fail_pattern": re.compile(r"Mail \(Яндекс\) IMAP error:|Mail fetch failed:"),
        "success_pattern": re.compile(r"Mail \(Яндекс\): (найдено|новых)"),
    },
    "gmail": {
        "label": "Gmail",
        "fail_pattern": re.compile(r"Mail \(Gmail\) IMAP error:|Gmail fetch failed:"),
        "success_pattern": re.compile(r"Mail \(Gmail\): (найдено|новых)"),
    },
    "netology": {
        "label": "Netology (дедлайны/уведомления)",
        # "неверный логин или пароль" / "cookie не найден" — конкретные тексты
        # NetologyAuthError (parsers/netology.py) — не совпадали ни с одним
        # паттерном раньше, из-за чего сорвавшийся пароль не засчитывался
        # этой категорией вообще никогда.
        "fail_pattern": re.compile(
            r"Netology auth failed:|Netology GET .* failed:|Netology: логин не удался"
            r"|Netology: неверный логин или пароль|Netology: cookie не найден"
        ),
        "success_pattern": re.compile(r"Netology: авторизация успешна"),
    },
    "vk": {
        "label": "ВКонтакте",
        "fail_pattern": re.compile(r"VK browser error:"),
        "success_pattern": re.compile(r"VK: открываем беседу"),
    },
    "messenger": {
        "label": "Яндекс Мессенджер",
        "fail_pattern": re.compile(r"Messenger fetch failed:|Scheduler messenger check error:"),
        "success_pattern": re.compile(r"Messenger: итого новых"),
    },
}

# ─── Сигнатуры, которые лечатся перезапуском ───────────────────────────
RESTART_SIGNATURES = [
    re.compile(r"playwright/driver/node"),
]


def log(msg: str):
    line = f"[{datetime.datetime.now(tz=UFA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with file_lock(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _save_state(state: dict):
    with file_lock(STATE_FILE):
        atomic_write_json(STATE_FILE, state)


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not MY_TELEGRAM_ID:
        log("нет TELEGRAM_TOKEN/MY_TELEGRAM_ID — не могу отправить уведомление")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": MY_TELEGRAM_ID, "text": text}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log(f"не удалось отправить в Telegram: {e!r}")


def is_bot_alive() -> bool:
    if not os.path.exists(PID_FILE):
        return False
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # не убивает, только проверяет существование процесса
        return True
    except (ProcessLookupError, ValueError, FileNotFoundError):
        return False
    except PermissionError:
        return True  # процесс есть, просто принадлежит не нам (не наш случай)


def restart_bot(reason: str):
    log(f"ПЕРЕЗАПУСК бота. Причина: {reason}")
    try:
        # DEVNULL, не capture_output=True: start.sh фонит caffeinate без
        # своего редиректа, и он наследует stdout/stderr родителя — если
        # это pipe (capture_output), subprocess.run зависает до тех пор,
        # пока caffeinate не завершится (а он живёт пока жив бот).
        #
        # start_new_session=True — КРИТИЧНО. error_watchdog.py сам launchd-
        # задача с StartInterval: запускается, отрабатывает main() и полностью
        # завершается до следующего срабатывания. Без start_new_session
        # start.sh (и его фоновые "&"-дети main.py/caffeinate) наследуют ТУ ЖЕ
        # группу процессов, что и сам error_watchdog.py — а когда его
        # собственный процесс завершается (сразу после restart_bot()),
        # launchd подчищает всю группу процессов этой задачи, включая только
        # что запущенного бота. Итог (обнаружено 2026-09-20): бот стартовал,
        # доживал до первого getUpdates/пары запросов (~5-10с) и тут же тихо
        # получал полное завершение — раз в 15 минут, бесконечно, с постоянным
        # "⚙️ Бот сам перезапустился" в Telegram. При ручном запуске из
        # интерактивного шелла (не launchd-задачи) это не воспроизводилось —
        # только через error_watchdog. start_new_session отвязывает
        # start.sh и всё, что он порождает, в отдельную сессию, которая
        # переживает завершение самого error_watchdog.py.
        subprocess.run(["./stop.sh"], cwd=PROJECT_DIR, timeout=30,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)
        subprocess.run(["./start.sh"], cwd=PROJECT_DIR, timeout=30,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)
        send_telegram(f"⚙️ Бот сам перезапустился (причина: {reason}). Если что-то ведёт себя странно — напиши.")
    except Exception as e:
        log(f"перезапуск не удался: {e!r}")


def _alert_launchd_fix(state: dict, now: datetime.datetime, key: str, text: str):
    jobs_state = state.setdefault("launchd_jobs", {}).setdefault(key, {})
    last_alert = jobs_state.get("last_alert")
    alert_cooldown_ok = True
    if last_alert:
        mins = (now - datetime.datetime.fromisoformat(last_alert)).total_seconds() / 60
        alert_cooldown_ok = mins > ALERT_COOLDOWN_MINUTES
    if alert_cooldown_ok:
        send_telegram(text)
        jobs_state["last_alert"] = now.isoformat()


def check_launchd_jobs(state: dict, now: datetime.datetime):
    """Сверяет LAUNCHD_JOBS с реальным `launchctl list` и тихо перезагружает
    любую пропавшую задачу — с одним уведомлением в Telegram (не на каждый
    прогон, сама починка почти мгновенная и должна остаться незаметной).
    Для LAUNCHD_ALWAYS_RUNNING (виджет) дополнительно проверяет PID — "загружена"
    и "реально работает" для KeepAlive=false задачи не одно и то же."""
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        loaded_labels = set()
        pid_by_label = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            pid, _status, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
            loaded_labels.add(label)
            pid_by_label[label] = pid
    except Exception as e:
        log(f"launchd: не удалось получить список задач: {e!r}")
        return

    for label, plist_rel in LAUNCHD_JOBS.items():
        plist_path = os.path.expanduser(plist_rel)

        if label not in loaded_labels:
            if not os.path.exists(plist_path):
                log(f"launchd: {label} не загружена, И plist не найден ({plist_path}) — не могу починить")
                continue
            log(f"launchd: {label} не зарегистрирована в системе — перезагружаю")
            try:
                subprocess.run(["launchctl", "load", plist_path], timeout=15,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"launchd: не удалось перезагрузить {label}: {e!r}")
                continue
            _alert_launchd_fix(
                state, now, label,
                f"⚙️ Задача {label} пропала из launchd (не должна была) — перезагрузил её обратно. "
                f"Если это повторяется часто — стоит разобраться, почему она слетает."
            )
            continue

        if label in LAUNCHD_ALWAYS_RUNNING and pid_by_label.get(label, "-") == "-":
            # Загружена, но процесса нет — KeepAlive=false, launchd сам не
            # перезапустит, а простой load на уже загруженный label ничего
            # не делает — нужен unload+load, чтобы сработал RunAtLoad заново.
            log(f"launchd: {label} загружена, но не работает (PID отсутствует) — перезапускаю")
            try:
                subprocess.run(["launchctl", "unload", plist_path], timeout=15,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["launchctl", "load", plist_path], timeout=15,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"launchd: не удалось перезапустить {label}: {e!r}")
                continue
            _alert_launchd_fix(
                state, now, label,
                f"⚙️ {label} была загружена, но не работала (упала) — перезапустил. "
                f"Если это повторяется часто — стоит разобраться, почему падает."
            )


def read_new_lines(state: dict) -> tuple[list[str], int]:
    """Читает только то, что добавилось в bot.log с прошлой проверки."""
    try:
        size = os.path.getsize(BOT_LOG)
    except FileNotFoundError:
        return [], 0

    if "log_offset" not in state:
        # первый запуск — не разбираем всю историю лога, начинаем с
        # текущего конца файла (иначе поймаем сигнатуры/сбои за недели)
        return [], size

    offset = state.get("log_offset", 0)
    if offset > size:
        # лог обрезали/ротировали — начинаем сначала (но не читаем весь
        # многогигабайтный файл, максимум последние 5 МБ)
        offset = max(0, size - 5_000_000)

    with open(BOT_LOG, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(offset)
        data = f.read()

    return data.splitlines(), size


def main():
    state = _load_state()
    now = datetime.datetime.now(tz=UFA_TZ)

    # ── 0. Автофикс: остальные launchd-задачи проекта на месте? ──
    # Независимо от бота (main.py) — эта проверка нужна даже если сам бот жив.
    check_launchd_jobs(state, now)
    _save_state(state)

    # ── 1. Автофикс: бот жив? ──
    if not is_bot_alive():
        last_restart = state.get("last_restart")
        cooldown_ok = True
        if last_restart:
            mins = (now - datetime.datetime.fromisoformat(last_restart)).total_seconds() / 60
            cooldown_ok = mins > RESTART_COOLDOWN_MINUTES
        if cooldown_ok:
            restart_bot("процесс бота не найден (упал)")
            state["last_restart"] = now.isoformat()
            _save_state(state)
        else:
            log("бот не жив, но недавно уже перезапускали — ждём кулдаун")
        return  # после перезапуска остальной анализ в этом прогоне не нужен

    # ── 2. Читаем новый кусок лога ──
    lines, new_offset = read_new_lines(state)
    state["log_offset"] = new_offset

    # ── 2b. Автофикс: тихая смерть опроса Telegram (см. TELEGRAM_SILENT_MINUTES) ──
    # Проверяем даже если lines пуст — отсутствие новых строк вообще тем более
    # не даёт увидеть getUpdates.
    if any(TELEGRAM_POLL_PATTERN.search(line) for line in lines):
        state["telegram_last_seen"] = now.isoformat()
    else:
        last_seen = state.get("telegram_last_seen")
        if last_seen is None:
            # Первое наблюдение — не знаем, когда реально был последний
            # getUpdates (лог мог начаться раньше offset'а), не тревожимся
            # раньше времени.
            state["telegram_last_seen"] = now.isoformat()
        else:
            silent_minutes = (now - datetime.datetime.fromisoformat(last_seen)).total_seconds() / 60
            if silent_minutes > TELEGRAM_SILENT_MINUTES:
                last_restart = state.get("last_restart")
                cooldown_ok = True
                if last_restart:
                    mins = (now - datetime.datetime.fromisoformat(last_restart)).total_seconds() / 60
                    cooldown_ok = mins > RESTART_COOLDOWN_MINUTES
                if cooldown_ok:
                    restart_bot(f"Telegram getUpdates молчит {silent_minutes:.0f} мин — опрос, похоже, тихо умер")
                    state["last_restart"] = now.isoformat()
                    state["telegram_last_seen"] = now.isoformat()
                    _save_state(state)
                    return
                else:
                    log(f"getUpdates молчит {silent_minutes:.0f} мин, но недавно уже перезапускали — ждём кулдаун")

    if not lines:
        _save_state(state)
        return

    # ── 3. Автофикс: известные "лечится перезапуском" сигнатуры ──
    for sig in RESTART_SIGNATURES:
        if any(sig.search(line) for line in lines):
            last_restart = state.get("last_restart")
            cooldown_ok = True
            if last_restart:
                mins = (now - datetime.datetime.fromisoformat(last_restart)).total_seconds() / 60
                cooldown_ok = mins > RESTART_COOLDOWN_MINUTES
            if cooldown_ok:
                restart_bot(f"найдена известная сигнатура в логе: {sig.pattern}")
                state["last_restart"] = now.isoformat()
                _save_state(state)
                return
            else:
                log(f"сигнатура {sig.pattern} найдена, но недавно уже перезапускали — пропуск")
            break

    # ── 4. Внешние сбои: считаем провалы/успехи по категориям ──
    for key, cat in WATCH_CATEGORIES.items():
        fails = sum(1 for line in lines if cat["fail_pattern"].search(line))
        successes = sum(1 for line in lines if cat["success_pattern"].search(line))

        cat_state = state.setdefault("categories", {}).setdefault(key, {})

        if successes > 0:
            cat_state["last_success"] = now.isoformat()

        if fails == 0:
            continue

        last_success = cat_state.get("last_success")
        unhealthy_minutes = None
        if last_success:
            unhealthy_minutes = (now - datetime.datetime.fromisoformat(last_success)).total_seconds() / 60
        else:
            # успеха не видели вообще ни разу с начала наблюдения — считаем
            # нездоровым только если ошибки идут не первый прогон подряд
            unhealthy_minutes = UNHEALTHY_MINUTES + 1 if cat_state.get("ever_seen_fail") else 0
        cat_state["ever_seen_fail"] = True

        if unhealthy_minutes is not None and unhealthy_minutes > UNHEALTHY_MINUTES:
            last_alert = cat_state.get("last_alert")
            alert_cooldown_ok = True
            if last_alert:
                mins = (now - datetime.datetime.fromisoformat(last_alert)).total_seconds() / 60
                alert_cooldown_ok = mins > SERVICE_ALERT_COOLDOWN_MINUTES
            if alert_cooldown_ok:
                log(f"{cat['label']}: нездоров {unhealthy_minutes:.0f} мин, шлём уведомление")
                send_telegram(
                    f"⚠️ {cat['label']} не отвечает уже примерно {unhealthy_minutes/60:.1f} ч "
                    f"— проверок было много, успешных нет. Это похоже на проблему на стороне "
                    f"сервиса, а не в боте, но стоит знать."
                )
                cat_state["last_alert"] = now.isoformat()

    _save_state(state)


if __name__ == "__main__":
    main()
