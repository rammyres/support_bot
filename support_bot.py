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
- A representative can have SEVERAL customers connected at once. The bot
  routes replies automatically: just reply (Telegram's native reply
  feature) to a specific customer's message to answer them. If a rep has
  only one active conversation, plain messages go straight through with no
  reply needed. /chats lists active conversations with numbered slots;
  /switch <number> sets which one plain (non-reply) messages go to.
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

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
    BotCommand("chats", "List your active conversations"),
    BotCommand("switch", "Switch your default reply target"),
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
                current_focus_customer_id INTEGER,
                status_message_id INTEGER,
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
                customer_name TEXT,
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rep_id INTEGER NOT NULL,
                rep_message_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (rep_id, rep_message_id)
            )
            """
        )

        # --- migration for databases created before this feature existed ---
        existing_session_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        if "customer_name" not in existing_session_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN customer_name TEXT")

        existing_rep_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(representatives)")
        }
        if "current_focus_customer_id" not in existing_rep_cols:
            conn.execute(
                "ALTER TABLE representatives ADD COLUMN current_focus_customer_id INTEGER"
            )
        if "status_message_id" not in existing_rep_cols:
            conn.execute(
                "ALTER TABLE representatives ADD COLUMN status_message_id INTEGER"
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


def set_rep_status_message(rep_id: int, message_id: int):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "UPDATE representatives SET status_message_id = ? WHERE telegram_id = ?",
            (message_id, rep_id),
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

def start_session(customer_id: int, rep_id: int, customer_name: str = None):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO sessions (customer_id, rep_id, customer_name, active) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(customer_id) DO UPDATE SET rep_id = excluded.rep_id, "
            "customer_name = excluded.customer_name, active = 1, "
            "started_at = CURRENT_TIMESTAMP",
            (customer_id, rep_id, customer_name),
        )


def end_session_for_customer(customer_id: int):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "UPDATE sessions SET active = 0 WHERE customer_id = ?", (customer_id,)
        )
        # Clear focus on any rep who had this customer focused - it's no
        # longer a valid reply target.
        conn.execute(
            "UPDATE representatives SET current_focus_customer_id = NULL "
            "WHERE current_focus_customer_id = ?",
            (customer_id,),
        )


def get_active_session_by_customer(customer_id: int):
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE customer_id = ? AND active = 1",
            (customer_id,),
        ).fetchone()


def get_active_sessions_by_rep(rep_id: int):
    """A rep can now have several active customers at once."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE rep_id = ? AND active = 1 "
            "ORDER BY started_at",
            (rep_id,),
        ).fetchall()


def set_rep_focus(rep_id: int, customer_id):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "UPDATE representatives SET current_focus_customer_id = ? "
            "WHERE telegram_id = ?",
            (customer_id, rep_id),
        )


# --- thread map (maps a message sent into the rep's chat back to the   --- #
# --- customer it came from, so replies route correctly)                --- #

def record_thread_map(rep_id: int, rep_message_id: int, customer_id: int):
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO thread_map (rep_id, rep_message_id, customer_id) "
            "VALUES (?, ?, ?)",
            (rep_id, rep_message_id, customer_id),
        )


def lookup_thread_customer(rep_id: int, rep_message_id: int):
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT customer_id FROM thread_map WHERE rep_id = ? AND rep_message_id = ?",
            (rep_id, rep_message_id),
        ).fetchone()
        return row["customer_id"] if row else None


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
# Representative status panel (persistent, editable message with buttons)
# --------------------------------------------------------------------------- #

