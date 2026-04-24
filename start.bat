@echo off
cd /d "%~dp0"

:: Убиваем старый процесс
if exist "%TEMP%\deadliner.pid" (
    set /p OLD_PID=<"%TEMP%\deadliner.pid"
    taskkill /PID %OLD_PID% /F >nul 2>&1
    del "%TEMP%\deadliner.pid"
)
echo Старые процессы убиты

:: Запускаем
start /b venv\Scripts\python.exe main.py >> bot.log 2>&1
echo Запускаем бот...
timeout /t 3 /nobreak >nul

:: Проверяем
for /f "tokens=2" %%i in ('tasklist /fi "imagename eq python.exe" /fo list ^| find "PID"') do (
    echo ✅ Запущено (PID %%i)
    goto :done
)
echo ❌ Не удалось запустить — смотри bot.log
:done
