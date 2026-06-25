"""
Трекер разматывания на мотике — версия с inline-кнопками.

Что изменилось по сравнению со старой версией:
- Главное меню теперь не стена команд, а карточка статуса + сетка inline-кнопок.
- Кнопки опроса «Неа 🚴 / Ага 💥», выбор частоты, подтверждение сброса и
  шаги мастера «свои тексты» — всё на inline-кнопках (callback_data).
- Все нажатия ловит ОДИН диспетчер on_menu + узкие обработчики в диалогах.
- Slash-команды (/ask, /setfreq, /checkin …) продолжают работать — удобно
  для тех, кто пользуется системным меню команд бота.
"""

import os
import logging
import sqlite3
from datetime import datetime
from html import escape as h

from telegram import (
    Update, BotCommand,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler,
)

# - НАСТРОЙКИ -----------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH   = os.environ.get("DB_PATH", "/var/lib/bike_crash_bot/state.db")
ACTIONS_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_actions.log")

# Подписи кнопок опроса (внутри callback_data — короткие коды a:no / a:yes)
BTN_NO  = "Неа 🚴"
BTN_YES = "Ага 💥"

# Тексты по умолчанию (тема "Размотался"). Переопределяются через /customtexts.
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
WAITING_FOR_CHANNEL       = 10
WAITING_FOR_FREQ          = 20
WAITING_FOR_FREQ_TEXT     = 21
WAITING_FOR_CUSTOM_TEXT   = 40
WAITING_FOR_RESET_CONFIRM = 50

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# - БАЗА ДАННЫХ ---------------------------------------------------------------

def _sql_escape(value):
    return value.replace("'", "''")

def _migrate_text_columns(conn):
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col, default in TEXT_DEFAULTS.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT '{_sql_escape(default)}'")

def db_connect():
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
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

def user_set(user_id, **kwargs):
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
    with db_connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


# - ВСПОМОГАТЕЛЬНЫЕ ------------------------------------------------------------

def freq_to_seconds(freq):
    unit = freq[-1]
    val  = int(freq[:-1])
    if unit == "h": return val * 3600
    if unit == "d": return val * 86400
    if unit == "w": return val * 604800
    return 604800

def freq_to_label(freq):
    unit   = freq[-1]
    val    = int(freq[:-1])
    labels = {"h": "ч", "d": "д", "w": "нед"}
    return f"каждые {val} {labels.get(unit, '?')}"

def job_name(user_id):
    return f"job_{user_id}"

def log_action(user_id, action):
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    with open(ACTIONS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"Пользователь {user_id} {now} сделал: {action}\n")

async def delete_last_message(bot, user):
    if user.get("last_msg_id") and user.get("channel_id"):
        try:
            await bot.delete_message(chat_id=user["channel_id"], message_id=user["last_msg_id"])
        except Exception as e:
            logger.warning(f"Не смог удалить сообщение у {user['user_id']}: {e}")

async def post_to_channel(bot, user, text):
    await delete_last_message(bot, user)
    msg = await bot.send_message(chat_id=user["channel_id"], text=text)
    user_set(user["user_id"], last_msg_id=msg.message_id)
    logger.info(f"[{user['user_id']}] Запостил в канал: {text}")

def is_registered(user):
    return user and user.get("channel_id")

def get_text(user, key):
    return user.get(key) or TEXT_DEFAULTS[key]


# - КЛАВИАТУРЫ (inline) -------------------------------------------------------

