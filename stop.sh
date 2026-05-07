#!/bin/bash
cd "$(dirname "$0")"

PID_FILE="/tmp/deadliner.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID"
        echo "✅ Бот остановлен (PID $PID)"
    else
        echo "⚠️ Процесс $PID уже не запущен"
    fi
    rm -f "$PID_FILE"
else
    echo "⚠️ PID файл не найден — ищу процесс..."
    pkill -f "main.py" && echo "✅ Остановлен" || echo "❌ Процесс не найден"
fi
