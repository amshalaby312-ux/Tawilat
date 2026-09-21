import asyncio
import html
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

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

GREEN = "\033[1;32m"
RESET = "\033[0m"
ONLINE_BANNER = r"""
 ██████╗ ███╗   ██╗██╗     ██╗███╗   ██╗███████╗
██╔═══██╗████╗  ██║██║     ██║████╗  ██║██╔════╝
██║   ██║██╔██╗ ██║██║     ██║██╔██╗ ██║█████╗
██║   ██║██║╚██╗██║██║     ██║██║╚██╗██║██╔══╝
╚██████╔╝██║ ╚████║███████╗██║██║ ╚████║███████╗
 ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚═╝╚═╝  ╚═══╝╚══════╝
"""


def print_online_banner():
    try:
        print(f"{GREEN}{ONLINE_BANNER}{RESET}", flush=True)
    except UnicodeEncodeError:
        # terminal can't render the block characters — fall back to plain text
        print(f"{GREEN}=== ONLINE ==={RESET}", flush=True)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "swaps.db")
GROUPS = [str(n) for n in range(1, 11)]  # G1 ... G10

# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------
BACKUP_CHANNEL_ID = -1002891277206
BACKUP_INTERVAL_SECONDS = 10
BACKUP_FILENAME = "swaps_backup.json"
ADMIN_IDS = {
    int(uid) for uid in os.environ.get("ADMIN_USER_IDS", "940770584").split(",") if uid.strip()
}

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


def _column_exists(conn, table, column):
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                current_group TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'searching'  -- searching | matched
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS desired_groups (
                user_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                PRIMARY KEY (user_id, group_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
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
        # Every user ID that ever registered. Survives /reset so /broadcast can still reach them.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS known_users (
                user_id INTEGER PRIMARY KEY
            )
            """
        )

        # One-time migration from the old single-desired-group schema.
        if _column_exists(conn, "users", "desired_group"):
            rows = conn.execute("SELECT user_id, desired_group FROM users").fetchall()
            for row in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO desired_groups (user_id, group_id) VALUES (?, ?)",
                    (row["user_id"], row["desired_group"]),
                )
            conn.execute("ALTER TABLE users RENAME TO users_old")
            conn.execute(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    current_group TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'searching'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO users (user_id, full_name, username, current_group, status)
                SELECT user_id, full_name, username, current_group, status FROM users_old
                """
            )
            conn.execute("DROP TABLE users_old")
            logger.info("Migrated users.desired_group into the desired_groups table.")

        # Backfill: anyone already registered before known_users existed.
        conn.execute("INSERT OR IGNORE INTO known_users (user_id) SELECT user_id FROM users")


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_desired_groups(user_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT group_id FROM desired_groups WHERE user_id = ? ORDER BY CAST(group_id AS INTEGER)",
            (user_id,),
        ).fetchall()
    return [r["group_id"] for r in rows]


def upsert_user(user_id, full_name, username, current_group, desired_groups):
    """desired_groups: an iterable of one or more group numbers the user wants."""
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, full_name, username, current_group, status)
            VALUES (?, ?, ?, ?, 'searching')
            ON CONFLICT(user_id) DO UPDATE SET
                full_name=excluded.full_name,
                username=excluded.username,
                current_group=excluded.current_group,
                status='searching'
            """,
            (user_id, full_name, username, current_group),
        )
        conn.execute("INSERT OR IGNORE INTO known_users (user_id) VALUES (?)", (user_id,))
        conn.execute("DELETE FROM desired_groups WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO desired_groups (user_id, group_id) VALUES (?, ?)",
            [(user_id, g) for g in desired_groups],
        )


def set_user_status(user_id, status):
    with db() as conn:
        conn.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))


def delete_user(user_id):
    with db() as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


def all_searching_users():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE status = 'searching' ORDER BY CAST(current_group AS INTEGER), full_name"
        ).fetchall()


# A user "on the waitlist" is still searching AND has no pending match offer.
# Once someone finds a match they drop off the list; if the match is cancelled
# they reappear automatically. (Confirmed swaps are status='matched', so they
# are already excluded by the status check.)
_NO_PENDING_MATCH = """
    NOT EXISTS (
        SELECT 1 FROM matches m
        WHERE m.status = 'pending'
          AND (m.user_a = u.user_id OR m.user_b = u.user_id)
    )