def main_menu_kb(user):
    """Сетка кнопок главного меню. Галочка на активном режиме."""
    ask  = "🔔 Опрос"   + (" ✓" if user["mode"] == 1 else "")
    auto = "📢 Автопост" + (" ✓" if user["mode"] == 2 else "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ask,  callback_data="m:ask"),
         InlineKeyboardButton(auto, callback_data="m:auto")],
        [InlineKeyboardButton("▶️ Запустить",  callback_data="m:run"),
         InlineKeyboardButton("⏸ Остановить", callback_data="m:stop")],
        [InlineKeyboardButton("⏱ Частота", callback_data="m:freq"),
         InlineKeyboardButton("✅ Чекин",  callback_data="m:checkin")],
        [InlineKeyboardButton("💥 Падение",     callback_data="m:crash"),
         InlineKeyboardButton("✏️ Свои тексты", callback_data="m:texts")],
        [InlineKeyboardButton("📊 Статус",  callback_data="m:status"),
         InlineKeyboardButton("❓ Справка", callback_data="m:help")],
        [InlineKeyboardButton("🔄 Сбросить всё", callback_data="m:reset")],
    ])

BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("‹ Меню", callback_data="m:home")]])

def freq_kb(user):
    presets = [("1 час", "1h"), ("6 часов", "6h"), ("1 день", "1d"),
               ("3 дня", "3d"), ("7 дней", "7d"), ("2 недели", "2w")]
    rows, row = [], []
    for label, code in presets:
        mark = " ✓" if user and user.get("freq") == code else ""
        row.append(InlineKeyboardButton(label + mark, callback_data="f:" + code))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Своё значение", callback_data="f:custom")])
    rows.append([InlineKeyboardButton("‹ Меню", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)

def answer_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(BTN_NO,  callback_data="a:no"),
        InlineKeyboardButton(BTN_YES, callback_data="a:yes"),
    ]])

def wizard_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Пропустить", callback_data="w:skip"),
        InlineKeyboardButton("✖️ Отмена",    callback_data="w:cancel"),
    ]])

def reset_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Да, сбросить всё", callback_data="r:yes")],
        [InlineKeyboardButton("Отмена",             callback_data="r:no")],
    ])


# - РЕНДЕР ТЕКСТОВ -------------------------------------------------------------

def render_menu_text(user):
    mode    = "🔔 Опрос" if user["mode"] == 1 else "📢 Автопост"
    running = "запущен ✅" if user["running"] else "остановлен ⏸"
    return (
        "🏍️ <b>Трекер разматывания на Мотике</b> — меню\n\n"
        f"Канал: <code>{h(str(user['channel_id']))}</code>\n"
        f"Режим: <b>{mode}</b>\n"
        f"Частота: <b>{h(freq_to_label(user['freq']))}</b>\n"
        f"Автопост: <b>{running}</b>"
    )

def render_status_text(user):
    mode    = "опрос" if user["mode"] == 1 else "автопост"
    running = "запущен ✅" if user["running"] else "остановлен ⏸"
    last_id = user["last_msg_id"] or "нет"
    return (
        "📊 <b>Статус</b>\n\n"
        f"Канал: <code>{h(str(user['channel_id']))}</code>\n"
        f"Режим: <b>{mode}</b>\n"
        f"Частота: <b>{h(freq_to_label(user['freq']))}</b>\n"
        f"Автопост: <b>{running}</b>\n"
        f"Последнее сообщение в канале: <code>{h(str(last_id))}</code>"
    )

HELP_TEXT = (
    "📖 Справка\n\n"
    "РЕЖИМЫ\n"
    "🔔 Опрос — бот спрашивает по расписанию, ты жмёшь кнопку, он постит в канал.\n"
    "📢 Автопост — бот сам постит по расписанию, пока не остановишь.\n\n"
    "УПРАВЛЕНИЕ\n"
    "▶️ Запустить / ⏸ Остановить — автопост.\n"
    "💥 Падение — зафиксировать падение и остановить автопост.\n"
    "✅ Чекин — ручная проверка с кнопками.\n\n"
    "НАСТРОЙКИ\n"
    "⏱ Частота — как часто проверять (1h / 3d / 1w …).\n"
    "✏️ Свои тексты — заменить тексты вопросов/постов/ответов.\n"
    "🔄 Сбросить всё — забыть канал, режим, частоту и тексты.\n\n"
    "Команды также доступны через меню «☰» рядом с полем ввода."
)


# - УНИВЕРСАЛЬНЫЕ ОТВЕТЫ (работают и для кнопки, и для команды) ----------------

