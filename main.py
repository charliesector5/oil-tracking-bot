import os
import logging
import asyncio
import nest_asyncio
import gspread
import uuid
from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Env ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

# --- Flask ---
app = Flask(__name__)

@app.route('/')
def index():
    return "✅ Oil Tracking Bot is up."

@app.route('/health')
def health():
    return "✅ Health check passed."

# --- Globals ---
telegram_app = None
worksheet = None
loop = asyncio.new_event_loop()
executor = ThreadPoolExecutor()

# conversational state per user while filling the form
user_state = {}

# pending approval requests stored server-side to keep callback_data short
# token -> {user_id, user_full_name, action, days, reason, group_id}
pending_requests = {}

# track admin PMs per token to clean up when one admin handles it
# token -> [(admin_id, message_id), ...]
admin_message_refs = {}

# --- Constants for Sheet columns (0-based) ---
COL_TS = 0
COL_TELEGRAM_ID = 1
COL_NAME = 2
COL_ACTION = 3
COL_CURRENT = 4
COL_ADD_SUB = 5
COL_FINAL = 6
COL_APPROVED_BY = 7
COL_APP_DATE = 8
COL_REMARKS = 9
COL_HOLIDAY = 10
COL_EXPIRY = 11
COL_PH_TOTAL = 12  # may not exist on old rows

# --- Helpers ---
def _parse_float_safe(s, default=0.0):
    try:
        return float(str(s).strip())
    except Exception:
        return default

