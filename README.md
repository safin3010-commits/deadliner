# ДедЛайнер 🤖

Telegram-бот для студентов ТюмГУ+Нетология. Следит за дедлайнами, расписанием и оценками, присылает брифинги утром/днём/вечером и не даёт лениться.

**Работает на:** macOS, Linux, Windows

---

## Что умеет

- Утренний/дневной/вечерний брифинг с расписанием и дедлайнами
- Синхронизация заданий из LMS и Нетологии
- Расписание из Modeus
- Оценки из Modeus и LMS — уведомление при появлении новой
- Уведомления о письмах (Яндекс Почта)
- Уведомления из Яндекс Мессенджера
- Мониторинг беседы ВКонтакте
- Английский язык — слово и правило 3 раза в день
- Теория по предметам каждый день
- AI-анализ учёбы и мотивация (если есть ключ OpenRouter)
- `/quiz` — парсинг вопросов тестов LMS прямо во время прохождения

---

## Установка

### 1. Требования

- Python 3.11+
- Google Chrome (только для `/quiz`)

### 2. Клонируй репозиторий

    git clone https://github.com/safin3010-commits/deadliner.git
    cd deadliner

### 3. Запусти установщик

**macOS / Linux:**

    python3 setup.py

**Windows:**

    python setup.py

Установщик сам спросит все нужные данные, создаст `.env` и установит зависимости.

---

## Ручная настройка .env

    cp .env.example .env
    nano .env

### Обязательно

    TELEGRAM_TOKEN=токен_от_botfather
    MY_TELEGRAM_ID=твой_telegram_id
    USER_NAME=Твоё имя
    TIMEZONE=Asia/Yekaterinburg

### AI — OpenRouter (настоятельно рекомендуется)

Без этого не работают: брифинги с анализом, мотивация, теория, английский.

1. Зарегистрируйся на [openrouter.ai](https://openrouter.ai)
2. Keys → Create Key
3. Вставь в `.env`:

        OPENROUTER_KEY_1=твой_ключ

Модель `google/gemini-2.0-flash-exp:free` — бесплатная, лимита нет.

### Учёба

    MODEUS_USERNAME=логин@utmn.ru
    MODEUS_PASSWORD=пароль
    LMS_USERNAME=логин
    LMS_PASSWORD=пароль
    NETOLOGY_EMAIL=email
    NETOLOGY_PASSWORD=пароль

### Погода (опционально)

1. Ключ на [openweathermap.org](https://openweathermap.org)
2. Координаты на [latlong.net](https://www.latlong.net)

        OPENWEATHER_KEY=ключ
        WEATHER_LAT=54.7355
        WEATHER_LON=55.9578
        USER_CITY=Уфа

### Яндекс Почта (опционально)

Пароль приложения: Яндекс ID → Безопасность → Пароли приложений.

    YANDEX_MAIL=твой@yandex.ru
    YANDEX_APP_PASSWORD=пароль_приложения

### ВКонтакте (опционально)

    VK_CHAT_URL=https://vk.com/im?sel=НОМЕР_БЕСЕДЫ
    VK_PROXY=http://127.0.0.1:10808
    CHROME_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome

---

## Запуск

Бот работает в фоне. Закрывать терминал после запуска можно.

**macOS / Linux:**

    bash start.sh

**Windows:**

    start.bat

### Остановка

**macOS / Linux:**

    kill $(cat /tmp/deadliner.pid)

**Windows:**

    taskkill /F /IM python.exe

### Логи

    tail -50 bot.log

---

## /quiz — парсинг тестов LMS

**macOS** — автомониторинг:

1. Открой LMS в Chrome и войди
2. Напиши боту `/quiz`
3. Открой тест — вопросы придут автоматически
4. После теста `/quizstop`

**Windows / Linux** — через URL:

Напиши боту: `/quiz https://lms.utmn.ru/mod/quiz/attempt.php?attempt=...`

---

## Проблемы

**Бот не запускается** — смотри лог: `tail -50 bot.log`

**Расписание не работает** — проверь `MODEUS_USERNAME/PASSWORD` в `.env`

**AI не отвечает** — проверь `OPENROUTER_KEY_1` на [openrouter.ai](https://openrouter.ai)

**Quiz не находит вопросы** — убедись что залогинен в LMS через Chrome