async def ui_respond(update, context, text, reply_markup=None, parse_mode=None):
    """Если пришло нажатие кнопки — редактируем сообщение; если команда — отвечаем новым."""
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except BadRequest:
            pass
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

async def notify(update, context, text, alert=False):
    """Короткое уведомление: всплывашка для кнопки, обычное сообщение для команды."""
    q = update.callback_query
    if q:
        await q.answer(text, show_alert=alert)
    else:
        await update.effective_message.reply_text(text)

async def ui_guard(update, context):
    await notify(update, context, "Сначала зарегистрируйся командой /start", alert=True)

async def show_menu(update, context, toast=None):
    """Показывает/обновляет карточку главного меню."""
    uid  = update.effective_user.id
    user = user_get(uid)
    text = render_menu_text(user)
    kb   = main_menu_kb(user)
    q = update.callback_query
    if q:
        await q.answer(toast or "")
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except BadRequest:
            pass
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# - ОНБОРДИНГ /start ----------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = user_get(update.effective_user.id)
    if is_registered(user):
        await show_menu(update, context)
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📡 Подключить канал", callback_data="onb:connect")]])
    await update.message.reply_text(
        "👋 Привет! Это трекер разматывания на мотике.\n\n"
        "Я буду спрашивать — размотался ты или нет — и постить результат в твой канал.\n\n"
        "Чтобы начать:\n"
        "1. Создай канал в Telegram\n"
        "2. Добавь меня администратором\n"
        "3. Нажми кнопку ниже и пришли @username или ID канала",
        reply_markup=kb,
    )
    return WAITING_FOR_CHANNEL

async def onb_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_text(
            "Пришли username канала (@mychannel) или его ID (-1001234567890).\n"
            "Я должен быть админом этого канала."
        )
    except BadRequest:
        pass
    return WAITING_FOR_CHANNEL

async def handle_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid        = update.effective_user.id
    channel_id = update.message.text.strip()
    if not channel_id.startswith("@") and not channel_id.lstrip("-").isdigit():
        channel_id = "@" + channel_id

    await update.message.reply_text("Проверяю доступ к каналу…")
    try:
        test_msg = await context.bot.send_message(
            chat_id=channel_id, text="🔧 Проверка подключения… сейчас удалю это сообщение."
        )
    except Exception:
        await update.message.reply_text(
            f"❌ Не могу написать в канал {channel_id}.\n\n"
            "Проверь, что канал существует и бот добавлен администратором с правом постить.\n"
            "Пришли username или ID ещё раз:"
        )
        return WAITING_FOR_CHANNEL

    try:
        await context.bot.delete_message(chat_id=channel_id, message_id=test_msg.message_id)
    except Exception as e:
        logger.warning(f"Не смог удалить тестовое сообщение в канале {channel_id}: {e}")

    user_set(uid, channel_id=channel_id)
    log_action(uid, f"подключил канал {channel_id}")
    reschedule_user(context.application, uid, user_get(uid)["freq"])

    await update.message.reply_text(f"✅ Канал {channel_id} подключён!")
    await show_menu(update, context)
    return ConversationHandler.END


# - СМЕНА КАНАЛА /setchannel ---------------------------------------------------

async def setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Пришли новый username или ID канала (@mychannel или -1001234567890).\n"
        "Бот должен быть админом нового канала."
    )
    return WAITING_FOR_CHANNEL


# - ДЕЙСТВИЯ МЕНЮ (работают из кнопки и из команды) ---------------------------

async def act_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = user_get(uid)
    if not is_registered(user):
        return await ui_guard(update, context)
    user_set(uid, mode=1, running=0)
    log_action(uid, "включил режим опроса")
    reschedule_user(context.application, uid, user["freq"])
    await show_menu(update, context, toast="✅ Режим опроса включён")

async def act_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_registered(user_get(uid)):
        return await ui_guard(update, context)
    user_set(uid, mode=2)
    log_action(uid, "включил режим автопоста")
    await show_menu(update, context, toast="📢 Режим автопоста. Запусти ▶️")

