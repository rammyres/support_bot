#!/usr/bin/env python3
"""
Telegram Customer <-> Representative Relay Bot
================================================

WHAT THIS BOT DOES
-------------------
- Admins (configured by Telegram user ID) approve who is allowed to act as
  a representative.
- An approved representative registers with the bot and picks a display
  name.
- The representative generates a one-time CODE and gives it to a customer
  (outside of Telegram - by phone, email, website, etc).
- The customer opens a private chat with the bot and sends:  /start CODE
- The bot links the customer and that representative into a live session.
- While the session is active, ANY message either side sends (text, photo,
  video, voice note, audio file, document, video note, sticker) is relayed
  to the other side automatically.
- Every photo, video, document, audio, voice note and video note that
  passes through a conversation is also downloaded and saved to local disk,
  with a matching record in the database (who sent it, when, to/from whom).
- Either side can end the session with /end.

IMPORTANT TELEGRAM LIMITATION (please read)
--------------------------------------------
Telegram's "Forwarded from <name>" tag is a native client feature tied to a
REAL account or channel identity. The Bot API gives bots no way to set that
tag to an arbitrary custom string - this is intentional, to prevent bots
from impersonating people.

So this bot does NOT use real Telegram message forwarding. Instead it uses
`copy_message`, which re-sends the content as a normal message with no
forward tag at all, and the bot prepends a small label line with the
representative's chosen name, e.g.:

    👤 João
    Hi, how can I help you today?

PRIVACY / COMPLIANCE NOTE
--------------------------
This bot stores customer files (photos, videos, documents, audio) on local
disk indefinitely. Depending on your jurisdiction (e.g. Brazil's LGPD, or
GDPR in the EU) you are likely required to: inform customers their files
and messages are being recorded, define a retention/deletion policy, and
restrict who can access the storage folder and database. This script does
not implement automatic deletion or consent prompts - add those if your
legal requirements call for them.

SETUP
-----
1. pip install python-telegram-bot
2. Get a bot token from @BotFather on Telegram.
3. Set environment variables:
     export BOT_TOKEN="123456:ABC..."
     export ADMIN_IDS="111111111,222222222"   # your Telegram user IDs
4. Run:  python3 support_bot.py

State is kept in a local SQLite file (support_bot.db) and downloaded media
goes into ./media_storage/ - both in the working directory, so they survive
restarts.

HOW TO FIND YOUR TELEGRAM USER ID
----------------------------------
Message @userinfobot (or any "what is my id" bot) on Telegram - it will
reply with your numeric ID. Put that number in ADMIN_IDS.
"""

import logging
import os
import secrets
import sqlite3
import string
from contextlib import closing
from datetime import datetime

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.environ.get("SUPPORT_BOT_DB", "support_bot.db")
MEDIA_DIR = os.environ.get("SUPPORT_BOT_MEDIA_DIR", "media_storage")
CODE_LENGTH = 8
CODE_ALPHABET = string.ascii_uppercase + string.digits

ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("support_bot")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --------------------------------------------------------------------------- #
# Bottom command menu (the list shown when tapping the "/" menu button)
# --------------------------------------------------------------------------- #

DEFAULT_COMMANDS = [
    BotCommand("start", "Connect with a code, or see instructions"),
    BotCommand("register", "Register as a representative"),
    BotCommand("end", "End the current conversation"),
]

REP_COMMANDS = DEFAULT_COMMANDS + [
    BotCommand("name", "Change your display name"),
    BotCommand("newcode", "Generate a one-time customer code"),
]

ADMIN_COMMANDS = REP_COMMANDS + [
    BotCommand("approve", "Approve a representative"),
    BotCommand("revoke", "Revoke a representative"),
    BotCommand("listreps", "List all representatives"),
]