"""


def waitlist_users():
    with db() as conn:
        return conn.execute(
            f"""
            SELECT u.* FROM users u
            WHERE u.status = 'searching' AND {_NO_PENDING_MATCH}
            ORDER BY CAST(u.current_group AS INTEGER), u.full_name
            """
        ).fetchall()


def groups_with_available_swap(user_id, current_group):
    """Groups holding at least one waitlisted user who wants `current_group` —
    i.e. picking that group as a target gives an immediate, mutual swap."""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT u.current_group
            FROM users u
            JOIN desired_groups dg ON dg.user_id = u.user_id
            WHERE u.status = 'searching'
              AND u.user_id != ?
              AND dg.group_id = ?
              AND {_NO_PENDING_MATCH}
            """,
            (user_id, current_group),
        ).fetchall()
    return {r["current_group"] for r in rows}


def all_known_user_ids():
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM known_users ORDER BY user_id").fetchall()
    return [r["user_id"] for r in rows]


def find_reverse_candidates(user_id, current_group, desired_groups):
    """Other active users whose current group is one this user wants, and who
    in turn want this user's current group — i.e. a real, mutual swap."""
    if not desired_groups:
        return []
    placeholders = ",".join("?" * len(desired_groups))
    with db() as conn:
        return conn.execute(
            f"""
            SELECT DISTINCT u.*
            FROM users u
            WHERE u.status = 'searching'
              AND u.user_id != ?
              AND u.current_group IN ({placeholders})
              AND EXISTS (
                  SELECT 1 FROM desired_groups dg
                  WHERE dg.user_id = u.user_id AND dg.group_id = ?
              )
            """,
            (user_id, *desired_groups, current_group),
        ).fetchall()


def find_reciprocal_seekers(user_id, current_group):
    """Any other searching user who wants this user's current group — even if
    their own group wasn't one this user originally asked for. This is what
    surfaces 'alternative' swaps: e.g. you want G2 (empty) but someone in G3,
    which you never listed, wants your G1 — that's a real swap worth showing."""
    with db() as conn:
        return conn.execute(
            """
            SELECT DISTINCT u.*
            FROM users u
            JOIN desired_groups dg ON dg.user_id = u.user_id
            WHERE u.status = 'searching'
              AND u.user_id != ?
              AND dg.group_id = ?
            ORDER BY CAST(u.current_group AS INTEGER), u.full_name
            """,
            (user_id, current_group),
        ).fetchall()


def users_in_groups(exclude_user_id, group_ids):
    """Other searching users currently in any of the given groups, regardless of
    whether they want ours back — used for the informational /available_swaps view."""
    if not group_ids:
        return []
    placeholders = ",".join("?" * len(group_ids))
    with db() as conn:
        return conn.execute(
            f"""
            SELECT * FROM users
            WHERE status = 'searching'
              AND user_id != ?
              AND current_group IN ({placeholders})
            ORDER BY CAST(current_group AS INTEGER), full_name
            """,
            (exclude_user_id, *group_ids),
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


def dump_state():
    """Serialize the whole DB (users, desired_groups, matches) to a plain dict."""
    with db() as conn:
        users = [dict(r) for r in conn.execute("SELECT * FROM users")]
        desired_groups = [dict(r) for r in conn.execute("SELECT * FROM desired_groups")]
        matches = [dict(r) for r in conn.execute("SELECT * FROM matches")]
        known_users = [r["user_id"] for r in conn.execute("SELECT user_id FROM known_users")]
    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "users": users,
        "desired_groups": desired_groups,
        "matches": matches,
        "known_users": known_users,
    }


