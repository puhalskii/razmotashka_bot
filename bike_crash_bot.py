"""
Трекер разматывания на мотике - мульти-юзер версия
- Каждый пользователь регистрирует свой канал
- Режим 1: спрашивает по расписанию, ждёт ответа
- Режим 2: постит сам по расписанию пока не остановишь или не скажешь что размотался
- Удаляет предыдущее сообщение в канале перед новым постом
- Хранит состояние каждого пользователя в SQLite
"""

import os
import logging
import sqlite3
from datetime import datetime
from telegram import Update, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# - НАСТРОЙКИ -----------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH   = os.environ.get("DB_PATH", "/var/lib/bike_crash_bot/state.db")
ACTIONS_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_actions.log")

# Кнопки опроса - общие для всех пользователей, не настраиваются
BTN_NO  = "Неа 🚴"
BTN_YES = "Ага 💥"

# Тексты вопросов/постов/ответов по умолчанию (тема "Размотался").
# Пользователь может переопределить через /customtexts - см. WIZARD_FIELDS.
TEXT_DEFAULTS = {
    "text_question":      "Размотался, дурак?",
    "text_checkin":       "Размотался? Признавайся.",
    "text_post_no":       "всё ещё не размотался 🚴✅",
    "text_post_yes":      "всё-таки размотался 💥",
    "text_reply_no":      "Шикос, отправил в канал. Пусть завидуют 🎉",
    "text_reply_yes":     "Ну в целом ожидаемо. Отправил в канал пусть посмеются.",
    "text_reply_invalid": "Тыкай в кнопки, тупица!",
    "text_crashed_reply": "💥 Зафиксировал. Автопост остановлен.\nНадеюсь всё норм, дурак 🤕",
}
MAX_CUSTOM_TEXT_LEN = 200

# - СОСТОЯНИЯ ДИАЛОГОВ --------------------------------------------------------
WAITING_FOR_CHANNEL    = 10
WAITING_FOR_FREQ       = 20
WAITING_FOR_CHECK      = 30
WAITING_FOR_CUSTOM_TEXT = 40

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# - БАЗА ДАННЫХ ---------------------------------------------------------------

def _sql_escape(value):
    """Эскейпит одиночные кавычки для вставки литерала в SQL (для своих, не пользовательских строк)."""
    return value.replace("'", "''")

def _migrate_text_columns(conn):
    """Добавляет колонки с кастомными текстами в уже существующие базы (старые версии бота)."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col, default in TEXT_DEFAULTS.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT '{_sql_escape(default)}'")

def db_connect():
    """Открывает соединение с БД, создаёт таблицу/колонки если нет."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    text_columns = ", ".join(
        f"{col} TEXT DEFAULT '{_sql_escape(default)}'" for col, default in TEXT_DEFAULTS.items()
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            channel_id TEXT,
            mode       INTEGER DEFAULT 1,
            freq       TEXT    DEFAULT '7d',
            running    INTEGER DEFAULT 0,
            last_msg_id INTEGER,
            {text_columns}
        )
    """)
    _migrate_text_columns(conn)
    conn.commit()
    return conn

def user_get(user_id):
    """Возвращает строку пользователя или None."""
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

def user_set(user_id, **kwargs):
    """Создаёт или обновляет поля пользователя."""
    with db_connect() as conn:
        existing = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            fields = ", ".join(f"{k}=?" for k in kwargs)
            conn.execute(f"UPDATE users SET {fields} WHERE user_id=?", (*kwargs.values(), user_id))
        else:
            kwargs["user_id"] = user_id
            fields  = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            conn.execute(f"INSERT INTO users ({fields}) VALUES ({placeholders})", list(kwargs.values()))
        conn.commit()

def all_users():
    """Возвращает всех пользователей."""
    with db_connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


# - ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---------------------------------------------------

def freq_to_seconds(freq):
    """Переводит строку частоты в секунды."""
    unit = freq[-1]
    val  = int(freq[:-1])
    if unit == "h": return val * 3600
    if unit == "d": return val * 86400
    if unit == "w": return val * 604800
    return 604800

def freq_to_label(freq):
    """Возвращает читаемую строку частоты."""
    unit   = freq[-1]
    val    = int(freq[:-1])
    labels = {"h": "ч", "d": "д", "w": "нед"}
    return f"каждые {val} {labels.get(unit, '?')}"

def job_name(user_id):
    """Имя задания планировщика для пользователя."""
    return f"job_{user_id}"

def log_action(user_id, action):
    """Добавляет читаемую запись о действии пользователя в лог-файл рядом со скриптом."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    with open(ACTIONS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"Пользователь {user_id} {now} сделал: {action}\n")

