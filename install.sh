#!/bin/bash
# Установка Bike Crash Tracker Bot на Debian (мультипользовательская версия)
set -e

echo "=== Установка Bike Crash Tracker Bot ==="

# - Проверка прав -------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo "Запусти скрипт от root: sudo bash install.sh"
    exit 1
fi

# - Зависимости ---------------------------------------------------------------
echo "[1/5] Устанавливаем зависимости..."
apt-get update -q
apt-get install -y python3 python3-venv python3-pip

# - Создаём пользователя ------------------------------------------------------
echo "[2/5] Создаём системного пользователя bike_bot..."
if ! id "bike_bot" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin bike_bot
fi

# - Директории ----------------------------------------------------------------
echo "[3/5] Создаём директории..."
mkdir -p /opt/bike_crash_bot
mkdir -p /var/lib/bike_crash_bot
chown bike_bot:bike_bot /var/lib/bike_crash_bot

# - Копируем файлы ------------------------------------------------------------
echo "[4/5] Копируем файлы..."
cp bike_crash_bot.py /opt/bike_crash_bot/
chown -R bike_bot:bike_bot /opt/bike_crash_bot

# - Виртуальное окружение и зависимости ---------------------------------------
echo "[5/5] Создаём виртуальное окружение..."
python3 -m venv /opt/bike_crash_bot/venv
/opt/bike_crash_bot/venv/bin/pip install --quiet "python-telegram-bot[job-queue]==21.6"
chown -R bike_bot:bike_bot /opt/bike_crash_bot/venv

# - Systemd сервис ------------------------------------------------------------
echo "[6/6] Устанавливаем systemd сервис..."
cp bike_crash_bot.service /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/bike_crash_bot.service 2>/dev/null || true
systemctl daemon-reload

echo ""
echo "=== Установка завершена! ==="
echo ""
echo "Следующий шаг - задай переменные окружения в сервисе:"
echo "  nano /etc/systemd/system/bike_crash_bot.service"
echo ""
echo "Замени значение:"
echo "  BOT_TOKEN=сюда_токен_от_botfather"
echo ""
echo "Потом запусти бота:"
echo "  systemctl daemon-reload"
echo "  systemctl enable bike_crash_bot"
echo "  systemctl start bike_crash_bot"
echo ""
echo "Проверить статус:"
echo "  systemctl status bike_crash_bot"
echo "  journalctl -u bike_crash_bot -f"
echo ""
echo "После запуска напиши /start в боте и пройди онбординг."