def _rep_panel_content(rep_id: int):
    """
    Build the text and InlineKeyboardMarkup for the representative's status
    panel. Returns (text, markup). Called both when creating the panel for
    the first time and whenever it needs to be refreshed.
    """
    sessions = get_active_sessions_by_rep(rep_id)
    rep = get_rep(rep_id)
    focus = rep["current_focus_customer_id"] if rep else None

    if not sessions:
        text = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💤  No active conversations\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 New code", callback_data="new_code")],
        ])
        return text, markup

    lines = ["━━━━━━━━━━━━━━━━━━━━━━"]
    focus_name = None
    focus_customer_id = None
    for i, s in enumerate(sessions, 1):
        name = s["customer_name"] or "Customer"
        if s["customer_id"] == focus:
            lines.append(f"👉 #{i}  {name}   ← talking to")
            focus_name = name
            focus_customer_id = s["customer_id"]
        else:
            lines.append(f"      #{i}  {name}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    text = "\n".join(lines)

    buttons = []

    # One button per non-default conversation so the rep can switch with one tap
    switch_row = []
    for i, s in enumerate(sessions, 1):
        if s["customer_id"] != focus:
            name = s["customer_name"] or "Customer"
            switch_row.append(
                InlineKeyboardButton(
                    f"↔️ Talk to #{i} {name}",
                    callback_data=f"set_focus:{s['customer_id']}",
                )
            )
    # Split into rows of 2 so buttons don't overflow on mobile
    for i in range(0, len(switch_row), 2):
        buttons.append(switch_row[i:i + 2])

    # Action row
    action_row = [InlineKeyboardButton("🔑 New code", callback_data="new_code")]
    if focus_customer_id:
        action_row.append(
            InlineKeyboardButton(
                f"🔚 End with {focus_name}",
                callback_data=f"end_chat:{focus_customer_id}",
            )
        )
    buttons.append(action_row)

    return text, InlineKeyboardMarkup(buttons)


async def upsert_rep_panel(context, rep_id: int):
    """
    Send the status panel to the rep if they don't have one yet, or edit
    the existing panel message in place. Falls back to sending a fresh
    message if the old one can no longer be edited (too old, or deleted).
    """
    rep = get_rep(rep_id)
    if rep is None:
        return

    text, markup = _rep_panel_content(rep_id)
    msg_id = rep["status_message_id"]

    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=rep_id,
                message_id=msg_id,
                text=text,
                reply_markup=markup,
            )
            return
        except Exception:
            # Message too old, deleted, or content unchanged — send a new one
            pass

    try:
        msg = await context.bot.send_message(
            chat_id=rep_id, text=text, reply_markup=markup
        )
        set_rep_status_message(rep_id, msg.message_id)
    except Exception:
        logger.exception("Could not send status panel for rep %s", rep_id)


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
# Multi-chat routing for representatives
# --------------------------------------------------------------------------- #