def restore_state(data):
    """Wipe the DB and reload it from a dict produced by dump_state(). Returns row counts."""
    users = data.get("users", [])
    desired_groups = data.get("desired_groups", [])
    matches = data.get("matches", [])
    known_ids = {int(u["user_id"]) for u in users} | {int(k) for k in data.get("known_users", [])}

    with db() as conn:
        conn.execute("DELETE FROM matches")
        conn.execute("DELETE FROM desired_groups")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM known_users")
        conn.executemany("INSERT INTO known_users (user_id) VALUES (?)", [(k,) for k in known_ids])

        conn.executemany(
            """
            INSERT INTO users (user_id, full_name, username, current_group, status)
            VALUES (:user_id, :full_name, :username, :current_group, :status)
            """,
            users,
        )
        conn.executemany(
            "INSERT INTO desired_groups (user_id, group_id) VALUES (:user_id, :group_id)",
            desired_groups,
        )
        conn.executemany(
            """
            INSERT INTO matches (match_id, user_a, user_b, a_confirmed, b_confirmed, status)
            VALUES (:match_id, :user_a, :user_b, :a_confirmed, :b_confirmed, :status)
            """,
            matches,
        )

        # Keep the AUTOINCREMENT counter for matches ahead of any restored match_id,
        # so newly created matches after a restore can't collide with old ones.
        max_match_id = max((m["match_id"] for m in matches), default=0)
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'matches'")
        if max_match_id:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('matches', ?)", (max_match_id,))

    return {"users": len(users), "desired_groups": len(desired_groups), "matches": len(matches)}


def reset_state():
    """Clear every listing, choice, wait-list entry and match (and the match id
    counter). User IDs are kept in known_users so /broadcast still works.
    Returns how many listings/matches were cleared and how many IDs are kept."""
    with db() as conn:
        # make sure every current user's ID is remembered before their rows go
        conn.execute("INSERT OR IGNORE INTO known_users (user_id) SELECT user_id FROM users")
        n_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        n_matches = conn.execute("SELECT COUNT(*) AS c FROM matches").fetchone()["c"]
        conn.execute("DELETE FROM matches")
        conn.execute("DELETE FROM desired_groups")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'matches'")
        n_known = conn.execute("SELECT COUNT(*) AS c FROM known_users").fetchone()["c"]
    return {"users": n_users, "matches": n_matches, "known": n_known}


async def send_backup(bot):
    """Dump the DB, post it to the backup channel, and (re)pin it as the latest backup."""
    data = dump_state()
    payload = json.dumps(data, indent=2).encode("utf-8")
    caption = (
        f"🗄 Auto-backup — {len(data['users'])} users, {len(data['matches'])} matches\n"
        f"{data['exported_at']}"
    )
    message = await bot.send_document(
        chat_id=BACKUP_CHANNEL_ID,
        document=payload,
        filename=BACKUP_FILENAME,
        caption=caption,
        disable_notification=True,
    )
    try:
        await bot.unpin_chat_message(chat_id=BACKUP_CHANNEL_ID)
    except Exception:
        pass  # nothing was pinned yet, or it was already unpinned
    await bot.pin_chat_message(
        chat_id=BACKUP_CHANNEL_ID, message_id=message.message_id, disable_notification=True
    )


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_backup(context.bot)
    except Exception:
        logger.exception("Periodic backup to channel %s failed", BACKUP_CHANNEL_ID)


async def fetch_pinned_backup(bot):
    """Return the parsed JSON of the channel's currently pinned backup document, or None."""
    chat = await bot.get_chat(BACKUP_CHANNEL_ID)
    pinned = chat.pinned_message
    if not pinned or not pinned.document:
        return None
    file = await bot.get_file(pinned.document.file_id)
    raw = await file.download_as_bytearray()
    return json.loads(bytes(raw).decode("utf-8"))