async def sync_menu_for(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """
    Sets the bottom command menu for a specific user, based on their role:
    admin > approved representative > pending representative / customer.
    Safe to call any time their role changes (register, approve, revoke).
    """
    if is_admin(user_id):
        commands = ADMIN_COMMANDS
    else:
        rep = get_rep(user_id)
        commands = REP_COMMANDS if (rep and rep["approved"]) else DEFAULT_COMMANDS

    try:
        await context.bot.set_my_commands(
            commands, scope=BotCommandScopeChat(chat_id=user_id)
        )
    except Exception:
        logger.exception("Could not set command menu for %s", user_id)


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS representatives (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT,
                pending_name_setup INTEGER DEFAULT 1,
                approved INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                rep_id INTEGER NOT NULL,
                used INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (rep_id) REFERENCES representatives (telegram_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                customer_id INTEGER PRIMARY KEY,
                rep_id INTEGER NOT NULL,
                active INTEGER DEFAULT 1,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (rep_id) REFERENCES representatives (telegram_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                rep_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL,       -- 'customer' or 'representative'
                media_type TEXT NOT NULL,        -- photo/video/document/audio/voice/video_note
                telegram_file_id TEXT NOT NULL,
                local_path TEXT NOT NULL,
                caption TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


# --- representatives -------------------------------------------------------

def get_rep(rep_id: int):
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM representatives WHERE telegram_id = ?", (rep_id,)
        ).fetchone()


def create_or_get_rep(rep_id: int):
    rep = get_rep(rep_id)
    if rep is None:
        auto_approved = 1 if is_admin(rep_id) else 0
        with closing(db_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO representatives (telegram_id, pending_name_setup, approved) "
                "VALUES (?, 1, ?)",
                (rep_id, auto_approved),
            )
        rep = get_rep(rep_id)
    return rep


def set_rep_name(rep_id: int, name: str):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "UPDATE representatives SET name = ?, pending_name_setup = 0 "
            "WHERE telegram_id = ?",
            (name, rep_id),
        )


def set_rep_approved(rep_id: int, approved: bool):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "UPDATE representatives SET approved = ? WHERE telegram_id = ?",
            (1 if approved else 0, rep_id),
        )


def list_reps():
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM representatives ORDER BY created_at DESC"
        ).fetchall()


# --- codes -------------------------------------------------------------

def generate_code(rep_id: int) -> str:
    with closing(db_connect()) as conn, conn:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            exists = conn.execute(
                "SELECT 1 FROM codes WHERE code = ?", (code,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO codes (code, rep_id) VALUES (?, ?)", (code, rep_id)
                )
                return code


def consume_code(code: str):
    """Returns rep_id if the code is valid and unused, marks it used. Else None."""
    with closing(db_connect()) as conn, conn:
        row = conn.execute(
            "SELECT * FROM codes WHERE code = ? AND used = 0", (code,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE codes SET used = 1 WHERE code = ?", (code,))
        return row["rep_id"]


# --- sessions ------------------------------------------------------------

def start_session(customer_id: int, rep_id: int):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO sessions (customer_id, rep_id, active) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(customer_id) DO UPDATE SET rep_id = excluded.rep_id, "
            "active = 1, started_at = CURRENT_TIMESTAMP",
            (customer_id, rep_id),
        )


def end_session_for_customer(customer_id: int):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "UPDATE sessions SET active = 0 WHERE customer_id = ?", (customer_id,)
        )


def get_active_session_by_customer(customer_id: int):
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE customer_id = ? AND active = 1",
            (customer_id,),
        ).fetchone()