async def resolve_target_customer_for_rep(update: Update, context: ContextTypes.DEFAULT_TYPE, rep_id: int):
    """
    Figures out which customer a representative's message/command is meant
    for, when they may have several active conversations at once.

    Resolution order:
      1. Only one active conversation -> that's the target, no ambiguity.
      2. Message is a Telegram reply to a previously relayed message -> use
         the customer that message came from, update focus, confirm source.
      3. A "focus" customer was set earlier via /switch -> use that.
      4. Otherwise: show a numbered list and ask the rep to either reply to
         a message or use /switch <number>, then return (None, None).

    Returns (customer_id, customer_name) or (None, None) if no sessions
    exist or the choice is ambiguous (a clarifying message has been sent).
    """
    sessions = get_active_sessions_by_rep(rep_id)
    if not sessions:
        await update.message.reply_text(
            "You don't have any active conversations right now.\n"
            "Use /newcode to generate one for a customer."
        )
        return None, None

    if len(sessions) == 1:
        s = sessions[0]
        return s["customer_id"], s["customer_name"] or "Customer"

    active_ids = {s["customer_id"]: (s["customer_name"] or "Customer") for s in sessions}

    if update.message.reply_to_message:
        mapped = lookup_thread_customer(rep_id, update.message.reply_to_message.message_id)
        if mapped is not None and mapped in active_ids:
            set_rep_focus(rep_id, mapped)
            return mapped, active_ids[mapped]

    rep = get_rep(rep_id)
    focus = rep["current_focus_customer_id"] if rep else None
    if focus is not None and focus in active_ids:
        return focus, active_ids[focus]

    # Ambiguous — show a numbered list so the rep can use /switch 1, /switch 2, etc.
    lines = []
    for i, s in enumerate(sessions, 1):
        lines.append(f"  #{i} — {s['customer_name'] or 'Customer'}")
    await update.message.reply_text(
        "💬 You have multiple active conversations and no default is set.\n\n"
        + "\n".join(lines)
        + "\n\n"
        "To answer someone: long-press their message and tap Reply.\n"
        "To set a default: /switch 1, /switch 2, etc. (numbers from /chats)."
    )
    return None, None


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
    customer_name = update.effective_user.full_name or "Customer"

    start_session(user_id, rep_id, customer_name)
    set_rep_focus(rep_id, user_id)  # new arrival becomes the default immediately

    customer_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔚 End conversation", callback_data="end_self")]
    ])
    await update.message.reply_text(
        f"✅ Connected! You're now chatting with {rep_name}.\n"
        "Send text, photos, videos, voice messages, or files - they'll be "
        "delivered directly.\nTap the button below or send /end to finish.",
        reply_markup=customer_markup,
    )
    try:
        sessions = get_active_sessions_by_rep(rep_id)
        active_count = len(sessions)
        others = [s["customer_name"] or "Customer" for s in sessions if s["customer_id"] != user_id]

        if active_count == 1:
            note = f"🔔 {customer_name} just connected. You can start chatting now."
        else:
            others_str = ", ".join(others) if others else "—"
            note = (
                f"🔔 {customer_name} just connected and is now your active conversation.\n\n"
                f"Other open chats: {others_str}"
            )
        await context.bot.send_message(chat_id=rep_id, text=note)
        await upsert_rep_panel(context, rep_id)
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
        customer_name = session["customer_name"] or "Customer"
        end_session_for_customer(user_id)
        await update.message.reply_text("Conversation ended. Take care!")
        try:
            remaining = get_active_sessions_by_rep(rep_id)
            if remaining:
                next_name = remaining[0]["customer_name"] or "Customer"
                set_rep_focus(rep_id, remaining[0]["customer_id"])
                tail = f"\n\nYou still have {len(remaining)} open conversation(s). Default is now: {next_name}."
            else:
                tail = "\n\nYou have no more active conversations."
            await context.bot.send_message(
                chat_id=rep_id,
                text=f"ℹ️ {customer_name} ended the conversation.{tail}"
            )
            await upsert_rep_panel(context, rep_id)
        except Exception:
            logger.exception("Could not notify rep %s of session end", rep_id)
        return

    rep = get_rep(user_id)
    if rep:
        customer_id, customer_name = await resolve_target_customer_for_rep(update, context, user_id)
        if customer_id is None:
            return

        end_session_for_customer(customer_id)

        remaining = get_active_sessions_by_rep(user_id)
        if remaining:
            next_name = remaining[0]["customer_name"] or "Customer"
            set_rep_focus(user_id, remaining[0]["customer_id"])
            tail = f"\nDefault is now: {next_name}."
        else:
            tail = "\nNo more active conversations."
        await update.message.reply_text(f"Conversation with {customer_name} ended.{tail}")
        await upsert_rep_panel(context, user_id)

        try:
            await context.bot.send_message(
                chat_id=customer_id,
                text="ℹ️ The representative has ended the conversation.",
            )
        except Exception:
            logger.exception("Could not notify customer %s of session end", customer_id)
        return

    await update.message.reply_text("You don't have an active conversation.")


async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep_id = update.effective_user.id
    rep = get_rep(rep_id)
    if rep is None:
        await update.message.reply_text("Please run /register first.")
        return

    sessions = get_active_sessions_by_rep(rep_id)
    if not sessions:
        await update.message.reply_text(
            "You have no active conversations right now. Use /newcode to start one."
        )
        return

    focus = rep["current_focus_customer_id"]
    lines = []
    for i, s in enumerate(sessions, 1):
        name = s["customer_name"] or "Customer"
        is_focus = s["customer_id"] == focus
        marker = "👉" if is_focus else f"#{i}"
        label = " (active default)" if is_focus else ""
        lines.append(f"{marker} {name}{label}  →  /switch {i}")

    header = f"💬 You have {len(sessions)} active conversation(s):\n"
    footer = (
        "\n\nTo reply to someone: long-press their message → Reply.\n"
        "To change default: /switch <number>"
    )
    await update.message.reply_text(header + "\n".join(lines) + footer)