async def act_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = user_get(uid)
    if not is_registered(user):
        return await ui_guard(update, context)
    if user["mode"] != 2:
        return await notify(update, context, "Сначала включи режим автопоста 📢", alert=True)
    user_set(uid, running=1)
    log_action(uid, "запустил автопост")
    reschedule_user(context.application, uid, user["freq"])
    await show_menu(update, context, toast="▶️ Автопост запущен")

async def act_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_registered(user_get(uid)):
        return await ui_guard(update, context)
    user_set(uid, running=0)
    log_action(uid, "остановил автопост")
    await show_menu(update, context, toast="⏸ Автопост остановлен")

async def act_crashed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = user_get(uid)
    if not is_registered(user):
        return await ui_guard(update, context)
    today = datetime.now().strftime("%d.%m.%Y")
    await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_yes')}")
    user_set(uid, running=0)
    log_action(uid, "зафиксировал падение")
    await show_menu(update, context, toast="💥 Зафиксировал. Автопост остановлен")

async def act_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = user_get(update.effective_user.id)
    if not is_registered(user):
        return await ui_guard(update, context)
    await ui_respond(update, context, render_status_text(user), reply_markup=BACK_KB, parse_mode="HTML")

async def act_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ui_respond(update, context, HELP_TEXT, reply_markup=BACK_KB)


# - ЧЕКИН И ОТВЕТЫ НА ОПРОС ----------------------------------------------------

async def do_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = user_get(update.effective_user.id)
    if not is_registered(user):
        return await ui_guard(update, context)
    text = f"🏍️ Ручной чекин\n\n{get_text(user, 'text_checkin')}"
    q = update.callback_query
    if q:
        await q.answer()
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=answer_kb())