def get_active_session_by_rep(rep_id: int):
    """A rep may have at most one active customer at a time in this design."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE rep_id = ? AND active = 1", (rep_id,)
        ).fetchone()


# --- media log -------------------------------------------------------------

def log_media(customer_id, rep_id, sender_role, media_type, file_id, local_path, caption):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO media_log "
            "(customer_id, rep_id, sender_role, media_type, telegram_file_id, "
            "local_path, caption) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (customer_id, rep_id, sender_role, media_type, file_id, local_path, caption),
        )


# --------------------------------------------------------------------------- #
# Admin helpers
# --------------------------------------------------------------------------- #

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            logger.exception("Could not notify admin %s", admin_id)


async def admin_only_guard(update: Update) -> bool:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ This command is for admins only.")
        return False
    return True


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve <telegram_id>")
        return

    target_id = int(context.args[0])
    rep = get_rep(target_id)
    if rep is None:
        await update.message.reply_text(
            "That user hasn't run /register yet, nothing to approve."
        )
        return

    set_rep_approved(target_id, True)
    await sync_menu_for(context, target_id)
    await update.message.reply_text(f"✅ Approved representative {target_id}.")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 You've been approved as a representative! "
                 "Use /newcode to generate a connection code for a customer.",
        )
    except Exception:
        logger.exception("Could not notify newly approved rep %s", target_id)


async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke <telegram_id>")
        return

    target_id = int(context.args[0])
    rep = get_rep(target_id)
    if rep is None:
        await update.message.reply_text("That user is not registered.")
        return

    set_rep_approved(target_id, False)
    await sync_menu_for(context, target_id)
    await update.message.reply_text(f"🚫 Revoked representative access for {target_id}.")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="Your representative access has been revoked by an admin.",
        )
    except Exception:
        logger.exception("Could not notify revoked rep %s", target_id)


async def list_reps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return

    reps = list_reps()
    if not reps:
        await update.message.reply_text("No representatives registered yet.")
        return

    lines = []
    for r in reps:
        status = "✅ approved" if r["approved"] else "⏳ pending"
        name = r["name"] or "(no name set)"
        lines.append(f"{r['telegram_id']} - {name} - {status}")

    await update.message.reply_text("Representatives:\n" + "\n".join(lines))


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if not args:
        await update.message.reply_text(
            "👋 Welcome!\n\n"
            "If you have a connection code from a representative, send it like this:\n"
            "/start YOURCODE\n\n"
            "If you are a representative, use /register instead."
        )
        return

    code = args[0].strip().upper()

    existing = get_active_session_by_customer(user_id)
    if existing:
        await update.message.reply_text(
            "You already have an active conversation. Send /end to close it first."
        )
        return

    rep_id = consume_code(code)
    if rep_id is None:
        await update.message.reply_text(
            "❌ That code is invalid or has already been used. "
            "Please check with your representative."
        )
        return

    rep = get_rep(rep_id)
    rep_name = rep["name"] if rep and rep["name"] else "Representative"

    if get_active_session_by_rep(rep_id):
        await update.message.reply_text(
            "⚠️ This representative is currently in another conversation. "
            "Please ask them for a new code in a moment."
        )
        return

    start_session(user_id, rep_id)

    await update.message.reply_text(
        f"✅ Connected! You're now chatting with {rep_name}.\n"
        "Send text, photos, videos, voice messages, or files - they'll be "
        "delivered directly.\nSend /end to finish the conversation."
    )
    try:
        await context.bot.send_message(
            chat_id=rep_id,
            text=f"🔔 A customer has connected using code {code}. You can chat now.\n"
                 "Send /end to finish the conversation.",
        )
    except Exception:
        logger.exception("Could not notify representative %s", rep_id)


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep_id = update.effective_user.id
    rep = create_or_get_rep(rep_id)
    await sync_menu_for(context, rep_id)

    if rep["name"]:
        status = "✅ approved" if rep["approved"] else "⏳ pending admin approval"
        await update.message.reply_text(
            f"You're already registered as '{rep['name']}' ({status}).\n"
            "Use /name <new name> to change your display name."
        )
        return

    await update.message.reply_text(
        "👤 Welcome! Before you start, please choose the display name that "
        "customers will see on your messages. Just send it as plain text now."
    )


async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep_id = update.effective_user.id
    rep = get_rep(rep_id)
    if rep is None:
        await update.message.reply_text("Please run /register first.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /name Your Display Name")
        return

    new_name = " ".join(context.args).strip()
    set_rep_name(rep_id, new_name)
    await update.message.reply_text(f"✅ Your display name is now '{new_name}'.")


async def new_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep_id = update.effective_user.id
    rep = get_rep(rep_id)

    if rep is None or rep["pending_name_setup"]:
        await update.message.reply_text(
            "Please finish registering first with /register."
        )
        return

    if not rep["approved"]:
        await update.message.reply_text(
            "⏳ Your representative account is still awaiting admin approval."
        )
        return

    if get_active_session_by_rep(rep_id):
        await update.message.reply_text(
            "You already have an active conversation. Finish it with /end "
            "before starting a new one."
        )
        return

    code = generate_code(rep_id)
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={code}"
    await update.message.reply_text(
        f"🔑 New connection code: {code}\n\n"
        f"Give this to your customer. They can either tap this link:\n{link}\n\n"
        f"...or send the code manually:\n/start {code}"
    )


async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    session = get_active_session_by_customer(user_id)
    if session:
        rep_id = session["rep_id"]
        end_session_for_customer(user_id)
        await update.message.reply_text("Conversation ended. Take care!")
        try:
            await context.bot.send_message(
                chat_id=rep_id, text="ℹ️ The customer has ended the conversation."
            )
        except Exception:
            logger.exception("Could not notify rep %s of session end", rep_id)
        return

    session = get_active_session_by_rep(user_id)
    if session:
        customer_id = session["customer_id"]
        end_session_for_customer(customer_id)
        await update.message.reply_text("Conversation ended.")
        try:
            await context.bot.send_message(
                chat_id=customer_id,
                text="ℹ️ The representative has ended the conversation.",
            )
        except Exception:
            logger.exception(
                "Could not notify customer %s of session end", customer_id
            )
        return

    await update.message.reply_text("You don't have an active conversation.")


# --------------------------------------------------------------------------- #
# Media download / storage
# --------------------------------------------------------------------------- #

async def _download(context, file_id, dest_path):
    file = await context.bot.get_file(file_id)
    await file.download_to_drive(dest_path)


async def save_media_if_present(message, context, customer_id, rep_id, sender_role):
    """
    Detects photo/video/document/audio/voice/video_note on an incoming
    message, downloads it to MEDIA_DIR, and logs it in media_log.
    Returns nothing - failures are logged but never block the relay.
    """
    media_type = None
    file_id = None
    suggested_ext = ""

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id  # highest resolution
        suggested_ext = ".jpg"
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        suggested_ext = ".mp4"
    elif message.video_note:
        media_type = "video_note"
        file_id = message.video_note.file_id
        suggested_ext = ".mp4"
    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id
        suggested_ext = ".ogg"
    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id
        suggested_ext = os.path.splitext(message.audio.file_name or "")[1] or ".mp3"
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
        suggested_ext = os.path.splitext(message.document.file_name or "")[1] or ""

    if not media_type:
        return  # plain text or unsupported type - nothing to store

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    customer_dir = os.path.join(MEDIA_DIR, str(customer_id))
    os.makedirs(customer_dir, exist_ok=True)
    filename = f"{timestamp}_{sender_role}_{media_type}{suggested_ext}"
    local_path = os.path.join(customer_dir, filename)

    try:
        await _download(context, file_id, local_path)
        log_media(
            customer_id=customer_id,
            rep_id=rep_id,
            sender_role=sender_role,
            media_type=media_type,
            file_id=file_id,
            local_path=local_path,
            caption=message.caption,
        )
        logger.info("Stored %s from %s -> %s", media_type, sender_role, local_path)
    except Exception:
        logger.exception("Failed to download/store media (%s)", media_type)


# --------------------------------------------------------------------------- #
# Message relay (text, photos, video, audio, voice, documents, etc.)
# --------------------------------------------------------------------------- #

async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    if message is None:
        return

    # Case 1: sender is a customer in an active session -> relay to rep
    session = get_active_session_by_customer(user_id)
    if session:
        rep_id = session["rep_id"]
        await save_media_if_present(message, context, user_id, rep_id, "customer")
        try:
            await context.bot.send_message(chat_id=rep_id, text="🙂 Customer")
            await context.bot.copy_message(
                chat_id=rep_id,
                from_chat_id=user_id,
                message_id=message.message_id,
            )
        except Exception:
            logger.exception("Failed relaying customer->rep message")
            await message.reply_text(
                "⚠️ Could not deliver your message. Please try again."
            )
        return

    # Case 2: sender is a representative in an active session -> relay to customer
    session = get_active_session_by_rep(user_id)
    if session:
        rep = get_rep(user_id)
        customer_id = session["customer_id"]
        rep_name = rep["name"] if rep and rep["name"] else "Representative"

        await save_media_if_present(
            message, context, customer_id, user_id, "representative"
        )
        try:
            await context.bot.send_message(chat_id=customer_id, text=f"👤 {rep_name}")
            await context.bot.copy_message(
                chat_id=customer_id,
                from_chat_id=user_id,
                message_id=message.message_id,
            )
        except Exception:
            logger.exception("Failed relaying rep->customer message")
            await message.reply_text(
                "⚠️ Could not deliver your message. Please try again."
            )
        return

    # Case 3: representative is mid name-setup (no active session yet)
    rep = get_rep(user_id)
    if rep and rep["pending_name_setup"]:
        if message.text:
            new_name = message.text.strip()
            set_rep_name(user_id, new_name)
            await sync_menu_for(context, user_id)
            if rep["approved"]:
                await message.reply_text(
                    f"✅ Got it, you'll appear as '{new_name}'.\n"
                    "Use /newcode to generate a connection code for a customer."
                )
            else:
                await message.reply_text(
                    f"✅ Got it, you'll appear as '{new_name}'.\n"
                    "⏳ Your account is now awaiting admin approval before you "
                    "can generate connection codes."
                )
                await notify_admins(
                    context,
                    f"🆕 New representative request:\n"
                    f"ID: {user_id}\nName: {new_name}\n\n"
                    f"Approve with: /approve {user_id}",
                )
        else:
            await message.reply_text("Please send your display name as plain text.")
        return

    # Case 4: nobody is in a session and not mid-setup
    await message.reply_text(
        "You don't have an active conversation right now.\n"
        "Customers: use /start <code>. Representatives: use /register."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def post_init(application: Application):
    """Runs once on startup: sets the global default menu, then pushes the
    correct menu to every admin and already-approved representative."""
    try:
        await application.bot.set_my_commands(
            DEFAULT_COMMANDS, scope=BotCommandScopeDefault()
        )
    except Exception:
        logger.exception("Could not set default command menu")

    # Use a lightweight shim so sync_menu_for's `context.bot` access works
    # even though we only have `application` at startup time.
    class _BotOnly:
        def __init__(self, bot):
            self.bot = bot

    ctx = _BotOnly(application.bot)
    for admin_id in ADMIN_IDS:
        await sync_menu_for(ctx, admin_id)
    for rep in list_reps():
        if rep["approved"]:
            await sync_menu_for(ctx, rep["telegram_id"])


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Please set the BOT_TOKEN environment variable with your bot token "
            "from @BotFather."
        )
    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty - nobody will be able to approve representatives. "
            "Set the ADMIN_IDS environment variable with your Telegram user ID(s)."
        )

    init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("name", name_command))
    application.add_handler(CommandHandler("newcode", new_code))
    application.add_handler(CommandHandler("end", end))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("revoke", revoke))
    application.add_handler(CommandHandler("listreps", list_reps_command))

    # Catch-all for everything else (text, photo, video, audio, voice,
    # document, video_note, sticker, ...) that isn't a command.
    application.add_handler(MessageHandler(~filters.COMMAND, relay))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()