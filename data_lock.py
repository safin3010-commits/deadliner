"""
Общие примитивы для безопасной работы с JSON-файлами в data/, которые
читают и пишут независимые процессы: сам бот (scheduler.py), наставник
(scripts/mentor_checkin.py) и почасовой сторож (scripts/mentor_hourly_watch.py).

atomic_write_json — запись через temp-файл + os.replace, чтобы аварийное
завершение процесса посреди записи не оставляло битый JSON.

file_lock — advisory-лок на отдельном .lock файле (fcntl.flock), чтобы
read-modify-write из двух процессов не терял чужую запись между чтением
и перезаписью одного и того же файла.
"""
import fcntl
import json
import os


def atomic_write_json(path: str, data) -> None:
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, path)


class file_lock:
    def __init__(self, path: str):
        self.lock_path = path + ".lock"
        self.fh = None

    def __enter__(self):
        dirname = os.path.dirname(self.lock_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        self.fh = open(self.lock_path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
