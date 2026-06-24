"""
Трекер разматывания на мотике - Мультипользовательская версия
- Поддержка нескольких пользователей с индивидуальными настройками
- Онбординг при /start с проверкой прав на канал
- Каждый пользователь работает со своим каналом
- Индивидуальные задания в планировщике для каждого пользователя
"""

import os
import logging
import sqlite3
import json
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# - НАСТРОЙКИ (через переменные окружения) ------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "/var/lib/bike_crash_bot/state.db")

# - СОСТОЯНИЯ ДИАЛОГА ---------------------------------------------------------
WAITING_FOR_CHANNEL = 1
WAITING_FOR_ANSWER = 2
WAITING_FOR_FREQ = 3
WAITING_FOR_CHECK = 4

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# - БАЗА ДАННЫХ ---------------------------------------------------------------

def db_connect():
    """Открывает соединение с БД, создаёт таблицу если нет."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            channel_id   TEXT,
            mode         INTEGER DEFAULT 1,
            freq         TEXT DEFAULT '7d',
            running      INTEGER DEFAULT 0,
            last_msg_id  TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

def get_user(user_id):
    """Получает настройки пользователя."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT channel_id, mode, freq, running, last_msg_id FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if row:
            return {
                "channel_id": row[0],
                "mode": row[1],
                "freq": row[2],
                "running": row[3],
                "last_msg_id": row[4]
            }
        return None

def create_user(user_id):
    """Создаёт нового пользователя."""
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,)
        )
        conn.commit()

def update_user(user_id, **kwargs):
    """Обновляет настройки пользователя."""
    with db_connect() as conn:
        for key, value in kwargs.items():
            conn.execute(
                f"UPDATE users SET {key}=? WHERE user_id=?",
                (value, user_id)
            )
        conn.commit()

def delete_user(user_id):
    """Удаляет пользователя."""
    with db_connect() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        conn.commit()

def get_all_users():
    """Возвращает список всех user_id."""
    with db_connect() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [row[0] for row in rows]


# - ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---------------------------------------------------

def get_mode(user_id):
    """Возвращает текущий режим пользователя: 1 или 2."""
    user = get_user(user_id)
    return user["mode"] if user else 1

def get_freq_seconds(user_id):
    """Возвращает частоту опроса в секундах."""
    user = get_user(user_id)
    freq = user["freq"] if user else "7d"
    unit = freq[-1]
    val = int(freq[:-1])
    if unit == "h": return val * 3600
    if unit == "d": return val * 86400
    if unit == "w": return val * 604800
    return 604800

def get_freq_label(user_id):
    """Возвращает читаемую строку частоты."""
    user = get_user(user_id)
    freq = user["freq"] if user else "7d"
    unit = freq[-1]
    val = int(freq[:-1])
    labels = {"h": "ч", "d": "д", "w": "нед"}
    return f"каждые {val} {labels.get(unit, '?')}"

def is_running(user_id):
    """Режим 2 активен?"""
    user = get_user(user_id)
    return user["running"] == 1 if user else False

async def delete_last_channel_message(bot, user_id, channel_id):
    """Удаляет предыдущее сообщение бота в канале если есть."""
    user = get_user(user_id)
    last_id = user["last_msg_id"] if user else None
    if last_id:
        try:
            await bot.delete_message(chat_id=channel_id, message_id=int(last_id))
        except Exception as e:
            logger.warning(f"Не смог удалить сообщение {last_id} для {user_id}: {e}")

async def post_to_channel(bot, user_id, channel_id, text):
    """Удаляет старое сообщение и постит новое, сохраняет message_id."""
    await delete_last_channel_message(bot, user_id, channel_id)
    try:
        msg = await bot.send_message(chat_id=channel_id, text=text)
        update_user(user_id, last_msg_id=str(msg.message_id))
        logger.info(f"Запостил в канал для {user_id}: {text}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки в канал для {user_id}: {e}")
        return False