async def maybe_restore_on_startup(application):
    """Called once before polling starts. Restores from the pinned backup only if the
    local DB is empty (e.g. a redeploy wiped the disk) — never overwrites live data."""
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if count > 0:
        logger.info("Found %d existing user(s) locally — skipping startup restore.", count)
        return
    logger.info("No local data found — checking channel %s for a pinned backup...", BACKUP_CHANNEL_ID)
    try:
        data = await fetch_pinned_backup(application.bot)
        if data is None:
            logger.info("No pinned backup document found. Starting with an empty database.")
            return
        counts = restore_state(data)
        logger.info("Restored from pinned backup (%s): %s", data.get("exported_at", "unknown time"), counts)
    except Exception:
        logger.exception("Startup restore failed — continuing with an empty database.")


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ You're not authorized to do that.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚠️ Yes, overwrite the live database", callback_data="restore:confirm")],
            [InlineKeyboardButton("Cancel", callback_data="restore:abort")],
        ]
    )
    await update.message.reply_text(
        "This will DELETE the current database and replace it with the last pinned "
        "backup from the channel. Are you sure?",
        reply_markup=keyboard,
    )


async def restore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    action = query.data.split(":")[1]
    if action == "abort":
        await query.answer("Cancelled.")
        await query.edit_message_text("Restore cancelled.")
        return

    await query.answer("Restoring...")
    try:
        data = await fetch_pinned_backup(context.bot)
        if data is None:
            await query.edit_message_text("⚠️ No pinned backup document found in the channel.")
            return
        counts = restore_state(data)
        await query.edit_message_text(
            f"✅ Restored from backup ({data.get('exported_at', 'unknown time')}):\n"
            f"{counts['users']} users, {counts['desired_groups']} desired-group entries, "
            f"{counts['matches']} matches."
        )
    except Exception:
        logger.exception("Manual restore failed")
        await query.edit_message_text("❌ Restore failed — check the logs.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ You're not authorized to do that.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚠️ Yes, reset everything", callback_data="reset:confirm")],
            [InlineKeyboardButton("Cancel", callback_data="reset:abort")],
        ]
    )
    await update.message.reply_text(
        "This will CLEAR every listing, choice, wait-list entry and match, for everyone. "
        "User IDs are kept so /broadcast still reaches them. "
        "The pinned channel backup will be replaced with the reset state too. Are you sure?",
        reply_markup=keyboard,
    )


