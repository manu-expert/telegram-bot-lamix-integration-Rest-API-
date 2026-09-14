import asyncio
import functools
import html
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    PicklePersistence,
    filters,
)

import db
import lamix_api
from config import (
    ADMIN_IDS,
    PROXY_URL,
    REQUIRED_CHANNELS,
    TELEGRAM_BOT_TOKEN,
    TRAFFIC_CHANNEL_ID,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MIN_REQUEST_QTY = 1
MAX_REQUEST_QTY = 25
MAX_PER_RANGE_PER_DAY = 25
RANGE_CALLBACK_PREFIX = "rng_"
FORMAT_STRIP_PREFIX = "fmts_"
FORMAT_ADD_PREFIX = "fmta_"


# ---------------------------------------------------------------------------
# Forced channel membership
# ---------------------------------------------------------------------------

async def get_unjoined_channels(bot, user_id: int) -> list:
    unjoined = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(channel["chat_ref"], user_id)
            if member.status in ("left", "kicked"):
                unjoined.append(channel)
        except Exception:
            logger.warning("Could not check membership for %s", channel["chat_ref"])
            unjoined.append(channel)
    return unjoined


def build_gate_keyboard(unjoined: list) -> InlineKeyboardMarkup:
    rows = []
    for channel in unjoined:
        chat_ref = channel["chat_ref"]
        url = f"https://t.me/{chat_ref[1:]}" if chat_ref.startswith("@") else None
        if url:
            rows.append([InlineKeyboardButton(f"Join {channel['label']}", url=url)])
    rows.append([InlineKeyboardButton("I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


def build_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Link Account", callback_data="menu_link_acc"),
                InlineKeyboardButton("Status", callback_data="menu_status"),
            ],
            [InlineKeyboardButton("Help", callback_data="menu_help")],
        ]
    )


async def send_gate_or_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    target = update.callback_query.message if update.callback_query else update.message

    if not REQUIRED_CHANNELS:
        await target.reply_text(MAIN_MENU_TEXT, reply_markup=build_menu_keyboard())
        return True

    unjoined = await get_unjoined_channels(context.bot, user_id)
    if unjoined:
        channel_list = "\n".join(f"- {html.escape(c['label'])}" for c in REQUIRED_CHANNELS)
        text = (
            "🔒 <b>Join required</b>\nTo use this bot's services (/addnum, /link_acc), you must join "
            f"our official channels.\n\n{channel_list}\n\n"
            "Join all channels using the buttons below, then try again."
        )
        await target.reply_text(text, reply_markup=build_gate_keyboard(unjoined))
        return False

    await target.reply_text(MAIN_MENU_TEXT, reply_markup=build_menu_keyboard())
    return True


def require_membership(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        unjoined = await get_unjoined_channels(context.bot, update.effective_user.id)
        if unjoined:
            await send_gate_or_menu(update, context)
            return
        await handler(update, context)

    return wrapper


def access_denied_text(user_id: int) -> str:
    return (
        "❌ <b>Access denied</b>\n"
        "This bot is only available to the agent's registered clients.\n"
        f"Your Telegram ID: <code>{user_id}</code>\n"
        "Contact the admin if you believe this is a mistake."
    )


def require_allowed(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS and not db.is_allowed(user_id):
            text = access_denied_text(user_id)
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.message.reply_text(text)
            else:
                await update.message.reply_text(text)
            return
        await handler(update, context)

    return wrapper


def require_admin(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("🚫 You are not authorized to use this command.")
            return
        await handler(update, context)

    return wrapper


def require_linked_approved(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        member = db.get_member(update.effective_user.id)
        if not member or not member["linked_account_ref"]:
            await update.message.reply_text(
                "🔗 <b>No account linked!</b>\nUse /link_acc to link your account first."
            )
            return
        if not member["is_approved"]:
            await update.message.reply_text("⏳ Your linked account is pending admin approval.")
            return
        await handler(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

STATUS_BANNER = "🟢 <b>Server Status: ONLINE</b>\nYour requests will be processed superfast!\n\n"
BOT_CREDIT = "\n\n🤖 <i>Bot created by M Mohsin Ali</i>"

MAIN_MENU_TEXT = (
    STATUS_BANNER
    + "✅ <b>You're all set.</b>\n\n"
    "🔗 /link_acc - link your account (you'll be asked for your username)\n"
    "🔓 /unlink_acc - unlink your current account\n"
    "📊 /status - check your linked account status\n"
    "📱 /addnum - request numbers from available ranges\n"
    "📊 /my_cdr - show your CDR history for the last 10 minutes\n"
    "❓ /help - show all commands"
    + BOT_CREDIT
)


def help_text(user_id: int) -> str:
    lines = [
        f"🆔 Your Telegram ID: <code>{user_id}</code>",
        "",
        "📖 <b>Commands</b>",
        "/start - show welcome message",
        "🔗 /link_acc - link your account (you'll be asked for your username)",
        "🔓 /unlink_acc - unlink your current account",
        "📊 /status - check your linked account status",
        "📱 /addnum - request numbers from available ranges (needs approval)",
        "📊 /my_cdr - show your CDR history for the last 24 hours",
        "🚫 /cancel - cancel an in-progress request",
    ]
    if user_id in ADMIN_IDS:
        lines += [
            "",
            "🛠 <b>Admin commands</b>",
            "/adduser &lt;user_id&gt; &lt;username&gt; - grant a client access and issue their PIN",
            "/removeuser &lt;user_id&gt; - revoke a client's access",
            "/syncclients - pull your client roster from Lamix automatically",
            "/clients - list registered client usernames",
            "/members - list all members",
        ]
    return "\n".join(lines) + BOT_CREDIT


def display_client_username(username: str) -> str:
    """Client-facing username, without the internal CT_ prefix."""
    return username[3:] if username[:3].lower() == "ct_" else username


def status_text(user_id: int) -> str:
    member = db.get_member(user_id)
    if not member or not member["linked_account_ref"]:
        return "🔗 <b>No account linked!</b>\nUse /link_acc to link your account first."
    state = "✅ Approved" if member["is_approved"] else "⏳ Pending admin approval"
    acc = html.escape(display_client_username(member["linked_account_ref"]))
    return f"📊 <b>Status</b>\nLinked account: <code>{acc}</code>\nStatus: {state}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_member(user.id, user.username, user.full_name)
    if user.id not in ADMIN_IDS and not db.is_allowed(user.id):
        await update.message.reply_text(access_denied_text(user.id))
        return
    await send_gate_or_menu(update, context)


@require_allowed
async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_gate_or_menu(update, context)


@require_allowed
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    unjoined = await get_unjoined_channels(context.bot, update.effective_user.id)
    if unjoined:
        await send_gate_or_menu(update, context)
        return

    if query.data == "menu_status":
        await query.message.reply_text(status_text(update.effective_user.id))
    elif query.data == "menu_help":
        await query.message.reply_text(help_text(update.effective_user.id))
    elif query.data == "menu_link_acc":
        await offer_link_prompt(query.message, update.effective_user.id, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(help_text(update.effective_user.id))


def clear_flow_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset all pending conversational-flow state (link/PIN/quantity prompts).

    Without this, a stale "awaiting" flag from an abandoned flow (e.g. someone
    never finishing a PIN prompt) silently hijacks the *next* plain-text reply
    - including replies meant for an unrelated command or a fresh /link_acc.
    """
    context.user_data["awaiting_username"] = False
    context.user_data["awaiting_pin"] = False
    context.user_data["awaiting_quantity"] = False
    context.user_data.pop("pending_link_username", None)
    context.user_data.pop("pin_attempts", None)
    context.user_data.pop("selected_range", None)


async def offer_link_prompt(message, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    member = db.get_member(user_id)
    if member and member["linked_account_ref"]:
        acc = html.escape(display_client_username(member["linked_account_ref"]))
        await message.reply_text(
            f"⚠️ <b>{acc}</b> is already linked to your account.\n"
            "To change, first use /unlink_acc."
        )
        return
    clear_flow_state(context)
    context.user_data["awaiting_username"] = True
    await message.reply_text("🔗 Please send your username to link your account.")


@require_allowed
@require_membership
async def link_acc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await offer_link_prompt(update.message, update.effective_user.id, context)


MAX_PIN_ATTEMPTS = 5


async def handle_username_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_username"] = False
    raw_input = update.message.text.strip().lstrip("@")

    username = db.resolve_agent_client(raw_input) or db.resolve_agent_client(f"CT_{raw_input}")
    if not username:
        await update.message.reply_text(
            "❌ <b>Invalid username.</b>\nThis doesn't match any of the agent's registered clients.\n"
            "Use /link_acc to try again."
        )
        return

    if not db.get_client_pin(username):
        await update.message.reply_text(
            "⚠️ <b>No PIN has been set up for this account yet.</b>\n"
            "Ask the admin to grant you access with /adduser, then try /link_acc again."
        )
        return

    context.user_data["pending_link_username"] = username
    context.user_data["awaiting_pin"] = True
    context.user_data["pin_attempts"] = 0
    acc = html.escape(display_client_username(username))
    await update.message.reply_text(
        f"🔑 <b>Enter the 6-digit PIN</b> for <b>{acc}</b> (given to you by the admin).\n\n/cancel"
    )


async def handle_pin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = context.user_data.get("pending_link_username")
    entered_pin = update.message.text.strip()

    if not username:
        context.user_data["awaiting_pin"] = False
        await update.message.reply_text("⚠️ Session expired. Use /link_acc to start again.")
        return

    correct_pin = db.get_client_pin(username)
    if not correct_pin or entered_pin != correct_pin:
        context.user_data["pin_attempts"] = context.user_data.get("pin_attempts", 0) + 1
        if context.user_data["pin_attempts"] >= MAX_PIN_ATTEMPTS:
            context.user_data["awaiting_pin"] = False
            context.user_data.pop("pending_link_username", None)
            context.user_data.pop("pin_attempts", None)
            await update.message.reply_text(
                "🚫 <b>Too many incorrect attempts.</b>\nUse /link_acc to start again."
            )
            return
        remaining = MAX_PIN_ATTEMPTS - context.user_data["pin_attempts"]
        await update.message.reply_text(
            f"❌ <b>Incorrect PIN.</b> {remaining} attempt(s) left, or /cancel."
        )
        return

    context.user_data["awaiting_pin"] = False
    context.user_data.pop("pending_link_username", None)
    context.user_data.pop("pin_attempts", None)

    db.link_account(update.effective_user.id, username)
    db.set_approved(update.effective_user.id, True)
    acc = html.escape(display_client_username(username))
    await update.message.reply_text(
        f"✅ <b>Username verified:</b> {acc}\n"
        "Your account is now linked and approved.\n"
        "You can use /addnum to request numbers, or /unlink_acc to unlink."
    )


@require_allowed
@require_membership
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(status_text(update.effective_user.id))


@require_allowed
@require_membership
async def unlink_acc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if db.unlink_account(update.effective_user.id):
        await update.message.reply_text(
            "🔓 <b>Your account has been unlinked.</b>\nUse /link_acc to link a new username."
        )
    else:
        await update.message.reply_text("⚠️ You don't have a linked account.")


def generate_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


@require_admin
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /adduser &lt;telegram_user_id&gt; &lt;username&gt;")
        return

    target_id = int(context.args[0])
    raw_username = context.args[1].strip().lstrip("@")
    username = db.resolve_agent_client(raw_username) or db.resolve_agent_client(f"CT_{raw_username}")
    if not username:
        await update.message.reply_text(
            "❌ <b>Unknown username.</b> This doesn't match any client in the roster.\n"
            "Run /syncclients first, or double check the username."
        )
        return

    pin = generate_pin()
    db.set_client_pin(username, pin)
    db.add_allowed_user(target_id, update.effective_user.id)

    acc = html.escape(display_client_username(username))
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> added to the allow-list for <b>{acc}</b>.\n"
        f"🔑 <b>PIN:</b> <code>{pin}</code>\n"
        "Share this PIN with the client - they'll need it to complete /link_acc."
    )
    try:
        await context.bot.send_message(
            target_id, "✅ You've been granted access to this bot. Send /start to begin."
        )
    except Exception:
        logger.warning("Could not notify user %s of allow-list access", target_id)


@require_admin
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removeuser &lt;telegram_user_id&gt;")
        return
    target_id = int(context.args[0])
    if not db.remove_allowed_user(target_id):
        await update.message.reply_text(f"⚠️ User <code>{target_id}</code> was not on the allow-list.")
        return
    await update.message.reply_text(f"🗑 User <code>{target_id}</code> removed from the allow-list.")


@require_admin
async def list_members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members = db.list_members()
    if not members:
        await update.message.reply_text("📋 No members yet.")
        return
    lines = ["📋 <b>Members</b>"]
    for m in members:
        state = "✅ approved" if m["is_approved"] else "⏳ pending"
        acc = html.escape(m["linked_account_ref"] or "-")
        username = html.escape(m["username"] or "-")
        lines.append(f"<code>{m['user_id']}</code> @{username} | acc: {acc} | {state}")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Client-facing number requests (live Lamix assignment)
# ---------------------------------------------------------------------------


def format_range_label(range_info: dict) -> str:
    prefix = range_info.get("prefix", "")
    name = range_info.get("name", "")
    available = range_info.get("unassignedNumbers")
    avail_text = f"Available: {available}" if available is not None else "Available: unknown"
    return f"{prefix} - {name} {prefix}xxxx ({avail_text})"


@require_allowed
@require_membership
@require_linked_approved
async def addnum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ranges = await lamix_api.fetch_ranges()
    except lamix_api.LamixApiError as e:
        await update.message.reply_text(f"❌ Could not load number ranges: <code>{e.code}</code>")
        return

    available_ranges = [
        r for r in ranges if r.get("active") and r.get("unassignedNumbers", 0) > 0
    ]
    if not available_ranges:
        await update.message.reply_text("📭 No number ranges are currently available. Try again later.")
        return

    context.user_data.setdefault("ranges_by_id", {}).update({r["id"]: r for r in available_ranges})
    buttons = [
        [
            InlineKeyboardButton(
                format_range_label(r), callback_data=f"{RANGE_CALLBACK_PREFIX}{r['id']}"
            )
        ]
        for r in available_ranges
    ]
    await update.message.reply_text(
        "📱 <b>Choose a number range:</b>", reply_markup=InlineKeyboardMarkup(buttons)
    )


def remaining_range_quota(client_username: str, range_id: str) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    used = db.get_range_assignment_count(client_username, range_id, since)
    return max(0, MAX_PER_RANGE_PER_DAY - used)


@require_allowed
async def select_range_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    range_id = query.data[len(RANGE_CALLBACK_PREFIX):]
    range_info = context.user_data.get("ranges_by_id", {}).get(range_id)
    if not range_info:
        await query.message.reply_text("⚠️ This range is no longer available. Use /addnum to try again.")
        return

    member = db.get_member(update.effective_user.id)
    remaining = remaining_range_quota(member["linked_account_ref"], range_info["id"])
    if remaining <= 0:
        await query.message.reply_text(
            f"⚠️ You've already reached the {MAX_PER_RANGE_PER_DAY}-number limit for this range in the "
            "last 24 hours. Use /addnum to try a different range."
        )
        return

    context.user_data["selected_range"] = range_info
    context.user_data["awaiting_quantity"] = True
    name = html.escape(range_info["name"])
    await query.message.reply_text(
        f"<b>{range_info['prefix']} - {name} {range_info['prefix']}xxxx</b>\n"
        f"Available count: {range_info.get('unassignedNumbers', 'unknown')}\n\n"
        f"🔢 <b>Enter quantity</b>\nSend a number between {MIN_REQUEST_QTY} and {min(MAX_REQUEST_QTY, remaining)}\n"
        f"(Limit: {MAX_PER_RANGE_PER_DAY} per range every 24h - you've used {MAX_PER_RANGE_PER_DAY - remaining})\n\n"
        "/cancel"
    )


async def handle_quantity_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_quantity"] = False
    range_info = context.user_data.pop("selected_range", None)
    text = update.message.text.strip()

    if not range_info:
        await update.message.reply_text("⚠️ Session expired. Use /addnum to start again.")
        return

    member = db.get_member(update.effective_user.id)
    client_username = member["linked_account_ref"]
    remaining = remaining_range_quota(client_username, range_info["id"])

    if remaining <= 0:
        await update.message.reply_text(
            f"⚠️ You've already reached the {MAX_PER_RANGE_PER_DAY}-number limit for this range in the "
            "last 24 hours. Use /addnum to try a different range."
        )
        return

    request_cap = min(MAX_REQUEST_QTY, remaining)
    if not text.isdigit() or not (MIN_REQUEST_QTY <= int(text) <= request_cap):
        context.user_data["selected_range"] = range_info
        context.user_data["awaiting_quantity"] = True
        await update.message.reply_text(
            "❌ <b>Invalid number.</b> Please re-enter the quantity - "
            f"send a number between {MIN_REQUEST_QTY} and {request_cap}, or /cancel."
        )
        return

    quantity = int(text)
    range_name = html.escape(range_info["name"])
    range_label = f"{range_info['prefix']} - {range_name} {range_info['prefix']}xxxx"

    await update.message.reply_text(
        f"✅ <b>Order queued!</b>\nRange: {range_label}\nQty: {quantity}\n\n"
        "⏳ Processing your order now..."
    )

    try:
        free_numbers = await lamix_api.fetch_free_numbers(range_info["id"], quantity)
    except lamix_api.LamixApiError as e:
        await update.message.reply_text(f"❌ Could not fetch numbers: <code>{e.code}</code>")
        return

    if not free_numbers:
        await update.message.reply_text(
            "📭 No numbers are currently free in this range. Try again later or pick another range."
        )
        return

    try:
        result = await lamix_api.assign_numbers(client_username, free_numbers, "0")
    except lamix_api.LamixApiError as e:
        await update.message.reply_text(f"❌ Assignment failed: <code>{e.code}</code>")
        return

    fetched_count = len(free_numbers)
    skipped = result.get("skipped", 0)
    landed_count = fetched_count - skipped

    if landed_count > 0:
        db.log_range_assignment(client_username, range_info["id"], landed_count)

    header_lines = [f"✅ <b>Assigned {landed_count} of {quantity} requested number(s):</b>"]
    if fetched_count < quantity:
        header_lines.append(
            f"\n⚠️ Short by {quantity - fetched_count} - the range didn't have enough free numbers."
        )
    if skipped:
        header_lines.append(
            f"\nℹ️ Note: {skipped} of the numbers above were taken by someone else moments before "
            "assignment - double-check the list against your Lamix panel."
        )

    order_id = str(context.user_data.get("order_seq", 0) + 1)
    context.user_data["order_seq"] = int(order_id)
    context.user_data.setdefault("number_orders", {})[order_id] = {
        "numbers": free_numbers,
        "prefix": range_info["prefix"],
        "header_lines": header_lines,
    }

    await update.message.reply_text(
        render_order_message(header_lines, free_numbers),
        reply_markup=build_format_keyboard(range_info["prefix"], order_id),
    )


def render_order_message(header_lines: list, numbers: list) -> str:
    return "\n".join([*header_lines, "", *numbers])


def build_format_keyboard(prefix: str, order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"❌ Remove +{prefix}", callback_data=f"{FORMAT_STRIP_PREFIX}{order_id}"),
                InlineKeyboardButton(f"➕ Add +{prefix}", callback_data=f"{FORMAT_ADD_PREFIX}{order_id}"),
            ]
        ]
    )


@require_allowed
async def format_numbers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data.startswith(FORMAT_STRIP_PREFIX):
        strip = True
        order_id = query.data[len(FORMAT_STRIP_PREFIX):]
    else:
        strip = False
        order_id = query.data[len(FORMAT_ADD_PREFIX):]

    order = context.user_data.get("number_orders", {}).get(order_id)
    if not order:
        await query.message.reply_text("⚠️ This list has expired. Use /addnum to request new numbers.")
        return

    prefix = order["prefix"]
    if strip:
        numbers = [n[len(prefix):] if n.startswith(prefix) else n for n in order["numbers"]]
    else:
        numbers = [n if n.startswith(prefix) else f"{prefix}{n}" for n in order["numbers"]]

    await query.edit_message_text(
        render_order_message(order["header_lines"], numbers),
        reply_markup=build_format_keyboard(prefix, order_id),
    )


CDR_DIVIDER = "―" * 20
TELEGRAM_MESSAGE_LIMIT = 3500
HELD_NUMBERS_REFRESH_INTERVAL = 1200


async def reply_in_chunks(message, lines: list) -> None:
    chunk: list = []
    chunk_len = 0
    for line in lines:
        if chunk and chunk_len + len(line) + 1 > TELEGRAM_MESSAGE_LIMIT:
            await message.reply_text("\n".join(chunk))
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        await message.reply_text("\n".join(chunk))


def format_cdr_entry(r: dict) -> str:
    time_str = html.escape(str(r.get("time") or "-"))
    cli = html.escape(str(r.get("cli") or "-"))
    number = html.escape(str(r.get("number") or "-"))
    content = html.escape(str(r.get("content") or "-"))
    range_name = html.escape(str(r.get("range") or "-"))
    status = html.escape(str(r.get("status") or "-"))
    payout = html.escape(str(r.get("clientPayout") or "0.0000"))
    return (
        f"<b>{cli}</b>\n"
        f"{number}\n\n"
        f"{content}\n"
        f"{time_str}\n\n"
        f"Range: {range_name} | Status: {status} | Payout: {payout}"
    )


async def fetch_cdrs_window(since: datetime, until: datetime, depth: int = 0) -> list:
    """Fetch all agent-wide CDRs in [since, until), bisecting the window if a page comes back full."""
    from_iso = since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    to_iso = until.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    records = await lamix_api.fetch_cdrs(from_iso=from_iso, to_iso=to_iso, limit=500)
    await asyncio.sleep(1.1)

    if len(records) < 500 or depth >= 6:
        return records

    mid = since + (until - since) / 2
    first_half = await fetch_cdrs_window(since, mid, depth + 1)
    second_half = await fetch_cdrs_window(mid, until, depth + 1)
    return first_half + second_half


async def refresh_held_numbers_cache(context: ContextTypes.DEFAULT_TYPE) -> None:
    linked_clients = db.list_distinct_linked_clients()
    if not linked_clients:
        return

    try:
        all_numbers = await lamix_api.fetch_all_numbers(assigned="true")
    except lamix_api.LamixApiError as e:
        logger.warning("Held-numbers refresh failed: %s", e.code)
        return

    numbers_by_client: dict = {}
    for n in all_numbers:
        owner = (n.get("client") or "").lower()
        numbers_by_client.setdefault(owner, []).append(n["number"])

    for client_username in linked_clients:
        held = numbers_by_client.get(client_username.lower(), [])
        db.set_held_numbers_cache(client_username, held)
        logger.info("Refreshed held-numbers cache for %s: %d number(s)", client_username, len(held))


@require_allowed
@require_membership
@require_linked_approved
async def my_cdr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("cdr_in_progress"):
        await update.message.reply_text("⏳ Already fetching your CDR history - please wait for that to finish.")
        return

    context.user_data["cdr_in_progress"] = True
    try:
        member = db.get_member(update.effective_user.id)
        client_username = member["linked_account_ref"]

        cached = db.get_held_numbers_cache(client_username)
        if cached:
            held_set = set(cached["numbers"])
        else:
            await update.message.reply_text(
                "⏳ Looking up your assigned numbers - this can take a minute or two the first time..."
            )
            try:
                held_numbers = await lamix_api.fetch_client_numbers(client_username)
            except lamix_api.LamixApiError as e:
                await update.message.reply_text(f"❌ Could not fetch your numbers: <code>{e.code}</code>")
                return

            if not held_numbers:
                await update.message.reply_text(
                    "📭 You don't have any numbers assigned yet. Use /addnum to request some."
                )
                return

            held_set = {n["number"] for n in held_numbers}
            db.set_held_numbers_cache(client_username, list(held_set))

        until = datetime.now(timezone.utc)
        since = until - timedelta(minutes=10)
        await update.message.reply_text(
            f"⏳ Scanning the last 10 minutes of traffic across {len(held_set)} of your numbers..."
        )

        try:
            all_records = await fetch_cdrs_window(since, until)
        except lamix_api.LamixApiError as e:
            await update.message.reply_text(f"❌ Could not fetch CDRs: <code>{e.code}</code>")
            return

        my_records = [r for r in all_records if r.get("number") in held_set]

        if not my_records:
            await update.message.reply_text("📭 No CDR activity on your numbers in the last 10 minutes.")
            return

        my_records.sort(key=lambda r: r["time"], reverse=True)

        lines = [f"📊 <b>Your CDR history (last 10 minutes) - {len(my_records)} record(s)</b>"]
        for r in my_records:
            lines.append(CDR_DIVIDER)
            lines.append(format_cdr_entry(r))
        await reply_in_chunks(update.message, lines)
    finally:
        context.user_data["cdr_in_progress"] = False


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow_state(context)
    await update.message.reply_text("🚫 Cancelled.")


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_quantity"):
        await handle_quantity_reply(update, context)
        return
    if context.user_data.get("awaiting_pin"):
        await handle_pin_reply(update, context)
        return
    if context.user_data.get("awaiting_username"):
        await handle_username_reply(update, context)
        return
    await update.message.reply_text("❓ Unknown command. Use /help to see available commands.")


@require_admin
async def sync_clients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        records = await lamix_api.fetch_clients()
    except lamix_api.LamixApiError as e:
        await update.message.reply_text(f"❌ Sync failed: <code>{e.code}</code>")
        return

    for record in records:
        db.add_agent_client(record["username"], update.effective_user.id)

    if not records:
        await update.message.reply_text("📭 Synced 0 clients from Lamix (none found).")
        return

    lines = [
        f"{html.escape(r['username'])} ({html.escape(r.get('name') or '-')}) - "
        f"{'🔴 disabled' if r.get('disabled') else '🟢 active'}"
        for r in records
    ]
    await update.message.reply_text(
        f"✅ <b>Synced {len(records)} client(s) from Lamix:</b>\n" + "\n".join(lines)
    )


@require_admin
async def list_clients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clients = db.list_agent_clients()
    if not clients:
        await update.message.reply_text("📭 No client usernames registered yet.")
        return
    lines = ["📋 <b>Registered clients</b>"] + [html.escape(c["username"]) for c in clients]
    await update.message.reply_text("\n".join(lines))


TRAFFIC_LAST_SEEN_KEY = "traffic_last_seen"
TRAFFIC_BRAND_TAG = "CHANDIA"
TRAFFIC_FOOTER = "⚡ <i>Powered by M Mohsin Ali</i>"
CODE_RE = re.compile(r"\b\d{4,8}\b")


def mask_number(number: str, tag: str = TRAFFIC_BRAND_TAG) -> str:
    if len(number) <= 7:
        return number
    return f"{number[:4]}{tag}{number[-3:]}"


def extract_code(content: str) -> str:
    match = CODE_RE.search(content)
    return match.group(0) if match else None


def format_traffic_post(r: dict):
    number = html.escape(mask_number(str(r.get("number") or "-")))
    content_raw = str(r.get("content") or "-")
    content = html.escape(content_raw)

    text = (
        f"<b>{TRAFFIC_BRAND_TAG}</b>\n"
        f"📞 Number: {number}\n"
        f"💬 Message:\n{content}\n\n"
        f"{TRAFFIC_FOOTER}"
    )

    cli = str(r.get("cli") or "-")
    buttons = [[InlineKeyboardButton(f"📋 CLI: {cli}", copy_text=CopyTextButton(text=cli))]]
    code = extract_code(content_raw)
    if code:
        buttons.append(
            [InlineKeyboardButton(f"🔑 {TRAFFIC_BRAND_TAG} ({code})", copy_text=CopyTextButton(text=code))]
        )

    return text, InlineKeyboardMarkup(buttons)


async def post_new_traffic(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not TRAFFIC_CHANNEL_ID:
        return

    last_seen = db.get_state(TRAFFIC_LAST_SEEN_KEY)
    if not last_seen:
        # First run: establish a cursor at "now" instead of backfilling all history.
        db.set_state(TRAFFIC_LAST_SEEN_KEY, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        return

    try:
        records = await lamix_api.fetch_messages(from_iso=last_seen, limit=1000)
    except lamix_api.LamixApiError as e:
        logger.warning("Traffic poll failed: %s", e.code)
        return

    if not records:
        return

    # Records come back newest-first; post oldest-to-newest, skip anything not newer than the cursor.
    new_records = [r for r in records if r["time"] > last_seen]
    for r in reversed(new_records):
        text, markup = format_traffic_post(r)
        try:
            await context.bot.send_message(TRAFFIC_CHANNEL_ID, text, reply_markup=markup)
        except Exception:
            logger.exception("Failed to post traffic message to channel")

    if new_records:
        db.set_state(TRAFFIC_LAST_SEEN_KEY, records[0]["time"])


GENERAL_COMMANDS = [
    BotCommand("start", "Show welcome message"),
    BotCommand("link_acc", "Link your account"),
    BotCommand("unlink_acc", "Unlink your current account"),
    BotCommand("status", "Check your linked account status"),
    BotCommand("addnum", "Request numbers from available ranges"),
    BotCommand("my_cdr", "Show your CDR history (last 10 minutes)"),
    BotCommand("cancel", "Cancel an in-progress request"),
    BotCommand("help", "Show all commands"),
]

ADMIN_COMMANDS = [
    BotCommand("adduser", "Grant a client access to the bot"),
    BotCommand("removeuser", "Revoke a client's access"),
    BotCommand("syncclients", "Pull client roster from Lamix"),
    BotCommand("clients", "List registered client usernames"),
    BotCommand("members", "List all members"),
]


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(GENERAL_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                GENERAL_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            logger.warning("Could not set admin command menu for %s", admin_id)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )

    db.init_db()

    persistence = PicklePersistence(filepath="bot_persistence.pickle", update_interval=10)

    builder = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .persistence(persistence)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
    )
    if PROXY_URL:
        logger.info("Routing Telegram requests through proxy: %s", PROXY_URL)
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("link_acc", link_acc))
    application.add_handler(CommandHandler("unlink_acc", unlink_acc))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("addnum", addnum))
    application.add_handler(CommandHandler("my_cdr", my_cdr))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("members", list_members_cmd))
    application.add_handler(CommandHandler("adduser", add_user))
    application.add_handler(CommandHandler("removeuser", remove_user))
    application.add_handler(CommandHandler("syncclients", sync_clients))
    application.add_handler(CommandHandler("clients", list_clients_cmd))
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    application.add_handler(CallbackQueryHandler(select_range_callback, pattern=f"^{RANGE_CALLBACK_PREFIX}"))
    application.add_handler(
        CallbackQueryHandler(format_numbers_callback, pattern=f"^({FORMAT_STRIP_PREFIX}|{FORMAT_ADD_PREFIX})")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    if TRAFFIC_CHANNEL_ID:
        application.job_queue.run_repeating(post_new_traffic, interval=20, first=10)
        logger.info("Live traffic feed polling every 20s, posting new messages to %s", TRAFFIC_CHANNEL_ID)
    else:
        logger.info("TRAFFIC_CHANNEL_ID not set - traffic feed disabled")

    application.job_queue.run_repeating(
        refresh_held_numbers_cache, interval=HELD_NUMBERS_REFRESH_INTERVAL, first=5
    )

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