async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep_id = update.effective_user.id
    rep = get_rep(rep_id)
    if rep is None:
        await update.message.reply_text("Please run /register first.")
        return

    sessions = get_active_sessions_by_rep(rep_id)
    if not sessions:
        await update.message.reply_text(
            "You have no active conversations right now. Use /newcode to start one."
        )
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Usage: /switch <number>  — use /chats to see the numbered list."
        )
        return

    slot = int(context.args[0])
    if slot < 1 or slot > len(sessions):
        await update.message.reply_text(
            f"There's no #{slot} in your list. You have {len(sessions)} active conversation(s).\n"
            "Use /chats to see the current numbers."
        )
        return

    chosen = sessions[slot - 1]
    set_rep_focus(rep_id, chosen["customer_id"])
    name = chosen["customer_name"] or "Customer"
    await update.message.reply_text(
        f"✅ Switched to #{slot} — {name}.\n"
        "Your plain messages will go to them until you reply to someone else or /switch again."
    )
    await upsert_rep_panel(context, rep_id)


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
        customer_name = session["customer_name"] or update.effective_user.full_name or "Customer"
        await save_media_if_present(message, context, user_id, rep_id, "customer")
        try:
            # Build label buttons. Always show End; show Set-as-default only
            # when this customer isn't already the rep's current focus.
            rep_state = get_rep(rep_id)
            label_buttons = [[
                InlineKeyboardButton(
                    "🔚 End chat", callback_data=f"end_chat:{user_id}"
                )
            ]]
            sessions_for_rep = get_active_sessions_by_rep(rep_id)
            if (
                len(sessions_for_rep) > 1
                and rep_state
                and rep_state["current_focus_customer_id"] != user_id
            ):
                label_buttons[0].insert(
                    0,
                    InlineKeyboardButton(
                        "👉 Talk to them", callback_data=f"set_focus:{user_id}"
                    ),
                )
            label_markup = InlineKeyboardMarkup(label_buttons)

            label_msg = await context.bot.send_message(
                chat_id=rep_id,
                text=f"🙂 {customer_name}",
                reply_markup=label_markup,
            )
            copied = await context.bot.copy_message(
                chat_id=rep_id,
                from_chat_id=user_id,
                message_id=message.message_id,
            )
            record_thread_map(rep_id, label_msg.message_id, user_id)
            record_thread_map(rep_id, copied.message_id, user_id)
        except Exception:
            logger.exception("Failed relaying customer->rep message")
            await message.reply_text(
                "⚠️ Could not deliver your message. Please try again."
            )
        return

    # Case 2: sender is a registered representative who has set their name
    # -> figure out which customer they're replying to and relay to them.
    rep = get_rep(user_id)
    if rep and not rep["pending_name_setup"]:
        focus_before = rep["current_focus_customer_id"]

        customer_id, customer_name = await resolve_target_customer_for_rep(update, context, user_id)
        if customer_id is None:
            return

        rep_name = rep["name"] or "Representative"
        await save_media_if_present(message, context, customer_id, user_id, "representative")
        try:
            await context.bot.send_message(chat_id=customer_id, text=f"👤 {rep_name}")
            await context.bot.copy_message(
                chat_id=customer_id,
                from_chat_id=user_id,
                message_id=message.message_id,
            )
            focus_after = get_rep(user_id)["current_focus_customer_id"]
            if focus_before != focus_after and focus_before is not None:
                confirm = f"↩️ Reply sent to {customer_name}. (Default switched to them.)"
            else:
                confirm = f"✉️ Sent to {customer_name}."
            await update.message.reply_text(confirm)
            # Refresh the panel if focus changed
            if focus_before != focus_after:
                await upsert_rep_panel(context, user_id)
        except Exception:
            logger.exception("Failed relaying rep->customer message")
            await message.reply_text(
                "⚠️ Could not deliver your message. Please try again."
            )
        return

    # Case 3: representative is mid name-setup (no active session yet)
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
# Button callback handler
# --------------------------------------------------------------------------- #

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()          # acknowledge immediately to dismiss loading state
    user_id = update.effective_user.id
    data = query.data

    # ── new_code ─────────────────────────────────────────────────────────────
    if data == "new_code":
        rep = get_rep(user_id)
        if not rep or rep["pending_name_setup"] or not rep["approved"]:
            await query.answer("Not available right now.", show_alert=True)
            return
        code = generate_code(user_id)
        bot_username = context.bot.username
        link = f"https://t.me/{bot_username}?start={code}"
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🔑 New connection code: {code}\n\n"
                 f"Send this link to your customer:\n{link}\n\n"
                 f"Or manual code:\n/start {code}",
        )

    # ── set_focus:<customer_id> ───────────────────────────────────────────────
    elif data.startswith("set_focus:"):
        customer_id = int(data.split(":")[1])
        sessions = get_active_sessions_by_rep(user_id)
        session = next((s for s in sessions if s["customer_id"] == customer_id), None)
        if not session:
            await query.answer("That conversation is no longer active.", show_alert=True)
        else:
            set_rep_focus(user_id, customer_id)
            name = session["customer_name"] or "Customer"
            await query.answer(f"Switched to {name}")
        await upsert_rep_panel(context, user_id)

    # ── end_chat:<customer_id> ────────────────────────────────────────────────
    elif data.startswith("end_chat:"):
        customer_id = int(data.split(":")[1])
        session = get_active_session_by_customer(customer_id)
        if not session or session["rep_id"] != user_id:
            await query.answer("That conversation is no longer active.", show_alert=True)
            await upsert_rep_panel(context, user_id)
            return
        name = session["customer_name"] or "Customer"
        end_session_for_customer(customer_id)
        remaining = get_active_sessions_by_rep(user_id)
        if remaining:
            set_rep_focus(user_id, remaining[0]["customer_id"])
            next_name = remaining[0]["customer_name"] or "Customer"
            await query.answer(f"Ended with {name}. Now talking to {next_name}.")
        else:
            await query.answer(f"Ended with {name}.")
        try:
            await context.bot.send_message(
                chat_id=customer_id,
                text="ℹ️ The representative has ended the conversation.",
            )
        except Exception:
            logger.exception("Could not notify customer %s", customer_id)
        await upsert_rep_panel(context, user_id)

    # ── end_self (customer tapping their own End button) ─────────────────────
    elif data == "end_self":
        session = get_active_session_by_customer(user_id)
        if not session:
            await query.answer("No active conversation.", show_alert=True)
            return
        rep_id = session["rep_id"]
        customer_name = session["customer_name"] or "Customer"
        end_session_for_customer(user_id)
        await context.bot.send_message(chat_id=user_id, text="Conversation ended. Take care!")
        try:
            remaining = get_active_sessions_by_rep(rep_id)
            if remaining:
                set_rep_focus(rep_id, remaining[0]["customer_id"])
                next_name = remaining[0]["customer_name"] or "Customer"
                tail = f"\n\nDefault is now: {next_name}."
            else:
                tail = "\n\nNo more active conversations."
            await context.bot.send_message(
                chat_id=rep_id,
                text=f"ℹ️ {customer_name} ended the conversation.{tail}",
            )
            await upsert_rep_panel(context, rep_id)
        except Exception:
            logger.exception("Could not notify rep %s", rep_id)


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
    application.add_handler(CommandHandler("chats", chats_command))
    application.add_handler(CommandHandler("switch", switch_command))
    application.add_handler(CommandHandler("end", end))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("revoke", revoke))
    application.add_handler(CommandHandler("listreps", list_reps_command))
    application.add_handler(CallbackQueryHandler(handle_button))

    # Catch-all for everything else (text, photo, video, audio, voice,
    # document, video_note, sticker, ...) that isn't a command.
    application.add_handler(MessageHandler(~filters.COMMAND, relay))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()