async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    if query.data.split(":")[1] == "abort":
        await query.answer("Cancelled.")
        await query.edit_message_text("Reset cancelled.")
        return

    await query.answer("Resetting...")
    try:
        counts = reset_state()
    except Exception:
        logger.exception("Reset failed")
        await query.edit_message_text("❌ Reset failed — check the logs.")
        return

    # Overwrite the pinned backup right away, so a restart can't restore the old data.
    try:
        await send_backup(context.bot)
    except Exception:
        logger.exception("Post-reset backup failed")

    await query.edit_message_text(
        f"✅ Reset done — {counts['users']} listings and {counts['matches']} matches cleared. "
        f"{counts['known']} user IDs kept for /broadcast."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def group_keyboard(prefix, exclude=None):
    """Single-select keyboard (used for the 'current group' step)."""
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


def desired_group_keyboard(selected, exclude=None, swap_groups=None):
    """Multi-select toggle keyboard (used for the 'desired groups' step).
    Groups in swap_groups get a 🔄 marker: a mutual swap is available there."""
    swap_groups = swap_groups or set()
    buttons = []
    row = []
    for g in GROUPS:
        if g == exclude:
            continue
        label = f"G{g}"
        if g in swap_groups:
            label += " 🔄"
        if g in selected:
            label = "✅ " + label
        row.append(InlineKeyboardButton(label, callback_data=f"des:{g}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    done_label = "➡️ Done" if selected else "➡️ Select at least one group"
    buttons.append([InlineKeyboardButton(done_label, callback_data="des:done")])
    return InlineKeyboardMarkup(buttons)


def mention(full_name, user_id, username):
    text = f'<a href="tg://user?id={user_id}">{full_name}</a>'
    if username:
        text += f" (@{username})"
    return text


def groups_str(group_list):
    return ", ".join(f"G{g}" for g in group_list) if group_list else "—"


def status_text(user_row, desired_groups):
    return (
        f"📋 Your listing:\n"
        f"Name: {user_row['full_name']}\n"
        f"Current group: G{user_row['current_group']}\n"
        f"Wants to move to: {groups_str(desired_groups)}\n"
        f"Status: {'🔎 searching' if user_row['status'] == 'searching' else '✅ matched'}"
    )


def build_available_swaps(user_id):
    """Build an 'Available Swaps' summary for this user's current group, plus an
    inline keyboard with one 🔄 button per available swap. An available swap is any
    waiting user who wants this user's current group, whether or not their own group
    is one this user listed. Returns (text, keyboard_or_None), or (None, None) if the
    user isn't an active listing."""
    row = get_user(user_id)
    if not row or row["status"] != "searching":
        return None, None

    desired_groups = get_desired_groups(user_id)

    # Same rule as the 🔄 markers in the group picker: they want my spot, and they
    # haven't already found a match with someone else. (A pending match with *me*
    # stays visible, shown as pending.)
    swaps = [
        u for u in find_reciprocal_seekers(user_id, row["current_group"])
        if existing_pending_match(user_id, u["user_id"]) or not pending_matches_for_user(u["user_id"])
    ]
    swap_ids = {u["user_id"] for u in swaps}

    others = [u for u in users_in_groups(user_id, desired_groups) if u["user_id"] not in swap_ids]

    lines = [f"🔄 <b>Available swaps for G{row['current_group']} ➜ {groups_str(desired_groups)}</b>", ""]
    buttons = []

    if swaps:
        lines.append("🔄 <b>Available swaps</b> — they want your spot:")
        for u in swaps:
            lines.append(f"• {mention(u['full_name'], u['user_id'], u['username'])} — has G{u['current_group']}")
            if existing_pending_match(user_id, u["user_id"]):
                buttons.append(
                    [InlineKeyboardButton(
                        f"⏳ Match pending — G{u['current_group']} ({u['full_name']})",
                        callback_data="noop",
                    )]
                )
            else:
                buttons.append(
                    [InlineKeyboardButton(
                        f"🔄 Propose swap into G{u['current_group']} ({u['full_name']})",
                        callback_data=f"propose:{u['user_id']}",
                    )]
                )
        lines.append("")

    if others:
        lines.append("👀 <b>Also currently holding a group you want</b> (not seeking your spot yet):")
        for u in others:
            u_desired = groups_str(get_desired_groups(u["user_id"]))
            lines.append(
                f"• {mention(u['full_name'], u['user_id'], u['username'])} — has G{u['current_group']}, wants {u_desired}"
            )
        lines.append("")

    if not swaps and not others:
        waiting_count = len(waitlist_users())
        lines.append("there are no alternative groups currently, Sorry :/")
        lines.append(f"current number of people waiting: {waiting_count}")

    text = "\n".join(lines).strip()
    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    return text, keyboard


# ---------------------------------------------------------------------------
# Registration conversation
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_row = get_user(update.effective_user.id)
    if user_row and user_row["status"] == "searching":
        desired_groups = get_desired_groups(update.effective_user.id)
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Edit my listing", callback_data="menu:edit")],
                [InlineKeyboardButton("❌ Cancel my listing", callback_data="menu:cancel")],
                [InlineKeyboardButton("📋 Waitlist", callback_data="menu:waitlist")],
            ]
        )
        await update.message.reply_text(
            status_text(user_row, desired_groups) + "\n\nWhat would you like to do?",
            reply_markup=keyboard,
        )
        swaps_text, swaps_keyboard = build_available_swaps(update.effective_user.id)
        if swaps_text:
            await update.message.reply_text(swaps_text, parse_mode=ParseMode.HTML, reply_markup=swaps_keyboard)
        return ConversationHandler.END

    if user_row and user_row["status"] == "matched":
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Register a new swap", callback_data="menu:edit")],
                [InlineKeyboardButton("📋 Waitlist", callback_data="menu:waitlist")],
            ]
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

    if action == "waitlist":
        await send_waitlist(query.message.chat_id, context)
        return


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
    group = query.data.split(":")[1]
    context.user_data["current_group"] = group
    context.user_data["desired_groups"] = set()
    swap_groups = groups_with_available_swap(query.from_user.id, group)
    context.user_data["swap_groups"] = swap_groups

    text = (
        f"Current group set to G{group}.\n\n"
        f"Which group(s) do you want to move to? Tap as many as you like, then tap Done."
    )
    if swap_groups:
        text += "\n\n(there is a swap available in groups with this 🔄 emoji in their button)"

    await query.edit_message_text(
        text,
        reply_markup=desired_group_keyboard(set(), exclude=group, swap_groups=swap_groups),
    )
    return ASK_DESIRED