async def delete_last_message(bot, user):
    """Удаляет последнее сообщение бота в канале пользователя."""
    if user.get("last_msg_id") and user.get("channel_id"):
        try:
            await bot.delete_message(
                chat_id=user["channel_id"],
                message_id=user["last_msg_id"]
            )
        except Exception as e:
            logger.warning(f"Не смог удалить сообщение у {user['user_id']}: {e}")

async def post_to_channel(bot, user, text):
    """Удаляет старое сообщение и постит новое."""
    await delete_last_message(bot, user)
    msg = await bot.send_message(chat_id=user["channel_id"], text=text)
    user_set(user["user_id"], last_msg_id=msg.message_id)
    logger.info(f"[{user['user_id']}] Запостил в канал: {text}")

def is_registered(user):
    """Пользователь зарегистрирован и канал задан?"""
    return user and user.get("channel_id")

def get_text(user, key):
    """Кастомный текст пользователя или дефолт темы "Размотался", если не задан."""
    return user.get(key) or TEXT_DEFAULTS[key]


# - ПЛАНИРОВЩИК ---------------------------------------------------------------

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    """Задание по расписанию для одного пользователя."""
    user_id = context.job.data
    user    = user_get(user_id)
    if not user or not user.get("channel_id"):
        return

    today = datetime.now().strftime("%d.%m.%Y")

    if user["mode"] == 1:
        # Режим опроса - спрашиваем
        keyboard     = [[BTN_NO, BTN_YES]]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Эй! 🏍️ Плановая проверка:\n\n{get_text(user, 'text_question')}",
            reply_markup=reply_markup,
        )
    elif user["mode"] == 2 and user["running"]:
        # Режим автопоста - постим сами
        await post_to_channel(
            context.bot, user,
            f"📅 {today}: {get_text(user, 'text_post_no')}"
        )

def reschedule_user(app, user_id, freq):
    """Перепланирует задание пользователя с новой частотой."""
    # Удаляем старое задание
    for job in app.job_queue.get_jobs_by_name(job_name(user_id)):
        job.schedule_removal()
    # Создаём новое
    app.job_queue.run_repeating(
        scheduled_job,
        interval=freq_to_seconds(freq),
        first=10,
        name=job_name(user_id),
        data=user_id
    )
    logger.info(f"[{user_id}] Задание запланировано: {freq_to_label(freq)}")

def restore_jobs(app):
    """Восстанавливает задания для всех пользователей при старте."""
    for user in all_users():
        reschedule_user(app, user["user_id"], user["freq"])
    logger.info(f"Восстановлено заданий: {len(all_users())}")