# - ЗАДАНИЯ ПО РАСПИСАНИЮ -----------------------------------------------------

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    """Основное задание по расписанию - режим 1 или 2."""
    user_id = context.job.data["user_id"]
    user = get_user(user_id)
    if not user:
        # Пользователь удалён, удаляем задание
        context.job.schedule_removal()
        return

    mode = user["mode"]
    channel_id = user["channel_id"]
    today = datetime.now().strftime("%d.%m.%Y")

    if mode == 1:
        # Режим 1 - спрашиваем
        keyboard = [["Неа 🚴", "Ага 💥"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🏍️ *Плановая проверка!*\n\nРазмотался, дурак?",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение {user_id}: {e}")
    elif mode == 2 and user["running"] == 1:
        # Режим 2 - постим сами без вопросов
        await post_to_channel(
            context.bot,
            user_id,
            channel_id,
            f"📅 {today}: всё ещё не размотался 🚴✅"
        )

def schedule_for_user(app, user_id):
    """Создаёт или обновляет задание для пользователя."""
    # Удаляем старое задание если есть
    for job in app.job_queue.jobs():
        if job.data and job.data.get("user_id") == user_id:
            job.schedule_removal()

    user = get_user(user_id)
    if not user or not user["channel_id"]:
        return

    # В режиме 2 если running=0 - не создаём задание
    if user["mode"] == 2 and user["running"] == 0:
        return

    interval = get_freq_seconds(user_id)
    app.job_queue.run_repeating(
        scheduled_job,
        interval=interval,
        first=10,
        name=f"user_{user_id}",
        data={"user_id": user_id}
    )
    logger.info(f"Задание для {user_id} запланировано: {get_freq_label(user_id)}")

async def reschedule_all(app):
    """Перепланирует все задания для всех пользователей."""
    # Удаляем все задания
    for job in app.job_queue.jobs():
        job.schedule_removal()

    # Создаём заново для всех пользователей
    for user_id in get_all_users():
        schedule_for_user(app, user_id)


# - ОНБОРДИНГ /start ----------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало работы - онбординг с проверкой канала."""
    user_id = update.effective_user.id
    
    # Создаём пользователя если его нет
    create_user(user_id)
    user = get_user(user_id)
    
    # Если уже есть канал - показываем меню
    if user["channel_id"]:
        await show_main_menu(update, context, user_id)
        return ConversationHandler.END
    
    # Запрашиваем канал
    await update.message.reply_text(
        "👋 *Привет! Я бот для трекинга разматывания на мотике.*\n\n"
        "Для начала работы мне нужен канал, куда я буду постить результаты.\n\n"
        "**Важно:** Бот должен быть добавлен как *администратор* в этот канал "
        "(даже если канал публичный).\n\n"
        "Отправь мне название канала:\n"
        "• Для публичного: `@имя_канала`\n"
        "• Для приватного: числовой ID канала (узнать у @userinfobot)",
        parse_mode="Markdown"
    )
    return WAITING_FOR_CHANNEL

async def handle_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод канала и проверяет доступ."""
    user_id = update.effective_user.id
    channel_input = update.message.text.strip()
    
    # Проверяем, что бот может постить в канал
    try:
        # Пробуем отправить тестовое сообщение
        test_msg = await context.bot.send_message(
            chat_id=channel_input,
            text="✅ Бот успешно подключён к каналу!"
        )
        # Удаляем тестовое сообщение
        await context.bot.delete_message(
            chat_id=channel_input,
            message_id=test_msg.message_id
        )
        
        # Сохраняем канал
        update_user(user_id, channel_id=channel_input)
        
        await update.message.reply_text(
            f"✅ Канал успешно подключён!\n\n"
            "Теперь настроим частоту проверок.\n"
            "По умолчанию: раз в неделю (7d).\n\n"
            "Ты можешь изменить частоту позже командой /setfreq",
            reply_markup=ReplyKeyboardRemove()
        )
        
        # Показываем главное меню
        await show_main_menu(update, context, user_id)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Ошибка проверки канала для {user_id}: {e}")
        await update.message.reply_text(
            "❌ Не удалось отправить сообщение в канал.\n\n"
            "Убедись, что:\n"
            "1. Бот добавлен в канал как администратор\n"
            "2. Название канала введено правильно\n"
            "3. Канал существует\n\n"
            "Попробуй ещё раз или напиши /cancel для отмены."
        )
        return WAITING_FOR_CHANNEL

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id=None):
    """Показывает главное меню."""
    if user_id is None:
        user_id = update.effective_user.id
    
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Начни с /start")
        return
    
    mode = user["mode"]
    freq = get_freq_label(user_id)
    running = "запущен ✅" if user["running"] == 1 else "остановлен ⏸"
    channel = user["channel_id"]
    
    message = (
        f"👋 *Трекер разматывания на Мотике!*\n\n"
        f"📌 Канал: `{channel}`\n"
        f"📊 Режим: *{mode}*\n"
        f"⏱ Частота: *{freq}*\n"
        f"▶️ Автопост: *{running}*\n\n"
        "*Команды:*\n"
        "/ask - режим опроса\n"
        "/autopost - режим автопоста\n"
        "/setfreq - задать частоту\n"
        "/start_autopost - запустить автопост\n"
        "/stop_autopost - остановить автопост\n"
        "/crashed - зафиксировать падение\n"
        "/checkin - ручной чекин\n"
        "/status - текущие настройки\n"
        "/reset - сбросить настройки\n"
        "/help - справка"
    )
    
    if update and update.message:
        await update.message.reply_text(message, parse_mode="Markdown")
    else:
        # Для внутреннего вызова
        await context.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")


# - КОМАНДА /reset ------------------------------------------------------------

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает настройки пользователя."""
    user_id = update.effective_user.id
    update_user(user_id, channel_id=None, mode=1, freq="7d", running=0, last_msg_id=None)
    
    # Удаляем задание
    for job in context.application.job_queue.jobs():
        if job.data and job.data.get("user_id") == user_id:
            job.schedule_removal()
    
    await update.message.reply_text(
        "🔄 Настройки сброшены. Начни заново с /start",
        reply_markup=ReplyKeyboardRemove()
    )


# - КОМАНДА /status -----------------------------------------------------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий статус пользователя."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    mode = user["mode"]
    freq = get_freq_label(user_id)
    running = "запущен ✅" if user["running"] == 1 else "остановлен ⏸"
    last_id = user["last_msg_id"] or "нет"
    channel = user["channel_id"]
    
    await update.message.reply_text(
        f"📊 *Текущий статус*\n\n"
        f"👤 ID: `{user_id}`\n"
        f"📌 Канал: `{channel}`\n"
        f"📊 Режим: *{mode}*\n"
        f"⏱ Частота: *{freq}*\n"
        f"▶️ Автопост: *{running}*\n"
        f"📨 Последнее сообщение: `{last_id}`",
        parse_mode="Markdown"
    )


# - КОМАНДА /ask --------------------------------------------------------------

async def set_mode1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает режим опроса."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    update_user(user_id, mode=1, running=0)
    # Перепланируем задание
    schedule_for_user(context.application, user_id)
    
    await update.message.reply_text(
        f"✅ Режим опроса включён.\n"
        f"Буду спрашивать тебя {get_freq_label(user_id)}."
    )


# - КОМАНДА /autopost ---------------------------------------------------------

async def set_mode2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает режим автопоста."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    update_user(user_id, mode=2)
    
    await update.message.reply_text(
        "✅ Режим автопоста включён.\n"
        "Буду постить в канал сам без вопросов.\n"
        "Используй /start_autopost чтобы запустить, /stop_autopost чтобы остановить."
    )


# - КОМАНДА /start_autopost ---------------------------------------------------

async def start_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает автопост."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    if user["mode"] != 2:
        await update.message.reply_text("Сначала переключись в режим автопоста командой /autopost")
        return
    
    update_user(user_id, running=1)
    # Перепланируем задание
    schedule_for_user(context.application, user_id)
    
    await update.message.reply_text(
        f"▶️ Автопост запущен! Постю {get_freq_label(user_id)}.\n"
        "Останови через /stop_autopost или зафиксируй падение через /crashed."
    )


# - КОМАНДА /stop_autopost ----------------------------------------------------

async def stop_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Останавливает автопост."""
    user_id = update.effective_user.id
    update_user(user_id, running=0)
    
    # Удаляем задание
    for job in context.application.job_queue.jobs():
        if job.data and job.data.get("user_id") == user_id:
            job.schedule_removal()
    
    await update.message.reply_text("⏸ Автопост остановлен.")


# - КОМАНДА /setfreq ----------------------------------------------------------

async def setfreq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает диалог установки частоты."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    keyboard = [["1h", "6h", "12h"], ["1d", "3d", "7d"], ["14d", "1w", "2w"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    
    await update.message.reply_text(
        "⏱ *Настройка частоты*\n\n"
        "Выбери или напиши своё значение:\n"
        "• `1h` - каждый час\n"
        "• `6h` - каждые 6 часов\n"
        "• `1d` - каждый день\n"
        "• `3d` - каждые 3 дня\n"
        "• `7d` - каждую неделю\n"
        "• `2w` - каждые 2 недели",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return WAITING_FOR_FREQ

async def handle_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод частоты."""
    user_id = update.effective_user.id
    text = update.message.text.strip().lower()
    
    # Валидация формата
    if len(text) < 2 or text[-1] not in ("h", "d", "w") or not text[:-1].isdigit():
        await update.message.reply_text(
            "❌ Неверный формат. Примеры: `1h`, `3d`, `7d`, `2w`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_FOR_FREQ
    
    update_user(user_id, freq=text)
    
    # Перепланируем задание
    schedule_for_user(context.application, user_id)
    
    await update.message.reply_text(
        f"✅ Частота установлена: {get_freq_label(user_id)}",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# - КОМАНДА /checkin ----------------------------------------------------------

async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной чекин."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    keyboard = [["Неа 🚴", "Ага 💥"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    
    await update.message.reply_text(
        "🏍️ *Ручной чекин*\n\n*Размотался? Признавайся.*",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return WAITING_FOR_CHECK

async def handle_check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответ на ручной чекин."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return ConversationHandler.END
    
    text = update.message.text
    today = datetime.now().strftime("%d.%m.%Y")
    
    if "Неа" in text:
        await post_to_channel(
            context.bot,
            user_id,
            user["channel_id"],
            f"📅 {today}: всё ещё не размотался 🚴✅"
        )
        await update.message.reply_text(
            "👍 Отправил в канал!",
            reply_markup=ReplyKeyboardRemove()
        )
    elif "Ага" in text:
        await post_to_channel(
            context.bot,
            user_id,
            user["channel_id"],
            f"📅 {today}: всё-таки размотался 💥"
        )
        update_user(user_id, running=0)  # останавливаем автопост если был
        await update.message.reply_text(
            "💥 Отправил в канал. Автопост остановлен.",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            "Тыкай в кнопки!",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_FOR_CHECK
    
    return ConversationHandler.END


# - КОМАНДА /crashed ----------------------------------------------------------

async def crashed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фиксирует падение вручную."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    today = datetime.now().strftime("%d.%m.%Y")
    
    await post_to_channel(
        context.bot,
        user_id,
        user["channel_id"],
        f"📅 {today}: всё-таки размотался 💥"
    )
    update_user(user_id, running=0)
    
    # Удаляем задание
    for job in context.application.job_queue.jobs():
        if job.data and job.data.get("user_id") == user_id:
            job.schedule_removal()
    
    await update.message.reply_text(
        "💥 Зафиксировал падение. Автопост остановлен.\n"
        "Надеюсь всё норм 🤕"
    )


# - КОМАНДА /help -------------------------------------------------------------

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает справку."""
    await update.message.reply_text(
        "📖 *Справка по боту*\n\n"
        "*РЕЖИМЫ:*\n"
        "/ask - режим опроса. Бот спрашивает тебя по расписанию,\n"
        "  ты отвечаешь кнопками, он постит в канал.\n"
        "/autopost - режим автопоста. Бот сам постит в канал по\n"
        "  расписанию без вопросов.\n\n"
        "*УПРАВЛЕНИЕ:*\n"
        "/start_autopost - запустить автопост\n"
        "/stop_autopost - остановить автопост\n"
        "/crashed - зафиксировать падение и остановить автопост\n"
        "/checkin - ручной чекин с кнопками\n\n"
        "*НАСТРОЙКИ:*\n"
        "/setfreq - задать частоту\n"
        "/status - показать текущие настройки\n"
        "/reset - сбросить все настройки\n\n"
        "*ПРОЧЕЕ:*\n"
        "/start - главное меню\n"
        "/help - эта справка",
        parse_mode="Markdown"
    )


# - ЛОВИМ КНОПКИ ИЗ ПЛАНИРОВЩИКА ---------------------------------------------

async def catch_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит нажатия кнопок из сообщений планировщика вне диалога."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not user["channel_id"]:
        await update.message.reply_text("Сначала настрой бота через /start")
        return
    
    text = update.message.text or ""
    today = datetime.now().strftime("%d.%m.%Y")
    
    if "Неа" in text:
        await post_to_channel(
            context.bot,
            user_id,
            user["channel_id"],
            f"📅 {today}: всё ещё не размотался 🚴✅"
        )
        await update.message.reply_text(
            "👍 Отправил в канал!",
            reply_markup=ReplyKeyboardRemove()
        )
    elif "Ага" in text:
        await post_to_channel(
            context.bot,
            user_id,
            user["channel_id"],
            f"📅 {today}: всё-таки размотался 💥"
        )
        update_user(user_id, running=0)
        await update.message.reply_text(
            "💥 Отправил в канал. Автопост остановлен.",
            reply_markup=ReplyKeyboardRemove()
        )


# - ОТМЕНА --------------------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет текущий диалог."""
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# - ЗАПУСК --------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Установи переменную окружения BOT_TOKEN.")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Диалог онбординга
    onboarding_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_channel_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # Диалог для /setfreq
    freq_handler = ConversationHandler(
        entry_points=[CommandHandler("setfreq", setfreq)],
        states={
            WAITING_FOR_FREQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_freq)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # Диалог для /checkin
    check_handler = ConversationHandler(
        entry_points=[CommandHandler("checkin", checkin)],
        states={
            WAITING_FOR_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_check_answer)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    app.add_handler(onboarding_handler)
    app.add_handler(freq_handler)
    app.add_handler(check_handler)
    
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("ask", set_mode1))
    app.add_handler(CommandHandler("autopost", set_mode2))
    app.add_handler(CommandHandler("start_autopost", start_autopost))
    app.add_handler(CommandHandler("stop_autopost", stop_autopost))
    app.add_handler(CommandHandler("crashed", crashed))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    
    # Кнопки из планировщика
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(Неа|Ага)"),
        catch_buttons
    ))
    
    # Запускаем задания для всех пользователей
    app.job_queue.start()
    for user_id in get_all_users():
        schedule_for_user(app, user_id)
    
    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    import asyncio
    import sys
    
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()
