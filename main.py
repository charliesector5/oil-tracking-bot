import os
import logging
import asyncio
import nest_asyncio
import gspread
import uuid
import calendar
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
from datetime import datetime, date as dt_date

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
# user_state[user_id] = {
#   action, stage, days, app_date, reason, group_id, calendar_year, calendar_month, calendar_message_id
# }
user_state = {}

# pending approval requests stored server-side to keep callback_data short
# token -> {user_id, user_full_name, action, days, reason, group_id, app_date}
pending_requests = {}

# track admin PMs per token to clean up when one admin handles it
# token -> [(admin_id, message_id), ...]
admin_message_refs = {}

# --- Helpers: Calendar UI ---
def _build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    """
    Builds an inline calendar keyboard for (year, month).
    callback_data patterns:
      - 'cal_pick|YYYY-MM-DD'  (pick a date)
      - 'cal_prev|YYYY-MM'     (go to previous month)
      - 'cal_next|YYYY-MM'     (go to next month)
      - 'cal_type'             (switch to manual input)
      - 'cal_cancel'           (cancel flow)
    """
    cal = calendar.Calendar(firstweekday=0)  # Monday=0? Telegram users often expect Monday or Sunday; default Monday
    month_days = cal.monthdayscalendar(year, month)

    header = f"{calendar.month_name[month]} {year}"
    keyboard = [[InlineKeyboardButton(text=header, callback_data="noop")]]

    # Weekday labels
    keyboard.append([InlineKeyboardButton(d, callback_data="noop") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])

    # Days grid
    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                day_str = f"{year:04d}-{month:02d}-{day:02d}"
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal_pick|{day_str}"))
        keyboard.append(row)

    # Navigation + manual input row
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    keyboard.append([
        InlineKeyboardButton("◀ Prev", callback_data=f"cal_prev|{prev_year:04d}-{prev_month:02d}"),
        InlineKeyboardButton("Type Manually", callback_data="cal_type"),
        InlineKeyboardButton("Next ▶", callback_data=f"cal_next|{next_year:04d}-{next_month:02d}")
    ])

    # Cancel row
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cal_cancel")])

    return InlineKeyboardMarkup(keyboard)