def _parse_date_ymd(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _now_sg_date():
    # Summary runs in Singapore context; date only needed for comparisons
    return datetime.utcnow().date()

def _is_ph_row(row):
    try:
        return len(row) > COL_HOLIDAY and str(row[COL_HOLIDAY]).strip().lower() == "yes"
    except Exception:
        return False

def _ph_amount(row):
    # From Add/Subtract column (e.g. "+1.0" or "-0.5")
    if len(row) <= COL_ADD_SUB:
        return 0.0
    raw = str(row[COL_ADD_SUB]).strip()
    try:
        return float(raw)
    except Exception:
        # Try to tolerate formats like "+1" or "1"
        try:
            return float(raw.replace("+", ""))
        except Exception:
            return 0.0

def _latest_final_off(rows_for_user):
    # Last row's Final Off if available, else 0.0
    if not rows_for_user:
        return 0.0
    last = rows_for_user[-1]
    if len(last) > COL_FINAL:
        return _parse_float_safe(last[COL_FINAL], 0.0)
    return 0.0

def _user_display_name_from_rows(rows_for_user):
    # Best-effort: last non-empty Name
    for row in reversed(rows_for_user):
        if len(row) > COL_NAME and str(row[COL_NAME]).strip():
            return row[COL_NAME]
    return ""

def _compute_active_ph_entries(rows_for_user):
    """
    Build active PH entries using FIFO on claims.
    Returns (entries, total_remaining)
    entries = list of dicts: {app_date, expiry, remaining, remark}
    Only non-expired entries with remaining > 0 are returned.
    FIFO consumes earliest-expiring lots first.
    """
    today = _now_sg_date()

    # Gather PH-positive lots and PH claims
    lots = []  # each: dict(app_date, expiry, remaining, remark)
    total_claim = 0.0

    for row in rows_for_user:
        if not _is_ph_row(row):
            continue

        amt = _ph_amount(row)  # +ve for clock, -ve for claim
        app_date_str = row[COL_APP_DATE] if len(row) > COL_APP_DATE else ""
        remark = row[COL_REMARKS] if len(row) > COL_REMARKS else ""
        expiry_str = row[COL_EXPIRY] if len(row) > COL_EXPIRY else ""

        app_date = _parse_date_ymd(str(app_date_str)) or _parse_date_ymd(str(row[COL_TS])[:10])  # fallback to TS date
        expiry_date = _parse_date_ymd(str(expiry_str))
        if expiry_date is None and app_date:
            # Fallback compute expiry = app_date + 365 days if missing
            expiry_date = app_date.replace(year=app_date.year + 1) if app_date else None

        if amt > 0:
            lots.append({
                "app_date": app_date,
                "expiry": expiry_date,
                "remaining": amt,
                "remark": str(remark) if remark is not None else ""
            })
        elif amt < 0:
            total_claim += (-amt)

    # Sort lots by expiry ascending (oldest expiry first)
    lots.sort(key=lambda x: (x["expiry"] or date.max, x["app_date"] or date.max))

    # Apply claims FIFO
    claim_left = total_claim
    for lot in lots:
        if claim_left <= 0:
            break
        can_consume = min(lot["remaining"], claim_left)
        lot["remaining"] -= can_consume
        claim_left -= can_consume

    # Produce active entries (non-expired, remaining > 0)
    active = []
    total_remaining = 0.0
    for lot in lots:
        exp = lot["expiry"]
        rem = round(lot["remaining"], 2)
        if rem > 0 and (exp is None or exp >= today):
            total_remaining += rem
            active.append({
                "app_date": lot["app_date"],
                "expiry": exp,
                "remaining": rem,
                "remark": lot["remark"]
            })

    return active, round(total_remaining, 2)

def _group_admin_ids(context, chat_id):
    return set([a.user.id for a in asyncio.get_event_loop().run_until_complete(context.bot.get_chat_administrators(chat_id))])

# --- Webhook ---
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        logger.warning("⚠️ Telegram app not yet initialized.")
        return "Bot not ready", 503

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        logger.info(f"📨 Incoming update: {request.get_json(force=True)}")
        future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
        future.add_done_callback(_callback)
        return "OK"
    except Exception:
        logger.exception("❌ Error processing update")
        return "Internal Server Error", 500

def _callback(fut):
    try:
        fut.result()
    except Exception:
        logger.exception("❌ Exception in handler")

# --- Commands ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n\n"
        "/clockoff – Request to clock OIL\n"
        "/claimoff – Request to claim OIL\n"
        "/summary – See how much OIL you have left (+ active PH list)\n"
        "/allsummary – Admin-only: everyone’s Off & PH at a glance\n"
        "/history – See your past 5 OIL logs\n"
        "/help – Show this help message",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()

        # Filter to this user
        rows = [row for row in all_data if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID] == str(user.id)]
        current_off = _latest_final_off(rows)

        # Build PH active entries and total
        active_entries, ph_total = _compute_active_ph_entries(rows)

        lines = [f"📊 Current Off Balance: {current_off:.1f} day(s).", f"🏖 PH Off Total: {ph_total:.1f} day(s)"]
        if active_entries:
            lines.append("🔎 Active PH Off Entries:")
            for e in active_entries:
                app_str = e["app_date"].strftime("%Y-%m-%d") if e["app_date"] else "N/A"
                exp_str = e["expiry"].strftime("%Y-%m-%d") if e["expiry"] else "N/A"
                remark = e["remark"] or "-"
                lines.append(f"• {app_str}: +{e['remaining']:.1f} (exp {exp_str}) - {remark}")
        else:
            lines.append("🔎 Active PH Off Entries: None")

        await update.message.reply_text("\n".join(lines))
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def allsummary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only, must be used in a group. Summarizes each user's current off and active PH total.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Please run /allsummary in the group.")
        return

    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admin_ids = {a.user.id for a in admins}
        if user.id not in admin_ids:
            await update.message.reply_text("❌ Only group admins can use /allsummary.")
            return

        all_data = worksheet.get_all_values()
        # Map user_id -> rows
        by_user = {}
        for row in all_data:
            if row and row[COL_TELEGRAM_ID] and row[COL_TELEGRAM_ID] != "Telegram ID":
                uid = str(row[COL_TELEGRAM_ID]).strip()
                by_user.setdefault(uid, []).append(row)

        # Build summaries
        lines = ["👥 *All Members Summary*", ""]
        for uid, rows in by_user.items():
            # Skip header-like or invalid UID entries
            if not uid.isdigit():
                continue
            off = _latest_final_off(rows)
            active_entries, ph_total = _compute_active_ph_entries(rows)
            name = _user_display_name_from_rows(rows) or uid
            lines.append(f"• {name} ({uid}): Off {off:.1f} day(s), PH {ph_total:.1f} day(s)")

        if len(lines) == 2:
            lines.append("No member rows found.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception:
        logger.exception("❌ Failed to build allsummary")
        await update.message.reply_text("❌ Could not build allsummary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            # For older sheet ordering, keep the same preview as before:
            response = "\n".join([f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | {row[8]}" for row in last_5])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text("🕒 How many days do you want to clock off? (0.5 to 3, in 0.5 increments)")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text("🧾 How many days do you want to claim off? (0.5 to 3, in 0.5 increments)")

# --- Conversation state machine ---
MAX_REMARKS_LEN = 80  # enforced (even though callback_data is now short, 80 keeps UX tidy)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message.text.strip()

    if user_id not in user_state:
        return

    state = user_state[user_id]

    if state["stage"] == "awaiting_days":
        try:
            days = float(message)
            if days < 0.5 or days > 3 or (days * 10) % 5 != 0:
                raise ValueError()
            state["days"] = days
            state["stage"] = "awaiting_reason"
            await update.message.reply_text(f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")

    elif state["stage"] == "awaiting_reason":
        reason = message
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        state["reason"] = reason
        state["group_id"] = update.message.chat_id
        await update.message.reply_text("📩 Your request has been submitted for approval.")
        await send_approval_request(update, context, state)
        user_state.pop(user_id, None)

# --- Admin approval ---
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    group_id = state["group_id"]
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID] == str(user.id)]
        current_off = _latest_final_off(user_rows)
        delta = float(state["days"])
        new_off = current_off + delta if state["action"] == "clockoff" else current_off - delta

        # create a short token to keep callback_data <= 64 bytes
        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": state["days"],
            "reason": state["reason"],
            "group_id": group_id,
        }
        admin_message_refs[token] = []

        admins = await context.bot.get_chat_administrators(group_id)
        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                msg = await context.bot.send_message(
                    chat_id=admin.user.id,
                    text=(
                        f"🆕 *{state['action'].title()} Request*\n\n"
                        f"👤 User: {user.full_name} ({user.id})\n"
                        f"📅 Days: {state['days']}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"📊 Current Off: {current_off:.1f} day(s)\n"
                        f"📈 New Balance: {new_off:.1f} day(s)\n\n"
                        "✅ Approve or ❌ Deny?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[  # callback_data kept short via token
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{token}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{token}")
                    ]])
                )
                admin_message_refs[token].append((admin.user.id, msg.message_id))
            except Exception as e:
                logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")
    except Exception:
        logger.exception("❌ Failed to fetch or notify admins")