async def ask_desired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split(":")[1]
    current_group = context.user_data["current_group"]
    selected = context.user_data.setdefault("desired_groups", set())

    if data == "done":
        if not selected:
            await query.answer("Select at least one group first.", show_alert=True)
            return ASK_DESIRED

        await query.answer()
        desired_groups = sorted(selected, key=int)
        full_name = context.user_data["full_name"]
        user = query.from_user
        upsert_user(user.id, full_name, user.username, current_group, desired_groups)

        await query.edit_message_text(
            f"✅ You're registered:\n{full_name}\nG{current_group} ➜ {groups_str(desired_groups)}\n\n"
            f"I'll notify you the moment someone wants a matching swap.\n"
            f"Use /mystatus anytime to check, or /cancel to withdraw.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📋 Waitlist", callback_data="menu:waitlist")]]
            ),
        )

        await notify_matches(context, user.id, full_name, user.username, current_group, desired_groups)

        swaps_text, swaps_keyboard = build_available_swaps(user.id)
        if swaps_text:
            await context.bot.send_message(
                chat_id=user.id, text=swaps_text, parse_mode=ParseMode.HTML, reply_markup=swaps_keyboard
            )

        context.user_data.clear()
        return ConversationHandler.END

    g = data
    if g in selected:
        selected.discard(g)
    else:
        selected.add(g)
    await query.answer()
    await query.edit_message_reply_markup(
        desired_group_keyboard(
            selected, exclude=current_group, swap_groups=context.user_data.get("swap_groups", set())
        )
    )
    return ASK_DESIRED


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Registration cancelled. Send /start to try again.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

async def notify_matches(context, user_id, full_name, username, current_group, desired_groups):
    candidates = find_reverse_candidates(user_id, current_group, desired_groups)
    for cand in candidates:
        if existing_pending_match(user_id, cand["user_id"]):
            continue
        cand_desired = get_desired_groups(cand["user_id"])
        match_id = create_match(user_id, cand["user_id"])
        await send_match_notification(
            context, match_id,
            to_user_id=cand["user_id"],
            other_full_name=full_name, other_user_id=user_id, other_username=username,
            other_from_group=current_group, other_to_groups=desired_groups,
        )
        await send_match_notification(
            context, match_id,
            to_user_id=user_id,
            other_full_name=cand["full_name"], other_user_id=cand["user_id"], other_username=cand["username"],
            other_from_group=cand["current_group"], other_to_groups=cand_desired,
        )


