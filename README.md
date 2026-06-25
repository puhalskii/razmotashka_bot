# Трекер разматывания на мотике 🏍️

Телеграм-бот который следит - размотался ты или нет - и постит результат в канал.
Любой пользователь может подключить свой канал и пользоваться ботом независимо.
Старое сообщение в канале автоматически удаляется перед новым постом.

---

## Как начать пользоваться

1. Создай канал в Telegram
2. Добавь бота администратором канала с правом публикации сообщений
3. Напиши боту /start
4. Отправь username канала (@mycannel) или его ID (-1001234567890)
5. Готово - бот проверит доступ и подключится

---

## Режимы работы

### Режим опроса (/ask)
Бот пишет тебе по расписанию с кнопками "Неа 🚴" / "Ага 💥".
Ты отвечаешь - он постит в канал.

### Режим автопоста (/autopost)
Бот сам постит в канал по расписанию без вопросов.
Останавливается по /stop_autopost или /crashed.

---

## Команды

| Команда | Что делает |
|---|---|
| /start | Регистрация или главное меню |
| /ask | Переключить в режим опроса |
| /autopost | Переключить в режим автопоста |
| /start_autopost | Запустить автопост |
| /stop_autopost | Остановить автопост |
| /setfreq | Задать частоту (1h / 3d / 1w и т.д.) |
| /checkin | Ручной чекин с кнопками |
| /crashed | Зафиксировать падение вручную |
| /setchannel | Сменить канал |
| /status | Текущие настройки и статус |
| /help | Справка |

---

## Установка на Debian

### Что нужно заранее

- Debian 11/12 с доступом root
- Токен бота от @BotFather в Telegram

### Шаг 1 - Скачай файлы

Положи в одну папку три файла:
- `bike_crash_bot.py`
- `bike_crash_bot.service`
- `install.sh`

### Шаг 2 - Запусти установку

```bash
sudo bash install.sh
```

### Шаг 3 - Задай токен бота

```bash
nano /etc/systemd/system/bike_crash_bot.service
```

Замени значение:
```
Environment=BOT_TOKEN=сюда_токен_от_botfather
```

### Шаг 4 - Запусти бота

```bash
systemctl daemon-reload
systemctl enable bike_crash_bot
systemctl start bike_crash_bot
```

### Проверить что работает

```bash
systemctl status bike_crash_bot
journalctl -u bike_crash_bot -f
```

---

## Локальный запуск для теста

```bash
pip install "python-telegram-bot[job-queue]==21.6"

# Linux/Mac
export BOT_TOKEN="токен_от_botfather"
export DB_PATH="./state.db"
python bike_crash_bot.py

# Windows
set BOT_TOKEN=токен_от_botfather
set DB_PATH=state.db
python bike_crash_bot.py
```

---

## Управление сервисом

```bash
# Остановить
systemctl stop bike_crash_bot

# Перезапустить
systemctl daemon-reload && systemctl restart bike_crash_bot

# Последние 50 строк лога
journalctl -u bike_crash_bot -n 50

# Лог в реальном времени
journalctl -u bike_crash_bot -f
```

---

## Настройка частоты

Через команду /setfreq или кнопки в боте.

Форматы:
- `1h` - каждый час
- `6h` - каждые 6 часов
- `1d` - каждый день
- `3d` - каждые 3 дня
- `7d` - каждые 7 дней (по умолчанию)
- `2w` - каждые 2 недели

---

## Структура файлов на сервере

```
/opt/bike_crash_bot/
  bike_crash_bot.py     - основной скрипт
  venv/                 - виртуальное окружение Python

/var/lib/bike_crash_bot/
  state.db              - SQLite база со всеми пользователями

/etc/systemd/system/
  bike_crash_bot.service - systemd сервис
```

---

## Сосуществование с другими сервисами

Бот не занимает никаких портов и не конфликтует с xl2tpd, AdGuard Home
и другими системными сервисами. Работает изолированно через systemd
под отдельным пользователем bike_bot.
