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

# Ротация bot.log — раньше рос бесконечно (доходил до 38+ МБ), ротируем
# при превышении ~15 МБ. error_watchdog.py сам переживает обрезку файла
# (сверяет offset с текущим размером), так что это безопасно на ходу.
LOG_MAX_BYTES=15000000
if [ -f bot.log ] && [ "$(wc -c < bot.log | tr -d ' ')" -gt "$LOG_MAX_BYTES" ]; then
    gzip -f bot.log.old 2>/dev/null
    mv bot.log bot.log.old
    echo "bot.log превысил лимит — заротирован в bot.log.old"
fi

# Запускаем
venv/bin/python3 main.py >> bot.log 2>&1 &
PID=$!

# Не давать маку спать — привязываем к PID бота
if command -v caffeinate &> /dev/null; then
  caffeinate -s -w $PID &
  echo "Caffeinate запущен"
fi
sleep 3

if kill -0 $PID 2>/dev/null; then
    echo "✅ Запущено (PID $PID)"
else
    echo "❌ Не удалось запустить — смотри bot.log"
fi