async def send_match_notification(context, match_id, to_user_id, other_full_name, other_user_id,
                                     other_username, other_from_group, other_to_groups):
    text = (
        "🎉 <b>Match found!</b>\n\n"
        f"{mention(other_full_name, other_user_id, other_username)} is in G{other_from_group} "
        f"and wants to move to {groups_str(other_to_groups)} — that overlaps with your swap.\n\n"
        "Tap the name above to message them, then confirm below once you've agreed to swap."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm swap", callback_data=f"confirm:{match_id}")],
            [InlineKeyboardButton("❌ Cancel this match", callback_data=f"cancel:{match_id}")],
        ]
    )
    try:
        await context.bot.send_message(
            chat_id=to_user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except Exception:
        logger.exception("Could not message user %s about match %s", to_user_id, match_id)


async def propose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fired when a user taps a 🔄 button on an available swap surfaced by
    /available_swaps — creates a real pending match, same as an automatic one."""
    query = update.callback_query
    other_id = int(query.data.split(":")[1])
    user_id = query.from_user.id

    me = get_user(user_id)
    other = get_user(other_id)
    if not me or me["status"] != "searching":
        await query.answer("Your listing is no longer active.", show_alert=True)
        return
    if not other or other["status"] != "searching":
        await query.answer("That person is no longer available.", show_alert=True)
        return

    other_desired = get_desired_groups(other_id)
    if me["current_group"] not in other_desired:
        await query.answer("They no longer want your group.", show_alert=True)
        return

    if existing_pending_match(user_id, other_id):
        await query.answer("You already have a pending match with them — check your messages.", show_alert=True)
        return

    await query.answer("Proposal sent!")
    my_desired = get_desired_groups(user_id)
    match_id = create_match(user_id, other_id)

    await send_match_notification(
        context, match_id,
        to_user_id=other_id,
        other_full_name=me["full_name"], other_user_id=user_id, other_username=me["username"],
        other_from_group=me["current_group"], other_to_groups=my_desired,
    )
    await send_match_notification(
        context, match_id,
        to_user_id=user_id,
        other_full_name=other["full_name"], other_user_id=other_id, other_username=other["username"],
        other_from_group=other["current_group"], other_to_groups=other_desired,
    )


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
                    [
                        [InlineKeyboardButton("⏳ Waiting for the other person...", callback_data="noop")],
                        [InlineKeyboardButton("❌ Cancel this match", callback_data=f"cancel:{match_id}")],
                    ]
                )
            )
        except Exception:
            pass


async def cancel_match_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    match_id = int(query.data.split(":")[1])
    match_row = get_match(match_id)

    if not match_row or match_row["status"] != "pending":
        await query.answer("This match is no longer available.", show_alert=True)
        return

    user_id = query.from_user.id
    if user_id not in (match_row["user_a"], match_row["user_b"]):
        await query.answer("This isn't your match.", show_alert=True)
        return

    set_match_status(match_id, "cancelled")
    await query.answer("Match cancelled.")

    try:
        await query.edit_message_text("❌ You cancelled this match. Still listed — we'll keep looking for others.")
    except Exception:
        pass

    other_id = match_row["user_b"] if match_row["user_a"] == user_id else match_row["user_a"]
    try:
        await context.bot.send_message(
            chat_id=other_id,
            text="ℹ️ The other person cancelled this match. You're still listed — "
                 "we'll keep looking for other matches. Send /mystatus to check anytime.",
        )
    except Exception:
        logger.exception("Could not notify %s of cancellation", other_id)


def _ghost(user_id):
    return {"full_name": "this user", "user_id": user_id, "username": None}


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ---------------------------------------------------------------------------
# Status / cancel / waitlist commands
# ---------------------------------------------------------------------------

async def mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_row = get_user(update.effective_user.id)
    if not user_row:
        await update.message.reply_text("You don't have an active listing. Send /start to register.")
        return
    desired_groups = get_desired_groups(update.effective_user.id)
    pending = pending_matches_for_user(update.effective_user.id)
    text = status_text(user_row, desired_groups)
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


async def send_waitlist(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Builds and sends the current wait list to chat_id, splitting into multiple
    messages if the content would exceed Telegram's ~4096 character limit."""
    users = waitlist_users()
    if not users:
        await context.bot.send_message(chat_id=chat_id, text="No one is currently waiting for a swap. 🎉")
        return

    lines = []
    for u in users:
        desired = get_desired_groups(u["user_id"])
        lines.append(
            f"• {mention(u['full_name'], u['user_id'], u['username'])} — G{u['current_group']} ➜ {groups_str(desired)}"
        )

    header = f"📋 <b>Current wait list</b> ({len(users)} waiting)\n\n"
    chunk = [header]
    length = len(header)
    for line in lines:
        if length + len(line) + 1 > 3800:
            await context.bot.send_message(chat_id=chat_id, text="".join(chunk), parse_mode=ParseMode.HTML)
            chunk = []
            length = 0
        chunk.append(line + "\n")
        length += len(line) + 1
    if chunk:
        await context.bot.send_message(chat_id=chat_id, text="".join(chunk), parse_mode=ParseMode.HTML)


async def waitlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_waitlist(update.effective_chat.id, context)


async def available_swaps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    swaps_text, swaps_keyboard = build_available_swaps(update.effective_user.id)
    if swaps_text is None:
        await update.message.reply_text(
            "You don't have an active listing to check swaps for. Send /start to register."
        )
        return
    await update.message.reply_text(swaps_text, parse_mode=ParseMode.HTML, reply_markup=swaps_keyboard)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ You're not authorized to do that.")
        return

    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Usage: /broadcast <message>\ne.g. /broadcast Reminder — swap deadline is Friday!"
        )
        return

    body = html.escape(parts[1].strip())
    text = f"📢 <b>Announcement</b>\n\n{body}"

    user_ids = all_known_user_ids()
    if not user_ids:
        await update.message.reply_text("No registered users yet — nothing to send.")
        return

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits

    report = f"📢 Broadcast sent to {sent} user(s)."
    if failed:
        report += f" {failed} failed to deliver (likely blocked the bot)."
    await update.message.reply_text(report)