# --- Callback handler ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)

            # Retrieve request by token
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or user_id
            action = req["action"]
            days = float(req["days"])
            reason = req["reason"]
            group_id = int(req["group_id"])

            if action_type == "approve":
                now = datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
                date_only = now.strftime("%Y-%m-%d")

                all_data = worksheet.get_all_values()
                rows = [row for row in all_data if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID] == user_id]
                current_off = _latest_final_off(rows)
                final = current_off + days if action == "clockoff" else current_off - days
                add_subtract = f"+{days}" if action == "clockoff" else f"-{days}"

                # Append in your specified order (non-PH entry here: Holiday Off = No, Expiry N/A, PH Total "No/N/A" placeholder)
                worksheet.append_row([
                    timestamp, user_id, user_full_name,
                    "Clock Off" if action == "clockoff" else "Claim Off",
                    f"{current_off:.1f}", add_subtract, f"{final:.1f}",
                    query.from_user.full_name, date_only, reason,
                    "No", "N/A", "N/A"
                ])

                # Resolve display name for group announcement
                display_name = user_full_name or user_id
                try:
                    member = await context.bot.get_chat_member(group_id, int(user_id))
                    if member and member.user:
                        if member.user.full_name:
                            display_name = member.user.full_name
                        elif member.user.username:
                            display_name = f"@{member.user.username}"
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {user_id} in group {group_id}: {e}")

                await query.edit_message_text("✅ Request approved and recorded.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=(
                        f"✅ {display_name}'s {action.replace('off', ' Off')} approved by {query.from_user.full_name}.\n"
                        f"📅 Days: {days}\n"
                        f"📝 Reason: {reason}\n"
                        f"📊 Final: {final:.1f} day(s)"
                    )
                )
            else:
                # deny
                display_name = user_full_name or user_id
                try:
                    member = await context.bot.get_chat_member(group_id, int(user_id))
                    if member and member.user:
                        if member.user.full_name:
                            display_name = member.user.full_name
                        elif member.user.username:
                            display_name = f"@{member.user.username}"
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {user_id} in group {group_id}: {e}")

                await query.edit_message_text("❌ Request denied.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
                )

            # Clean up all admin messages for this token
            if token in admin_message_refs:
                for admin_id, msg_id in admin_message_refs[token]:
                    if admin_id != query.from_user.id:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=admin_id,
                                message_id=msg_id,
                                text=f"⚠️ Request already handled by {query.from_user.full_name}.",
                            )
                        except Exception:
                            pass
                del admin_message_refs[token]

            # Remove the pending request
            pending_requests.pop(token, None)

    except Exception:
        logger.exception("❌ Failed to process callback")
        await query.edit_message_text("❌ Something went wrong.")

# --- Init ---
async def init_app():
    global telegram_app, worksheet

    logger.info("🔐 Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    worksheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    logger.info("✅ Google Sheets ready.")

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).get_updates_http_version("1.1").build()
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("summary", summary))
    telegram_app.add_handler(CommandHandler("allsummary", allsummary))
    telegram_app.add_handler(CommandHandler("history", history))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook set.")

# --- Run ---
if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    logger.info("🟢 Starting Flask...")
    app.run(host="0.0.0.0", port=10000)
