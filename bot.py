import logging
import os
import sqlite3
from contextlib import contextmanager

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "swaps.db")
GROUPS = list(range(1, 9))  # G1..G8

# Conversation states
ASK_NAME, ASK_CURRENT, ASK_DESIRED = range(3)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                current_group INTEGER NOT NULL,
                desired_group INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'searching'  -- searching | matched
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_a INTEGER NOT NULL,
                user_b INTEGER NOT NULL,
                a_confirmed INTEGER NOT NULL DEFAULT 0,
                b_confirmed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'  -- pending | confirmed | cancelled
            )
            """
        )


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def upsert_user(user_id, full_name, username, current_group, desired_group):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, full_name, username, current_group, desired_group, status)
            VALUES (?, ?, ?, ?, ?, 'searching')
            ON CONFLICT(user_id) DO UPDATE SET
                full_name=excluded.full_name,
                username=excluded.username,
                current_group=excluded.current_group,
                desired_group=excluded.desired_group,
                status='searching'
            """,
            (user_id, full_name, username, current_group, desired_group),
        )


def set_user_status(user_id, status):
    with db() as conn:
        conn.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))


def delete_user(user_id):
    with db() as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


def find_reverse_candidates(user_id, current_group, desired_group):
    """Other active users whose swap is the exact mirror of this one."""
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM users
            WHERE status = 'searching'
              AND user_id != ?
              AND current_group = ?
              AND desired_group = ?
            """,
            (user_id, desired_group, current_group),
        ).fetchall()


def existing_pending_match(user_a, user_b):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM matches
            WHERE status = 'pending'
              AND ((user_a = ? AND user_b = ?) OR (user_a = ? AND user_b = ?))
            """,
            (user_a, user_b, user_b, user_a),
        ).fetchone()


def create_match(user_a, user_b):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO matches (user_a, user_b) VALUES (?, ?)", (user_a, user_b)
        )
        return cur.lastrowid


