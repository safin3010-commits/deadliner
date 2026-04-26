#!/bin/bash
cd "$(dirname "$0")"

# Убиваем старый процесс по PID файлу
PID_FILE="/tmp/deadliner.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Убиваю старый процесс PID $OLD_PID..."
        kill -9 "$OLD_PID" 2>/dev/null
        sleep 2
    fi
    rm -f "$PID_FILE"
fi
echo "Старые процессы убиты"

# Убиваем все оставшиеся экземпляры на всякий случай
pkill -f "main\.py" 2>/dev/null; sleep 1

# Не давать маку спать
if command -v caffeinate &> /dev/null; then
  caffeinate -i &
  echo "Caffeinate запущен"
fi

# Запускаем
venv/bin/python3 main.py >> bot.log 2>&1 &
PID=$!
sleep 3

if kill -0 $PID 2>/dev/null; then
    echo "✅ Запущено (PID $PID)"
else
    echo "❌ Не удалось запустить — смотри bot.log"
fi