# - ОНБОРДИНГ /start ----------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)

    if is_registered(user):
        # Уже зарегистрирован - показываем меню
        freq    = freq_to_label(user["freq"])
        mode    = "опрос" if user["mode"] == 1 else "автопост"
        running = "запущен ✅" if user["running"] else "остановлен ⏸"
        await update.message.reply_text(
            "👋 Трекер разматывания на Мотике!\n\n"
            f"Канал: `{user['channel_id']}`\n"
            f"Режим: *{mode}* | Частота: *{freq}* | Статус: *{running}*\n\n"
            "Команды:\n"
            "/ask - режим опроса\n"
            "/autopost - режим автопоста\n"
            "/setfreq - задать частоту опроса/автопоста\n"
            "/start_autopost - запустить автопост\n"
            "/stop_autopost - остановить автопост\n"
            "/crashed - зафиксировать падение\n"
            "/checkin - ручной чекин\n"
            "/status - текущие настройки\n"
            "/setchannel - сменить канал\n"
            "/customtexts - свои тексты вместо темы «Размотался»\n"
            "/help - справка",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    else:
        # Новый пользователь - запускаем онбординг
        await update.message.reply_text(
            "👋 Привет! Это трекер разматывания на мотике.\n\n"
            "Бот будет спрашивать тебя — размотался или нет — и постить результат в твой Telegram-канал.\n\n"
            "Для начала:\n"
            "1. Создай канал в Telegram\n"
            "2. Добавь этого бота администратором канала\n"
            "3. Пришли мне username канала или его ID\n\n"
            "Пример: `@mycannel` или `-1001234567890`",
            parse_mode="Markdown"
        )
        return WAITING_FOR_CHANNEL

async def handle_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает channel_id от пользователя и проверяет доступ."""
    user_id    = update.effective_user.id
    channel_id = update.message.text.strip()

    # Нормализуем - добавляем @ если нет и не числовой ID
    if not channel_id.startswith("@") and not channel_id.lstrip("-").isdigit():
        channel_id = "@" + channel_id

    # Проверяем что бот может постить в канал
    await update.message.reply_text("Проверяю доступ к каналу...")
    try:
        test_msg = await context.bot.send_message(
            chat_id=channel_id,
            text="🔧 Проверка подключения... сейчас удалю это сообщение."
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не могу написать в канал `{channel_id}`.\n\n"
            "Убедись что:\n"
            "- Канал существует\n"
            "- Бот добавлен администратором с правом постить сообщения\n\n"
            "Попробуй снова:",
            parse_mode="Markdown"
        )
        return WAITING_FOR_CHANNEL

    # Удаление тестового сообщения не критично - канал уже подтверждён отправкой выше
    try:
        await context.bot.delete_message(chat_id=channel_id, message_id=test_msg.message_id)
    except Exception as e:
        logger.warning(f"Не смог удалить тестовое сообщение в канале {channel_id}: {e}")

    # Сохраняем и запускаем задание
    user_set(user_id, channel_id=channel_id)
    log_action(user_id, f"подключил канал {channel_id}")
    reschedule_user(context.application, user_id, "7d")

    await update.message.reply_text(
        f"✅ Канал `{channel_id}` подключён!\n\n"
        "По умолчанию стоит режим опроса раз в 7 дней.\n"
        "Используй /setfreq чтобы изменить частоту.\n\n"
        "/help - список всех команд",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


# - КОМАНДА /setchannel -------------------------------------------------------

async def setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Пришли новый username или ID канала.\n"
        "Пример: `@mycannel` или `-1001234567890`\n\n"
        "Не забудь что бот должен быть администратором нового канала.",
        parse_mode="Markdown"
    )
    return WAITING_FOR_CHANNEL


# - КОМАНДА /status -----------------------------------------------------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)
    if not is_registered(user):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return

    mode    = "опрос" if user["mode"] == 1 else "автопост"
    freq    = freq_to_label(user["freq"])
    running = "запущен ✅" if user["running"] else "остановлен ⏸"
    last_id = user["last_msg_id"] or "нет"

    await update.message.reply_text(
        f"📊 Статус:\n\n"
        f"Канал: `{user['channel_id']}`\n"
        f"Режим: *{mode}*\n"
        f"Частота: *{freq}*\n"
        f"Автопост: *{running}*\n"
        f"Последнее сообщение в канале: `{last_id}`",
        parse_mode="Markdown"
    )


# - КОМАНДА /ask --------------------------------------------------------------

async def set_mode_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)
    if not is_registered(user):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    user_set(user_id, mode=1, running=0)
    log_action(user_id, "включил режим опроса (/ask)")
    reschedule_user(context.application, user_id, user["freq"])
    await update.message.reply_text(
        "✅ Режим опроса включён.\n"
        f"Буду спрашивать тебя {freq_to_label(user['freq'])}."
    )


# - КОМАНДА /autopost ---------------------------------------------------------

async def set_mode_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)
    if not is_registered(user):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    user_set(user_id, mode=2)
    log_action(user_id, "включил режим автопоста (/autopost)")
    await update.message.reply_text(
        "✅ Режим автопоста включён.\n"
        "Буду постить в канал сам без вопросов.\n"
        "Используй /start_autopost чтобы запустить, /stop_autopost чтобы остановить."
    )


# - КОМАНДА /start_autopost ---------------------------------------------------

async def start_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)
    if not is_registered(user):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    if user["mode"] != 2:
        await update.message.reply_text("Сначала переключись в режим автопоста командой /autopost")
        return
    user_set(user_id, running=1)
    log_action(user_id, "запустил автопост (/start_autopost)")
    reschedule_user(context.application, user_id, user["freq"])
    await update.message.reply_text(
        f"▶️ Автопост запущен! Постю {freq_to_label(user['freq'])}.\n"
        "Останови через /stop_autopost или зафиксируй падение через /crashed."
    )


# - КОМАНДА /stop_autopost ----------------------------------------------------

async def stop_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_registered(user_get(user_id)):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    user_set(user_id, running=0)
    log_action(user_id, "остановил автопост (/stop_autopost)")
    await update.message.reply_text("⏸ Автопост остановлен.")


# - КОМАНДА /setfreq ----------------------------------------------------------

async def setfreq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_registered(user_get(user_id)):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    keyboard     = [["1d", "3d", "7d"], ["14d", "1w", "2w"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Выбери частоту или напиши своё значение:\n"
        "Форматы: `1h` (час), `3d` (дня), `1w` (неделя)",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return WAITING_FOR_FREQ

async def handle_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip().lower()
    if len(text) < 2 or text[-1] not in ("h", "d", "w") or not text[:-1].isdigit():
        await update.message.reply_text(
            "Неверный формат. Примеры: `1h`, `3d`, `7d`, `2w`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_FOR_FREQ
    user_set(user_id, freq=text)
    log_action(user_id, f"установил частоту {text} (/setfreq)")
    reschedule_user(context.application, user_id, text)
    await update.message.reply_text(
        f"✅ Частота установлена: {freq_to_label(text)}",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# - КОМАНДА /checkin ----------------------------------------------------------

async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)
    if not is_registered(user):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    keyboard     = [[BTN_NO, BTN_YES]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"Ручной чекин 🏍️\n\n{get_text(user, 'text_checkin')}",
        reply_markup=reply_markup,
    )
    return WAITING_FOR_CHECK

async def handle_checkin_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)
    text    = update.message.text
    today   = datetime.now().strftime("%d.%m.%Y")

    if "Неа" in text:
        await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_no')}")
        log_action(user_id, "ручной чекин (/checkin): не размотался")
        await update.message.reply_text(
            get_text(user, "text_reply_no"),
            reply_markup=ReplyKeyboardRemove()
        )
    elif "Ага" in text:
        await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_yes')}")
        user_set(user_id, running=0)
        log_action(user_id, "ручной чекин (/checkin): размотался")
        await update.message.reply_text(
            get_text(user, "text_reply_yes"),
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            get_text(user, "text_reply_invalid"),
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_FOR_CHECK

    return ConversationHandler.END


# - КОМАНДА /crashed ----------------------------------------------------------

async def crashed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = user_get(user_id)
    if not is_registered(user):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    today = datetime.now().strftime("%d.%m.%Y")
    await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_yes')}")
    user_set(user_id, running=0)
    log_action(user_id, "зафиксировал падение (/crashed)")
    await update.message.reply_text(get_text(user, "text_crashed_reply"))


# - КОМАНДА /help -------------------------------------------------------------

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Справка по боту\n\n"
        "РЕЖИМЫ:\n"
        "/ask - режим опроса. Бот спрашивает тебя по расписанию,\n"
        "  ты отвечаешь кнопками, он постит в канал.\n"
        "/autopost - режим автопоста. Бот сам постит в канал по\n"
        "  расписанию без вопросов. Останавливается по /stop_autopost\n"
        "  или /crashed.\n\n"
        "УПРАВЛЕНИЕ:\n"
        "/start_autopost - запустить автопост\n"
        "/stop_autopost - остановить автопост\n"
        "/crashed - зафиксировать падение и остановить автопост\n"
        "/checkin - ручной чекин с кнопками\n\n"
        "НАСТРОЙКИ:\n"
        "/setfreq - задать частоту. Форматы:\n"
        "  1h = каждый час\n"
        "  3d = каждые 3 дня\n"
        "  1w = каждую неделю\n"
        "/setchannel - сменить канал\n"
        "/customtexts - настроить свои тексты вопросов/постов/ответов\n"
        "/resettexts - вернуть стандартные тексты\n"
        "/status - показать текущие настройки\n\n"
        "ПРОЧЕЕ:\n"
        "/start - главное меню\n"
        "/help - эта справка"
    )


# - КОМАНДА /customtexts (пошаговый wizard) -----------------------------------

WIZARD_FIELDS = [
    ("text_question",     "Вопрос при плановой проверке по расписанию"),
    ("text_checkin",       "Вопрос при ручном чекине (/checkin)"),
    ("text_post_no",       f"Текст поста в канал на «{BTN_NO}» (что не случилось)"),
    ("text_post_yes",      f"Текст поста в канал на «{BTN_YES}» (что случилось)"),
    ("text_reply_no",      f"Личный ответ тебе после «{BTN_NO}»"),
    ("text_reply_yes",     f"Личный ответ тебе после «{BTN_YES}»"),
    ("text_reply_invalid", "Ответ, если вместо кнопки пришло что-то другое"),
    ("text_crashed_reply", "Ответ на команду /crashed"),
]

async def customtexts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_registered(user_get(user_id)):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return ConversationHandler.END
    context.user_data["wizard_idx"] = 0
    await update.message.reply_text(
        "✏️ Настройка своих текстов вместо темы «Размотался».\n\n"
        f"Кнопки «{BTN_NO}» / «{BTN_YES}» не меняются - настраиваются только "
        "тексты вопросов, постов в канал и ответов.\n\n"
        "На каждом шаге пришли свой текст или /skip чтобы оставить текущий.\n"
        "/cancel - прервать в любой момент."
    )
    return await ask_next_text(update, context)

async def ask_next_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    idx = context.user_data.get("wizard_idx", 0)
    if idx >= len(WIZARD_FIELDS):
        await update.message.reply_text(
            "✅ Готово! Все тексты настроены.\nЧтобы вернуть стандартные - /resettexts."
        )
        return ConversationHandler.END

    field, label = WIZARD_FIELDS[idx]
    user    = user_get(user_id)
    current = get_text(user, field)
    await update.message.reply_text(
        f"({idx + 1}/{len(WIZARD_FIELDS)}) {label}\n\n"
        f"Текущий текст:\n«{current}»\n\n"
        "Пришли новый текст или /skip, чтобы оставить этот."
    )
    return WAITING_FOR_CUSTOM_TEXT

async def handle_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    idx      = context.user_data.get("wizard_idx", 0)
    field, _ = WIZARD_FIELDS[idx]
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("Текст не может быть пустым. Пришли текст или /skip.")
        return WAITING_FOR_CUSTOM_TEXT
    if len(text) > MAX_CUSTOM_TEXT_LEN:
        await update.message.reply_text(f"Слишком длинно (максимум {MAX_CUSTOM_TEXT_LEN} символов), напиши короче.")
        return WAITING_FOR_CUSTOM_TEXT

    user_set(user_id, **{field: text})
    log_action(user_id, f"настроил текст «{field}» (/customtexts)")
    context.user_data["wizard_idx"] = idx + 1
    return await ask_next_text(update, context)

async def skip_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get("wizard_idx", 0)
    context.user_data["wizard_idx"] = idx + 1
    return await ask_next_text(update, context)


# - КОМАНДА /resettexts --------------------------------------------------------

async def resettexts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_registered(user_get(user_id)):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    user_set(user_id, **TEXT_DEFAULTS)
    log_action(user_id, "сбросил тексты на стандартные (/resettexts)")
    await update.message.reply_text("🔄 Тексты сброшены на стандартные (тема «Размотался»).")


# - ОТМЕНА --------------------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# - ЛОВИМ КНОПКИ ИЗ ПЛАНИРОВЩИКА (режим 1) -----------------------------------

async def catch_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит нажатия кнопок из сообщений планировщика вне диалога."""
    user_id = update.effective_user.id
    user    = user_get(user_id)
    if not is_registered(user):
        return

    text  = update.message.text or ""
    today = datetime.now().strftime("%d.%m.%Y")

    if "Неа" in text:
        await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_no')}")
        log_action(user_id, "ответил на плановую проверку: не размотался")
        await update.message.reply_text(
            get_text(user, "text_reply_no"),
            reply_markup=ReplyKeyboardRemove()
        )
    elif "Ага" in text:
        await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_yes')}")
        user_set(user_id, running=0)
        log_action(user_id, "ответил на плановую проверку: размотался")
        await update.message.reply_text(
            get_text(user, "text_reply_yes"),
            reply_markup=ReplyKeyboardRemove()
        )


# - ОБРАБОТКА ОШИБОК -----------------------------------------------------------

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик необработанных исключений в хендлерах и заданиях."""
    logger.error(f"Необработанная ошибка при обновлении {update}: {context.error}", exc_info=context.error)


# - СИНХРОНИЗАЦИЯ МЕНЮ КОМАНД --------------------------------------------------

async def sync_bot_commands(app: Application):
    """Перезаписывает список команд в Telegram, чтобы меню бота не расходилось с кодом
    (иначе старые команды из прошлых версий остаются висеть в меню как нерабочие кнопки)."""
    await app.bot.set_my_commands([
        BotCommand("start", "Регистрация / главное меню"),
        BotCommand("ask", "Режим опроса"),
        BotCommand("autopost", "Режим автопоста"),
        BotCommand("start_autopost", "Запустить автопост"),
        BotCommand("stop_autopost", "Остановить автопост"),
        BotCommand("setfreq", "Задать частоту проверок"),
        BotCommand("checkin", "Ручной чекин"),
        BotCommand("crashed", "Зафиксировать падение"),
        BotCommand("setchannel", "Сменить канал"),
        BotCommand("customtexts", "Настроить свои тексты"),
        BotCommand("resettexts", "Вернуть стандартные тексты"),
        BotCommand("status", "Текущие настройки"),
        BotCommand("help", "Справка"),
    ])


# - ЗАПУСК --------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Установи переменную окружения BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).post_init(sync_bot_commands).build()

    # Онбординг и смена канала
    channel_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start",      start),
            CommandHandler("setchannel", setchannel),
        ],
        states={
            WAITING_FOR_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_channel_input)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Диалог для /setfreq
    freq_handler = ConversationHandler(
        entry_points=[CommandHandler("setfreq", setfreq)],
        states={
            WAITING_FOR_FREQ: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_freq)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Диалог для /checkin
    checkin_handler = ConversationHandler(
        entry_points=[CommandHandler("checkin", checkin)],
        states={
            WAITING_FOR_CHECK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_checkin_answer)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Диалог для /customtexts
    customtexts_handler = ConversationHandler(
        entry_points=[CommandHandler("customtexts", customtexts)],
        states={
            WAITING_FOR_CUSTOM_TEXT: [
                CommandHandler("skip", skip_custom_text),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(channel_handler)
    app.add_handler(freq_handler)
    app.add_handler(checkin_handler)
    app.add_handler(customtexts_handler)
    app.add_handler(CommandHandler("status",         status))
    app.add_handler(CommandHandler("ask",            set_mode_ask))
    app.add_handler(CommandHandler("autopost",       set_mode_autopost))
    app.add_handler(CommandHandler("start_autopost", start_autopost))
    app.add_handler(CommandHandler("stop_autopost",  stop_autopost))
    app.add_handler(CommandHandler("crashed",        crashed))
    app.add_handler(CommandHandler("resettexts",     resettexts))
    app.add_handler(CommandHandler("help",           help_cmd))
    # Кнопки из планировщика
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(Неа|Ага)"),
        catch_buttons
    ))

    app.add_error_handler(error_handler)

    # Восстанавливаем задания для всех пользователей
    restore_jobs(app)

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    import asyncio
    import sys

    # Фикс для Python 3.14 на Windows
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()