async def do_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит a:no / a:yes — и от планового опроса, и от ручного чекина."""
    q   = update.callback_query
    uid = update.effective_user.id
    user = user_get(uid)
    if not is_registered(user):
        return await q.answer()
    today = datetime.now().strftime("%d.%m.%Y")
    if q.data == "a:yes":
        await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_yes')}")
        user_set(uid, running=0)
        log_action(uid, "ответ на проверку: размотался")
        reply = get_text(user, "text_reply_yes")
    else:
        await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_no')}")
        log_action(uid, "ответ на проверку: не размотался")
        reply = get_text(user, "text_reply_no")
    await q.answer("Отправлено в канал ✅")
    try:
        await q.edit_message_text(f"✅ {reply}")   # убирает кнопки, показывает ответ
    except BadRequest:
        pass


# - ЧАСТОТА /setfreq (диалог) --------------------------------------------------

async def setfreq_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = user_get(update.effective_user.id)
    if not is_registered(user):
        await ui_guard(update, context)
        return ConversationHandler.END
    text = "⏱ Как часто проверять?\nВыбери период или пришли своё (например 12h)."
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text(text, reply_markup=freq_kb(user))
        except BadRequest:
            pass
    else:
        await update.effective_message.reply_text(text, reply_markup=freq_kb(user))
    return WAITING_FOR_FREQ

async def freq_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    code = q.data.split(":")[1]
    user_set(uid, freq=code)
    log_action(uid, f"установил частоту {code}")
    reschedule_user(context.application, uid, code)
    await q.answer(f"✅ {freq_to_label(code)}")
    user = user_get(uid)
    try:
        await q.edit_message_text(render_menu_text(user), reply_markup=main_menu_kb(user), parse_mode="HTML")
    except BadRequest:
        pass
    return ConversationHandler.END

async def freq_custom_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_text("Пришли своё значение.\nФорматы: 1h (час), 3d (дня), 1w (неделя).")
    except BadRequest:
        pass
    return WAITING_FOR_FREQ_TEXT

async def handle_freq_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip().lower()
    if len(text) < 2 or text[-1] not in ("h", "d", "w") or not text[:-1].isdigit():
        await update.message.reply_text("Неверный формат. Примеры: 1h, 3d, 7d, 2w")
        return WAITING_FOR_FREQ_TEXT
    user_set(uid, freq=text)
    log_action(uid, f"установил частоту {text}")
    reschedule_user(context.application, uid, text)
    user = user_get(uid)
    await update.message.reply_text(
        render_menu_text(user), reply_markup=main_menu_kb(user), parse_mode="HTML"
    )
    return ConversationHandler.END


# - СВОИ ТЕКСТЫ /customtexts (мастер) ------------------------------------------

WIZARD_FIELDS = [
    ("text_question",     "Вопрос при плановой проверке по расписанию"),
    ("text_checkin",      "Вопрос при ручном чекине (✅ Чекин)"),
    ("text_post_no",      f"Текст поста в канал на «{BTN_NO}» (что не случилось)"),
    ("text_post_yes",     f"Текст поста в канал на «{BTN_YES}» (что случилось)"),
    ("text_reply_no",     f"Личный ответ тебе после «{BTN_NO}»"),
    ("text_reply_yes",    f"Личный ответ тебе после «{BTN_YES}»"),
    ("text_reply_invalid","Ответ, если вместо кнопки пришло что-то другое"),
    ("text_crashed_reply","Ответ на 💥 Падение"),
]

async def customtexts_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_registered(user_get(uid)):
        await ui_guard(update, context)
        return ConversationHandler.END
    context.user_data["wizard_idx"] = 0
    intro = (
        "✏️ Свои тексты вместо темы «Размотался».\n"
        f"Кнопки «{BTN_NO}» / «{BTN_YES}» не меняются — только тексты вопросов, постов и ответов.\n"
        "На каждом шаге пришли свой текст или нажми «Пропустить»."
    )
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text(intro)
        except BadRequest:
            pass
    else:
        await update.effective_message.reply_text(intro)
    return await ask_next_text(update, context)

async def ask_next_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    idx = context.user_data.get("wizard_idx", 0)

    if idx >= len(WIZARD_FIELDS):
        await context.bot.send_message(chat_id, "✅ Готово! Все тексты настроены.\nВернуть стандартные — /resettexts.")
        user = user_get(uid)
        await context.bot.send_message(
            chat_id, render_menu_text(user), reply_markup=main_menu_kb(user), parse_mode="HTML"
        )
        return ConversationHandler.END

    field, label = WIZARD_FIELDS[idx]
    current = get_text(user_get(uid), field)
    await context.bot.send_message(
        chat_id,
        f"Шаг {idx + 1} из {len(WIZARD_FIELDS)}\n\n{label}\n\n"
        f"Текущий текст:\n«{current}»\n\nПришли новый текст или нажми «Пропустить».",
        reply_markup=wizard_kb(),
    )
    return WAITING_FOR_CUSTOM_TEXT

async def handle_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    idx = context.user_data.get("wizard_idx", 0)
    field, _ = WIZARD_FIELDS[idx]
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст не может быть пустым. Пришли текст или нажми «Пропустить».")
        return WAITING_FOR_CUSTOM_TEXT
    if len(text) > MAX_CUSTOM_TEXT_LEN:
        await update.message.reply_text(f"Слишком длинно (максимум {MAX_CUSTOM_TEXT_LEN} символов), напиши короче.")
        return WAITING_FOR_CUSTOM_TEXT
    user_set(uid, **{field: text})
    log_action(uid, f"настроил текст «{field}»")
    context.user_data["wizard_idx"] = idx + 1
    return await ask_next_text(update, context)

async def skip_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer("Пропущено")
        try:
            await q.edit_message_reply_markup(None)
        except BadRequest:
            pass
    context.user_data["wizard_idx"] = context.user_data.get("wizard_idx", 0) + 1
    return await ask_next_text(update, context)

async def cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_text("Настройка прервана.")
    except BadRequest:
        pass
    user = user_get(update.effective_user.id)
    await context.bot.send_message(
        update.effective_chat.id, render_menu_text(user),
        reply_markup=main_menu_kb(user), parse_mode="HTML",
    )
    return ConversationHandler.END


# - СБРОС ТЕКСТОВ /resettexts --------------------------------------------------

async def resettexts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_registered(user_get(uid)):
        await update.message.reply_text("Сначала зарегистрируйся командой /start")
        return
    user_set(uid, **TEXT_DEFAULTS)
    log_action(uid, "сбросил тексты на стандартные")
    await update.message.reply_text("🔄 Тексты сброшены на стандартные (тема «Размотался»).")


# - ПОЛНЫЙ СБРОС /reset (диалог с подтверждением) ------------------------------

async def reset_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_get(uid):
        await notify(update, context, "Ты ещё не зарегистрирован. Начни с /start")
        return ConversationHandler.END
    text = (
        "⚠️ Это отключит канал, остановит опрос/автопост и вернёт все тексты к "
        "стандартным — всё будет как при первом запуске. Отменить нельзя.\n\n"
        "Точно сбросить всё?"
    )
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text(text, reply_markup=reset_kb())
        except BadRequest:
            pass
    else:
        await update.effective_message.reply_text(text, reply_markup=reset_kb())
    return WAITING_FOR_RESET_CONFIRM

async def reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    if q.data == "r:no":
        await q.answer("Отменено")
        user = user_get(uid)
        try:
            await q.edit_message_text(render_menu_text(user), reply_markup=main_menu_kb(user), parse_mode="HTML")
        except BadRequest:
            pass
        return ConversationHandler.END

    for job in context.application.job_queue.get_jobs_by_name(job_name(uid)):
        job.schedule_removal()
    user_set(uid, channel_id=None, mode=1, freq="7d", running=0, last_msg_id=None, **TEXT_DEFAULTS)
    log_action(uid, "сбросил все настройки")
    await q.answer("Сброшено")
    try:
        await q.edit_message_text("🔄 Все настройки сброшены.\nНачни заново с /start")
    except BadRequest:
        pass
    return ConversationHandler.END


# - ОБЩИЕ: отмена и выход в меню -----------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

async def end_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)
    return ConversationHandler.END


# - ГЛАВНЫЙ ДИСПЕТЧЕР КНОПОК ---------------------------------------------------

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит простые кнопки меню и ответы опроса (узкие диалоги ловят своё сами)."""
    data = update.callback_query.data
    if data == "m:home":       await show_menu(update, context)
    elif data == "m:ask":      await act_ask(update, context)
    elif data == "m:auto":     await act_autopost(update, context)
    elif data == "m:run":      await act_run(update, context)
    elif data == "m:stop":     await act_stop(update, context)
    elif data == "m:crash":    await act_crashed(update, context)
    elif data == "m:status":   await act_status(update, context)
    elif data == "m:help":     await act_help(update, context)
    elif data == "m:checkin":  await do_checkin(update, context)
    elif data in ("a:no", "a:yes"): await do_answer(update, context)