def get_match(match_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()


def set_confirmed(match_id, is_a):
    field = "a_confirmed" if is_a else "b_confirmed"
    with db() as conn:
        conn.execute(f"UPDATE matches SET {field} = 1 WHERE match_id = ?", (match_id,))


def set_match_status(match_id, status):
    with db() as conn:
        conn.execute("UPDATE matches SET status = ? WHERE match_id = ?", (status, match_id))


def pending_matches_for_user(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM matches
            WHERE status = 'pending' AND (user_a = ? OR user_b = ?)
            """,
            (user_id, user_id),
        ).fetchall()


def cancel_other_pending_matches(user_id, except_match_id):
    """Called once a user confirms a real match — retire their other pending offers."""
    rows = pending_matches_for_user(user_id)
    cancelled = []
    for row in rows:
        if row["match_id"] == except_match_id:
            continue
        set_match_status(row["match_id"], "cancelled")
        other_id = row["user_b"] if row["user_a"] == user_id else row["user_a"]
        cancelled.append(other_id)
    return cancelled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def group_keyboard(prefix, exclude=None):
    buttons = []
    row = []
    for g in GROUPS:
        if g == exclude:
            continue
        row.append(InlineKeyboardButton(f"G{g}", callback_data=f"{prefix}:{g}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def mention(full_name, user_id, username):
    text = f'<a href="tg://user?id={user_id}">{full_name}</a>'
    if username:
        text += f" (@{username})"
    return text


def status_text(user_row):
    return (
        f"📋 Your listing:\n"
        f"Name: {user_row['full_name']}\n"
        f"Current group: G{user_row['current_group']}\n"
        f"Wants to move to: G{user_row['desired_group']}\n"
        f"Status: {'🔎 searching' if user_row['status'] == 'searching' else '✅ matched'}"
    )


# ---------------------------------------------------------------------------
# Registration conversation
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_row = get_user(update.effective_user.id)
    if user_row and user_row["status"] == "searching":
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Edit my listing", callback_data="menu:edit")],
                [InlineKeyboardButton("❌ Cancel my listing", callback_data="menu:cancel")],
            ]
        )
        await update.message.reply_text(
            status_text(user_row) + "\n\nWhat would you like to do?",
            reply_markup=keyboard,
        )
        return ConversationHandler.END

    if user_row and user_row["status"] == "matched":
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔁 Register a new swap", callback_data="menu:edit")]]
        )
        await update.message.reply_text(
            "You already completed a confirmed swap. Want to register a new one?",
            reply_markup=keyboard,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Welcome! Let's find you a group swap.\n\n"
        "First, what's your full name (so the other person recognizes you)?"
    )
    return ASK_NAME


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    user_id = query.from_user.id

    if action == "cancel":
        delete_user(user_id)
        await query.edit_message_text("❌ Your listing has been withdrawn. Send /start anytime to register again.")
        return ConversationHandler.END

    if action == "edit":
        await query.edit_message_text("Okay! What's your full name?")
        return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_name = update.message.text.strip()
    if not full_name:
        await update.message.reply_text("Please send your full name as text.")
        return ASK_NAME

    context.user_data["full_name"] = full_name
    await update.message.reply_text(
        "Which group are you currently in?", reply_markup=group_keyboard("cur")
    )
    return ASK_CURRENT


async def ask_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    group = int(query.data.split(":")[1])
    context.user_data["current_group"] = group

    await query.edit_message_text(
        f"Current group set to G{group}.\n\nWhich group do you want to move to?",
        reply_markup=group_keyboard("des", exclude=group),
    )
    return ASK_DESIRED


async def ask_desired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    desired_group = int(query.data.split(":")[1])
    current_group = context.user_data["current_group"]
    full_name = context.user_data["full_name"]

    user = query.from_user
    upsert_user(user.id, full_name, user.username, current_group, desired_group)

    await query.edit_message_text(
        f"✅ You're registered:\n{full_name}\nG{current_group} ➜ G{desired_group}\n\n"
        f"I'll notify you the moment someone wants the opposite swap.\n"
        f"Use /mystatus anytime to check, or /cancel to withdraw."
    )

    await notify_matches(context, user.id, full_name, user.username, current_group, desired_group)
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Registration cancelled. Send /start to try again.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

async def notify_matches(context, user_id, full_name, username, current_group, desired_group):
    candidates = find_reverse_candidates(user_id, current_group, desired_group)
    for cand in candidates:
        if existing_pending_match(user_id, cand["user_id"]):
            continue
        match_id = create_match(user_id, cand["user_id"])
        await send_match_notification(
            context, match_id,
            to_user_id=cand["user_id"],
            other_full_name=full_name, other_user_id=user_id, other_username=username,
            other_from_group=current_group, other_to_group=desired_group,
        )
        await send_match_notification(
            context, match_id,
            to_user_id=user_id,
            other_full_name=cand["full_name"], other_user_id=cand["user_id"], other_username=cand["username"],
            other_from_group=cand["current_group"], other_to_group=cand["desired_group"],
        )


async def send_match_notification(context, match_id, to_user_id, other_full_name, other_user_id,
                                     other_username, other_from_group, other_to_group):
    text = (
        "🎉 <b>Match found!</b>\n\n"
        f"{mention(other_full_name, other_user_id, other_username)} is in G{other_from_group} "
        f"and wants to move to G{other_to_group} — the exact opposite of your swap.\n\n"
        "Tap the name above to message them, then confirm below once you've agreed to swap."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Confirm swap", callback_data=f"confirm:{match_id}")]]
    )
    try:
        await context.bot.send_message(
            chat_id=to_user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except Exception:
        logger.exception("Could not message user %s about match %s", to_user_id, match_id)


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    match_id = int(query.data.split(":")[1])
    match_row = get_match(match_id)

    if not match_row or match_row["status"] != "pending":
        await query.answer("This match is no longer available.", show_alert=True)
        return

    user_id = query.from_user.id
    is_a = match_row["user_a"] == user_id
    if not is_a and match_row["user_b"] != user_id:
        await query.answer("This isn't your match.", show_alert=True)
        return

    set_confirmed(match_id, is_a)
    match_row = get_match(match_id)
    await query.answer("Confirmed! ✅")

    if match_row["a_confirmed"] and match_row["b_confirmed"]:
        set_match_status(match_id, "confirmed")
        set_user_status(match_row["user_a"], "matched")
        set_user_status(match_row["user_b"], "matched")

        a = get_user(match_row["user_a"]) or _ghost(match_row["user_a"])
        b = get_user(match_row["user_b"]) or _ghost(match_row["user_b"])

        for target, other in ((match_row["user_a"], b), (match_row["user_b"], a)):
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text=(
                        "✅ <b>Swap confirmed by both sides!</b>\n\n"
                        f"Go ahead and coordinate with {mention(other['full_name'], other['user_id'], other['username'])} "
                        "to finalize the group change."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Could not send confirmation to %s", target)

        for uid in (match_row["user_a"], match_row["user_b"]):
            cancelled_with = cancel_other_pending_matches(uid, match_id)
            for other_id in cancelled_with:
                try:
                    await context.bot.send_message(
                        chat_id=other_id,
                        text="ℹ️ Heads up — the person from a previous match confirmed a swap with someone else, "
                             "so that match has been closed. Send /start to look for a new one.",
                    )
                except Exception:
                    logger.exception("Could not notify %s of cancellation", other_id)
    else:
        try:
            await query.edit_message_reply_markup(
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⏳ Waiting for the other person...", callback_data="noop")]]
                )
            )
        except Exception:
            pass


def _ghost(user_id):
    return {"full_name": "this user", "user_id": user_id, "username": None}


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ---------------------------------------------------------------------------
# Status / cancel commands
# ---------------------------------------------------------------------------

async def mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_row = get_user(update.effective_user.id)
    if not user_row:
        await update.message.reply_text("You don't have an active listing. Send /start to register.")
        return
    pending = pending_matches_for_user(update.effective_user.id)
    text = status_text(user_row)
    if user_row["status"] == "searching":
        text += f"\n\nPending potential matches: {len(pending)}"
    await update.message.reply_text(text)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_row = get_user(update.effective_user.id)
    if not user_row:
        await update.message.reply_text("You don't have an active listing.")
        return
    delete_user(update.effective_user.id)
    await update.message.reply_text("❌ Your listing has been withdrawn. Send /start anytime to register again.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — register or update your swap request\n"
        "/mystatus — check your current listing\n"
        "/cancel — withdraw your listing\n"
        "/help — this message"
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_CURRENT: [CallbackQueryHandler(ask_current, pattern=r"^cur:\d+$")],
            ASK_DESIRED: [CallbackQueryHandler(ask_desired, pattern=r"^des:\d+$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^confirm:\d+$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_handler(CommandHandler("mystatus", mystatus))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
