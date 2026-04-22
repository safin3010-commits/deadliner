#!/bin/bash
# Убиваем ВСЕ процессы бота
while true; do
    PIDS=$(ps aux | grep "anti_laziness_bot.*main\.py" | grep -v grep | awk '{print $2}')
    if [ -z "$PIDS" ]; then
        break
    fi
    echo "Убиваю: $PIDS"
    echo "$PIDS" | xargs kill -9 2>/dev/null
    sleep 2
done
echo "Все старые процессы убиты"

# Запускаем один
/Users/ilnursafin/anti_laziness_bot/venv/bin/python3 /Users/ilnursafin/anti_laziness_bot/main.py >> /Users/ilnursafin/anti_laziness_bot/bot.log 2>&1 &
PID=$!
sleep 3

COUNT=$(ps aux | grep "anti_laziness_bot.*main\.py" | grep -v grep | wc -l | tr -d ' ')
echo "✅ Запущено экземпляров: $COUNT (PID $PID)"
