#!/bin/bash

# Убиваем старые процессы jarvis
while true; do
    PIDS=$(ps aux | grep "anti_laziness_bot.*jarvis\.py" | grep -v grep | awk '{print $2}')
    if [ -z "$PIDS" ]; then
        break
    fi
    echo "Убиваю jarvis: $PIDS"
    echo "$PIDS" | xargs kill -9 2>/dev/null
    sleep 2
done
echo "Старые процессы Jarvis убиты"

# Не давать маку спать
caffeinate -i &
CAFF_PID=$!
echo "Caffeinate PID: $CAFF_PID"

# Запускаем jarvis
PYTHONUNBUFFERED=1 /Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python /Users/ilnursafin/anti_laziness_bot/jarvis.py >> /Users/ilnursafin/anti_laziness_bot/jarvis.log 2>&1 &
PID=$!
sleep 3

COUNT=$(ps aux | grep "anti_laziness_bot.*jarvis\.py" | grep -v grep | wc -l | tr -d ' ')
echo "✅ Jarvis запущен: $COUNT экземпляр (PID $PID)"
