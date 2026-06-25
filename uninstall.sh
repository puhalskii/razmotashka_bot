#!/bin/bash
# Деинсталляция Bike Crash Tracker Bot с Debian
# Использование:
#   sudo bash uninstall.sh           - удаляет приложение, спрашивает про БД и пользователя
#   sudo bash uninstall.sh --purge   - удаляет всё без вопросов (включая БД и логи)
set -e

PURGE=0
if [ "$1" == "--purge" ]; then
    PURGE=1
fi

echo "=== Деинсталляция Bike Crash Tracker Bot ==="

# - Проверка прав -------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo "Запусти скрипт от root: sudo bash uninstall.sh"
    exit 1
fi

# - Останавливаем и отключаем сервис -------------------------------------------
echo "[1/4] Останавливаем сервис..."
systemctl stop bike_crash_bot 2>/dev/null || true
systemctl disable bike_crash_bot 2>/dev/null || true

# - Удаляем systemd unit --------------------------------------------------------
echo "[2/4] Удаляем systemd сервис..."
rm -f /etc/systemd/system/bike_crash_bot.service
systemctl daemon-reload

# - Удаляем приложение (скрипт, venv, лог действий) ----------------------------
echo "[3/4] Удаляем /opt/bike_crash_bot (скрипт, venv, user_actions.log)..."
rm -rf /opt/bike_crash_bot

# - БД с пользователями ---------------------------------------------------------
echo "[4/4] База данных пользователей в /var/lib/bike_crash_bot/"
if [ -d /var/lib/bike_crash_bot ]; then
    if [ "$PURGE" -eq 1 ]; then
        rm -rf /var/lib/bike_crash_bot
        echo "  Удалена (--purge)."
    else
        read -p "  Удалить базу данных со всеми пользователями? [y/N] " answer < /dev/tty
        if [[ "$answer" =~ ^[Yy]$ ]]; then
            rm -rf /var/lib/bike_crash_bot
            echo "  Удалена."
        else
            echo "  Оставлена: /var/lib/bike_crash_bot"
        fi
    fi
else
    echo "  Не найдена, пропускаем."
fi

# - Системный пользователь bike_bot ---------------------------------------------
if id "bike_bot" &>/dev/null; then
    if [ "$PURGE" -eq 1 ]; then
        userdel bike_bot 2>/dev/null || true
        echo "Системный пользователь bike_bot удалён (--purge)."
    else
        read -p "Удалить системного пользователя bike_bot? [y/N] " answer < /dev/tty
        if [[ "$answer" =~ ^[Yy]$ ]]; then
            userdel bike_bot 2>/dev/null || true
            echo "Пользователь bike_bot удалён."
        else
            echo "Пользователь bike_bot оставлен."
        fi
    fi
fi

echo ""
echo "=== Деинсталляция завершена! ==="
