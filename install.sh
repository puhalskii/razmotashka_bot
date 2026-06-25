#!/bin/bash
# Установка / обновление Bike Crash Tracker Bot на Debian
# Скрипт сам скачивает всё необходимое с GitHub - локальная копия репозитория не нужна.
# Использование:
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/puhalskii/razmotashka_bot/main/install.sh)"
#   sudo bash -c "$(curl -fsSL .../install.sh)" -- --upgrade   - обновить уже установленного бота
set -e

REPO_RAW_BASE="https://raw.githubusercontent.com/puhalskii/razmotashka_bot/main"
SERVICE_FILE="/etc/systemd/system/bike_crash_bot.service"

UPGRADE=0
for arg in "$@"; do
    case "$arg" in
        -u|--upgrade) UPGRADE=1 ;;
    esac
done

if [ "$UPGRADE" -eq 1 ]; then
    echo "=== Обновление Bike Crash Tracker Bot ==="
else
    echo "=== Установка Bike Crash Tracker Bot ==="
fi

# - Проверка прав -------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo "Запусти скрипт от root: sudo bash install.sh"
    exit 1
fi

# - При обновлении бот должен уже быть установлен -----------------------------
if [ "$UPGRADE" -eq 1 ] && [ ! -f "$SERVICE_FILE" ]; then
    echo "Бот не найден ($SERVICE_FILE не существует)."
    echo "Сначала выполни обычную установку (без --upgrade)."
    exit 1
fi

# - Зависимости ---------------------------------------------------------------
echo "[1/7] Устанавливаем зависимости..."
apt-get update -q
apt-get install -y python3 python3-venv python3-pip curl

# - Токен бота -----------------------------------------------------------------
if [ "$UPGRADE" -eq 1 ]; then
    echo "[2/7] Беру токен бота из текущей установки..."
    BOT_TOKEN=$(grep -oP '^Environment=BOT_TOKEN=\K.*' "$SERVICE_FILE" || true)
    if [ -z "$BOT_TOKEN" ]; then
        echo "Не нашёл токен в $SERVICE_FILE. Обновление невозможно, переустанови бота без --upgrade."
        exit 1
    fi
else
    echo "[2/7] Настройка токена бота..."
    echo "Создай бота у @BotFather в Telegram и возьми у него токен."
    while true; do
        read -s -p "Вставь токен бота: " BOT_TOKEN < /dev/tty
        echo
        if [ -z "$BOT_TOKEN" ]; then
            echo "Токен не может быть пустым, попробуй ещё раз."
            continue
        fi
        RESPONSE=$(curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/getMe" || true)
        if echo "$RESPONSE" | grep -q '"ok":true'; then
            BOT_USERNAME=$(echo "$RESPONSE" | grep -oP '"username":"\K[^"]+' || true)
            echo "Токен подтверждён, бот: @${BOT_USERNAME:-неизвестно}"
            break
        else
            echo "Telegram не подтвердил этот токен. Проверь его и вставь снова (или Ctrl+C для отмены)."
        fi
    done
fi

# - Создаём пользователя ------------------------------------------------------
echo "[3/7] Создаём системного пользователя bike_bot..."
if ! id "bike_bot" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin bike_bot
fi

# - Директории ----------------------------------------------------------------
echo "[4/7] Создаём директории..."
mkdir -p /opt/bike_crash_bot
mkdir -p /var/lib/bike_crash_bot
chown bike_bot:bike_bot /var/lib/bike_crash_bot

# - Скачиваем файлы бота с GitHub -----------------------------------------------
echo "[5/7] Скачиваем файлы бота с GitHub..."
if [ -f /opt/bike_crash_bot/bike_crash_bot.py ]; then
    cp /opt/bike_crash_bot/bike_crash_bot.py /opt/bike_crash_bot/bike_crash_bot.py.bak
    echo "  Старая версия сохранена в bike_crash_bot.py.bak"
fi
curl -fsSL "${REPO_RAW_BASE}/bike_crash_bot.py" -o /opt/bike_crash_bot/bike_crash_bot.py
curl -fsSL "${REPO_RAW_BASE}/uninstall.sh" -o /opt/bike_crash_bot/uninstall.sh
chmod +x /opt/bike_crash_bot/uninstall.sh
chown -R bike_bot:bike_bot /opt/bike_crash_bot

# - Виртуальное окружение и зависимости ---------------------------------------
echo "[6/7] Обновляем виртуальное окружение..."
python3 -m venv /opt/bike_crash_bot/venv
/opt/bike_crash_bot/venv/bin/pip install --quiet "python-telegram-bot[job-queue]==21.6"
chown -R bike_bot:bike_bot /opt/bike_crash_bot/venv

# - Systemd сервис --------------------------------------------------------------
echo "[7/7] Настраиваем и (пере)запускаем systemd сервис..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Bike Crash Tracker Bot
After=network.target

[Service]
Type=simple
User=bike_bot
WorkingDirectory=/opt/bike_crash_bot
ExecStart=/opt/bike_crash_bot/venv/bin/python bike_crash_bot.py
Restart=always
RestartSec=10

# - Переменные окружения ------------------------------------------------------
Environment=BOT_TOKEN=${BOT_TOKEN}
Environment=DB_PATH=/var/lib/bike_crash_bot/state.db

# - Логи ----------------------------------------------------------------------
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bike_crash_bot

[Install]
WantedBy=multi-user.target
EOF
chmod 600 "$SERVICE_FILE"

systemd-analyze verify "$SERVICE_FILE" 2>/dev/null || true
systemctl daemon-reload
systemctl enable bike_crash_bot
systemctl restart bike_crash_bot

echo ""
if systemctl is-active --quiet bike_crash_bot; then
    if [ "$UPGRADE" -eq 1 ]; then
        echo "=== Обновление завершено, бот перезапущен! ==="
        if [ -f /opt/bike_crash_bot/bike_crash_bot.py.bak ]; then
            echo "Если что-то пошло не так - откат:"
            echo "  sudo cp /opt/bike_crash_bot/bike_crash_bot.py.bak /opt/bike_crash_bot/bike_crash_bot.py"
            echo "  sudo systemctl restart bike_crash_bot"
        fi
    else
        echo "=== Установка завершена, бот запущен! ==="
        echo ""
        if [ -n "$BOT_USERNAME" ]; then
            echo "Открой бота и напиши /start, чтобы начать онбординг:"
            echo "  https://t.me/${BOT_USERNAME}"
        else
            echo "Найди своего бота в Telegram и напиши ему /start, чтобы начать онбординг."
        fi
    fi
else
    echo "=== Готово, но сервис не запустился. ==="
    echo "Посмотри логи: journalctl -u bike_crash_bot -n 50"
fi
echo ""
echo "Проверить статус:"
echo "  systemctl status bike_crash_bot"
echo "  journalctl -u bike_crash_bot -f"
echo ""
echo "Обновить бота:"
echo "  sudo bash -c \"\$(curl -fsSL ${REPO_RAW_BASE}/install.sh)\" -- --upgrade"
echo ""
echo "Деинсталляция:"
echo "  sudo bash /opt/bike_crash_bot/uninstall.sh"