# - ПЛАНИРОВЩИК ---------------------------------------------------------------

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    uid  = context.job.data
    user = user_get(uid)
    if not user or not user.get("channel_id"):
        return
    today = datetime.now().strftime("%d.%m.%Y")
    if user["mode"] == 1:
        await context.bot.send_message(
            chat_id=uid,
            text=f"🏍️ Плановая проверка\n\n{get_text(user, 'text_question')}",
            reply_markup=answer_kb(),
        )
    elif user["mode"] == 2 and user["running"]:
        await post_to_channel(context.bot, user, f"📅 {today}: {get_text(user, 'text_post_no')}")

def reschedule_user(app, user_id, freq):
    for job in app.job_queue.get_jobs_by_name(job_name(user_id)):
        job.schedule_removal()
    app.job_queue.run_repeating(
        scheduled_job, interval=freq_to_seconds(freq), first=10,
        name=job_name(user_id), data=user_id,
    )
    logger.info(f"[{user_id}] Задание запланировано: {freq_to_label(freq)}")

def restore_jobs(app):
    users = all_users()
    for user in users:
        reschedule_user(app, user["user_id"], user["freq"])
    logger.info(f"Восстановлено заданий: {len(users)}")


# - ОБРАБОТКА ОШИБОК -----------------------------------------------------------

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Необработанная ошибка при обновлении {update}: {context.error}", exc_info=context.error)