async def tell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tell <user_id> <message> — admin sends one user a direct message."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ You're not authorized to do that.")
        return

    parts = update.message.text.split(maxsplit=2)
    usage = "Usage: /tell <user_id> <message>\ne.g. /tell 123456789 Please confirm your swap"
    if len(parts) < 3 or not parts[2].strip():
        await update.message.reply_text(usage)
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await update.message.reply_text(f"❌ '{parts[1]}' isn't a valid user ID.\n\n{usage}")
        return

    try:
        # plain text on purpose (no parse_mode): the message goes out exactly as typed
        await context.bot.send_message(chat_id=target_id, text=f"رد ضروري: {parts[2].strip()}")
    except Exception as e:
        logger.warning("/tell to %s failed: %s", target_id, e)
        await update.message.reply_text(
            f"❌ Couldn't deliver to {target_id} (they may have blocked the bot or never started it)."
        )
        return
    await update.message.reply_text(f"✅ Sent to {target_id}.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_lines = (
        "\n/restore — restore the DB from the last channel backup (admin only)"
        "\n/reset — clear all listings, choices and matches; keeps user IDs (admin only)"
        "\n/broadcast <message> — message everyone who has registered (admin only)"
        "\n/tell <user_id> <message> — message one user (admin only)"
        if update.effective_user.id in ADMIN_IDS
        else ""
    )
    await update.message.reply_text(
        "/start — register or update your swap request\n"
        "/mystatus — check your current listing\n"
        "/available_swaps — see swaps available for your current group\n"
        "/waitlist — see everyone currently waiting for a swap\n"
        "/cancel — withdraw your listing\n"
        "/help — this message"
        + admin_lines
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

async def on_startup(application):
    await maybe_restore_on_startup(application)
    print_online_banner()


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_CURRENT: [CallbackQueryHandler(ask_current, pattern=r"^cur:([1-9]|10)$")],
            ASK_DESIRED: [CallbackQueryHandler(ask_desired, pattern=r"^des:([1-9]|10|done)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^confirm:\d+$"))
    app.add_handler(CallbackQueryHandler(cancel_match_callback, pattern=r"^cancel:\d+$"))
    app.add_handler(CallbackQueryHandler(propose_callback, pattern=r"^propose:\d+$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_handler(CommandHandler("mystatus", mystatus))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("waitlist", waitlist_command))
    app.add_handler(CommandHandler("available_swaps", available_swaps_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("tell", tell_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(CallbackQueryHandler(restore_callback, pattern=r"^restore:"))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern=r"^reset:"))

    app.job_queue.run_repeating(
        backup_job, interval=BACKUP_INTERVAL_SECONDS, first=BACKUP_INTERVAL_SECONDS
    )

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