def _valid_yyyy_mm_dd(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

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
        "/summary – See how much OIL you have left\n"
        "/history – See your past 5 OIL logs\n"
        "/help – Show this help message",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[6]
            await update.message.reply_text(f"📊 Your current off balance: {balance} day(s).")
        else:
            await update.message.reply_text("📊 No records found.")
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if row[1] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
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
MAX_REMARKS_LEN = 80  # cap to keep admin PM safe even if we later change callback_data

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
            state["stage"] = "awaiting_date"
            # Show calendar for current month
            today = dt_date.today()
            state["calendar_year"] = today.year
            state["calendar_month"] = today.month
            sent = await update.message.reply_text(
                "📅 Select the *Application Date* from the calendar below, or tap *Type Manually* to enter a date (YYYY-MM-DD).",
                parse_mode="Markdown",
                reply_markup=_build_calendar(today.year, today.month)
            )
            state["calendar_message_id"] = sent.message_id
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")

    elif state["stage"] == "awaiting_manual_date":
        # Manual typed date after pressing "Type Manually"
        if not _valid_yyyy_mm_dd(message):
            await update.message.reply_text("❌ Invalid date format. Please use YYYY-MM-DD.")
            return
        state["app_date"] = message
        state["stage"] = "awaiting_reason"
        await update.message.reply_text(f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")

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
        user_rows = [row for row in all_data if row[1] == str(user.id)]
        current_off = float(user_rows[-1][6]) if user_rows else 0.0
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
            "app_date": state.get("app_date", ""),  # YYYY-MM-DD
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
                        f"📅 Application Date: {state.get('app_date','') or '-'}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"📊 Current Off: {current_off:.1f} day(s)\n"
                        f"📈 New Balance: {new_off:.1f} day(s)\n\n"
                        "✅ Approve or ❌ Deny?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
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
    user_id = query.from_user.id

    # Calendar navigation & selection (user flow)
    try:
        # Guard: only react to calendar callbacks if the user is in that stage
        st = user_state.get(user_id)

        if data.startswith("cal_prev|") or data.startswith("cal_next|"):
            if not st or st.get("stage") != "awaiting_date":
                return
            _, y_m = data.split("|", maxsplit=1)
            y, m = map(int, y_m.split("-"))
            st["calendar_year"], st["calendar_month"] = y, m
            await query.edit_message_reply_markup(reply_markup=_build_calendar(y, m))
            return

        if data.startswith("cal_pick|"):
            if not st or st.get("stage") != "awaiting_date":
                return
            _, picked = data.split("|", maxsplit=1)
            if not _valid_yyyy_mm_dd(picked):
                await query.edit_message_text("❌ Invalid date picked.")
                return
            st["app_date"] = picked
            st["stage"] = "awaiting_reason"
            await query.edit_message_text(f"📅 Application Date selected: *{picked}*.\n\n📝 Now enter the *reason* (max {MAX_REMARKS_LEN} chars).", parse_mode="Markdown")
            return

        if data == "cal_type":
            if not st or st.get("stage") != "awaiting_date":
                return
            st["stage"] = "awaiting_manual_date"
            await query.edit_message_text("⌨️ Please *type* the Application Date in the format: YYYY-MM-DD", parse_mode="Markdown")
            return

        if data == "cal_cancel":
            if not st:
                return
            user_state.pop(user_id, None)
            await query.edit_message_text("❎ Cancelled.")
            return

        # Admin callbacks (approve/deny via token)
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)

            # Retrieve request by token
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            req_user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or req_user_id
            action = req["action"]
            days = float(req["days"])
            reason = req["reason"]
            group_id = int(req["group_id"])
            app_date = req.get("app_date", "")

            if action_type == "approve":
                now = datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

                all_data = worksheet.get_all_values()
                rows = [row for row in all_data if row[1] == req_user_id]
                current_off = float(rows[-1][6]) if rows else 0.0
                final = current_off + days if action == "clockoff" else current_off - days
                add_subtract = f"+{days}" if action == "clockoff" else f"-{days}"

                # New order:
                # Timestamp, Telegram ID, Name, Action, Current Off,
                # Add/Subtract, Final Off, Approved By, Application Date,
                # Remarks, Holiday Off, Expiry
                worksheet.append_row([
                    timestamp, req_user_id, user_full_name,
                    "Clock Off" if action == "clockoff" else "Claim Off",
                    f"{current_off:.1f}", add_subtract, f"{final:.1f}",
                    query.from_user.full_name, app_date or "-", reason, "No", "N/A"
                ])

                # Resolve display name for group announcement
                display_name = user_full_name or req_user_id
                try:
                    member = await context.bot.get_chat_member(group_id, int(req_user_id))
                    if member and member.user:
                        if member.user.full_name:
                            display_name = member.user.full_name
                        elif member.user.username:
                            display_name = f"@{member.user.username}"
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {req_user_id} in group {group_id}: {e}")

                await query.edit_message_text("✅ Request approved and recorded.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=(
                        f"✅ {display_name}'s {action.replace('off', ' Off')} approved by {query.from_user.full_name}.\n"
                        f"📅 Days: {days}\n"
                        f"📅 Application Date: {app_date or '-'}\n"
                        f"📝 Reason: {reason}\n"
                        f"📊 Final: {final:.1f} day(s)"
                    )
                )
            else:
                # deny
                display_name = user_full_name or req_user_id
                try:
                    member = await context.bot.get_chat_member(group_id, int(req_user_id))
                    if member and member.user:
                        if member.user.full_name:
                            display_name = member.user.full_name
                        elif member.user.username:
                            display_name = f"@{member.user.username}"
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {req_user_id} in group {group_id}: {e}")

                await query.edit_message_text("❌ Request denied.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=(
                        f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n"
                        f"📅 Application Date: {app_date or '-'}\n"
                        f"📝 Reason: {reason}"
                    )
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