# - МЕНЮ КОМАНД В TELEGRAM ------------------------------------------------------

async def sync_bot_commands(app: Application):
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
        BotCommand("reset", "Сбросить всё и начать заново"),
        BotCommand("status", "Текущие настройки"),
        BotCommand("help", "Справка"),
    ])


# - ЗАПУСК --------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Установи переменную окружения BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).post_init(sync_bot_commands).build()

    # Онбординг и смена канала (ждём текст с каналом)
    channel_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("setchannel", setchannel),
        ],
        states={
            WAITING_FOR_CHANNEL: [
                CallbackQueryHandler(onb_prompt, pattern="^onb:connect$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_channel_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Частота: вход с команды /setfreq или с кнопки «⏱ Частота» (m:freq)
    freq_handler = ConversationHandler(
        entry_points=[
            CommandHandler("setfreq", setfreq_entry),
            CallbackQueryHandler(setfreq_entry, pattern="^m:freq$"),
        ],
        states={
            WAITING_FOR_FREQ: [
                CallbackQueryHandler(freq_pick, pattern="^f:(1h|6h|1d|3d|7d|2w)$"),
                CallbackQueryHandler(freq_custom_ask, pattern="^f:custom$"),
                CallbackQueryHandler(end_to_menu, pattern="^m:home$"),
            ],
            WAITING_FOR_FREQ_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_freq_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Свои тексты: вход с /customtexts или кнопки «✏️ Свои тексты» (m:texts)
    customtexts_handler = ConversationHandler(
        entry_points=[
            CommandHandler("customtexts", customtexts_start),
            CallbackQueryHandler(customtexts_start, pattern="^m:texts$"),
        ],
        states={
            WAITING_FOR_CUSTOM_TEXT: [
                CommandHandler("skip", skip_custom_text),
                CallbackQueryHandler(skip_custom_text, pattern="^w:skip$"),
                CallbackQueryHandler(cancel_wizard, pattern="^w:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Полный сброс: вход с /reset или кнопки «🔄 Сбросить всё» (m:reset)
    reset_handler = ConversationHandler(
        entry_points=[
            CommandHandler("reset", reset_start),
            CallbackQueryHandler(reset_start, pattern="^m:reset$"),
        ],
        states={
            WAITING_FOR_RESET_CONFIRM: [
                CallbackQueryHandler(reset_confirm, pattern="^r:(yes|no)$"),
                CallbackQueryHandler(end_to_menu, pattern="^m:home$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Диалоги — первыми, чтобы они перехватывали свои callback'и
    app.add_handler(channel_handler)
    app.add_handler(freq_handler)
    app.add_handler(customtexts_handler)
    app.add_handler(reset_handler)

    # Slash-команды простых действий (дублируют кнопки меню)
    app.add_handler(CommandHandler("status",         act_status))
    app.add_handler(CommandHandler("ask",            act_ask))
    app.add_handler(CommandHandler("autopost",       act_autopost))
    app.add_handler(CommandHandler("start_autopost", act_run))
    app.add_handler(CommandHandler("stop_autopost",  act_stop))
    app.add_handler(CommandHandler("crashed",        act_crashed))
    app.add_handler(CommandHandler("checkin",        do_checkin))
    app.add_handler(CommandHandler("resettexts",     resettexts))
    app.add_handler(CommandHandler("help",           act_help))

    # Глобальный диспетчер простых кнопок и ответов опроса — последним
    app.add_handler(CallbackQueryHandler(
        on_menu,
        pattern="^(m:(ask|auto|run|stop|crash|status|help|home|checkin)|a:(no|yes))$",
    ))

    app.add_error_handler(error_handler)

    restore_jobs(app)
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
