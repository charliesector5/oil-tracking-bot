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
from datetime import datetime, timedelta, date as dt_date

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
    cal = calendar.Calendar(firstweekday=0)  # Monday
    month_days = cal.monthdayscalendar(year, month)

    header = f"{calendar.month_name[month]} {year}"
    keyboard = [[InlineKeyboardButton(text=header, callback_data="noop")]]

    keyboard.append([InlineKeyboardButton(d, callback_data="noop") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])

    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                day_str = f"{year:04d}-{month:02d}-{day:02d}"
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal_pick|{day_str}"))
        keyboard.append(row)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    keyboard.append([
        InlineKeyboardButton("◀ Prev", callback_data=f"cal_prev|{prev_year:04d}-{prev_month:02d}"),
        InlineKeyboardButton("Type Manually", callback_data="cal_type"),
        InlineKeyboardButton("Next ▶", callback_data=f"cal_next|{next_year:04d}-{next_month:02d}")
    ])
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(keyboard)

def _valid_yyyy_mm_dd(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

def _safe_float(s, default=0.0):
    try:
        return float(s)
    except Exception:
        return default

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
        "/clockPHOil – Clock Public Holiday OIL (PH Off)\n"
        "/claimPHOil – Claim Public Holiday OIL (PH Off)\n"
        "/summary – See how much OIL & PH OIL you have left\n"
        "/history – See your past 5 OIL logs\n"
        "/help – Show this help message",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) >= 13 and row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[6]  # Final Off
            ph_total = last_row[11] if len(last_row) >= 12 else "0"  # old order fallback
            if len(last_row) >= 13:
                ph_total = last_row[12]  # PH Off Total (new index)
            await update.message.reply_text(
                f"📊 Your balances:\n• Normal Off: {balance} day(s)\n• PH Off: {ph_total} day(s)."
            )
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
            # Show Application Date & Remarks as part of the history line
            response = "\n".join([
                f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | App: {row[8]} | {row[9]}"
                for row in last_5
            ])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text("🕒 How many days do you want to *clock off*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text("🧾 How many days do you want to *claim off*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

async def clockPHOil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockph", "stage": "awaiting_days"}
    await update.message.reply_text("🏝️ How many *PH Off* days to *clock*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

async def claimPHOil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimph", "stage": "awaiting_days"}
    await update.message.reply_text("🏝️ How many *PH Off* days to *claim*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

# --- Conversation state machine ---
MAX_REMARKS_LEN = 80

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message.text.strip()

    if user_id not in user_state:
        return

    state = user_state[user_id]
    action = state["action"]

    if state["stage"] == "awaiting_days":
        try:
            days = float(message)
            if days < 0.5 or days > 3 or (days * 10) % 5 != 0:
                raise ValueError()
            state["days"] = days
            state["stage"] = "awaiting_date"
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
        user_rows = [row for row in all_data if len(row) >= 7 and row[1] == str(user.id)]
        # Current normal off balance from Final Off column (index 6)
        current_off = _safe_float(user_rows[-1][6]) if user_rows else 0.0

        delta = float(state["days"])
        is_ph = state["action"] in ("clockph", "claimph")

        # For preview, calculate what Final Off and PH Off Total *would* be
        if is_ph:
            # PH actions do not change normal Final Off
            new_off = current_off
            # Determine last PH total (index 12 if present)
            last_ph = 0.0
            if user_rows and len(user_rows[-1]) >= 13:
                last_ph = _safe_float(user_rows[-1][12], 0.0)
            new_ph = last_ph + delta if state["action"] == "clockph" else last_ph - delta
            ph_note = f"\n🏝️ PH Off Total → {last_ph:.1f} ➜ {new_ph:.1f}"
        else:
            new_off = current_off + delta if state["action"] == "clockoff" else current_off - delta
            ph_note = ""

        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": state["days"],
            "reason": state["reason"],
            "group_id": group_id,
            "app_date": state.get("app_date", ""),
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
                        f"🆕 *{state['action'].replace('ph',' PH').title()} Request*\n\n"
                        f"👤 User: {user.full_name}\n"
                        f"📅 Days: {state['days']}\n"
                        f"📅 Application Date: {state.get('app_date','') or '-'}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"📊 Current Off: {current_off:.1f} day(s)\n"
                        f"📈 New Balance: {new_off:.1f} day(s){ph_note}\n\n"
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
    cb_from_user_id = query.from_user.id

    try:
        # Calendar navigation & selection
        st = user_state.get(cb_from_user_id)
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
            await query.edit_message_text(
                f"📅 Application Date selected: *{picked}*.\n\n📝 Now enter the *reason* (max {MAX_REMARKS_LEN} chars).",
                parse_mode="Markdown"
            )
            return

        if data == "cal_type":
            if not st or st.get("stage") != "awaiting_date":
                return
            st["stage"] = "awaiting_manual_date"
            await query.edit_message_text("⌨️ Please *type* the Application Date in the format: YYYY-MM-DD", parse_mode="Markdown")
            return

        if data == "cal_cancel":
            if st:
                user_state.pop(cb_from_user_id, None)
            await query.edit_message_text("❎ Cancelled.")
            return

        # Admin approve/deny
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            req_user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or req_user_id
            action = req["action"]  # 'clockoff' | 'claimoff' | 'clockph' | 'claimph'
            days = float(req["days"])
            reason = req["reason"]
            group_id = int(req["group_id"])
            app_date = req.get("app_date", "")

            # Fetch last balances
            all_data = worksheet.get_all_values()
            rows = [row for row in all_data if len(row) >= 7 and row[1] == req_user_id]
            current_off = _safe_float(rows[-1][6]) if rows else 0.0
            last_ph_total = 0.0
            if rows and len(rows[-1]) >= 13:
                last_ph_total = _safe_float(rows[-1][12], 0.0)

            # Compute results depending on action
            is_ph = action in ("clockph", "claimph")

            if action_type == "approve":
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if is_ph:
                    # PH does not change Final Off
                    final_off = current_off
                    add_subtract = "+0"
                    holiday_flag = "Yes"
                    # PH total update & expiry
                    new_ph_total = last_ph_total + days if action == "clockph" else last_ph_total - days
                    expiry = "N/A"
                    if action == "clockph":
                        try:
                            app_dt = datetime.strptime(app_date, "%Y-%m-%d").date()
                            expiry = (app_dt + timedelta(days=365)).strftime("%Y-%m-%d")
                        except Exception:
                            expiry = "N/A"
                else:
                    # Normal off logic
                    final_off = current_off + days if action == "clockoff" else current_off - days
                    add_subtract = f"+{days}" if action == "clockoff" else f"-{days}"
                    holiday_flag = "No"
                    expiry = "N/A"
                    new_ph_total = last_ph_total  # carry over

                # Append to sheet in new order:
                # Timestamp, Telegram ID, Name, Action, Current Off,
                # Add/Subtract, Final Off, Approved By, Application Date,
                # Remarks, Holiday Off, Expiry, PH Off Total
                worksheet.append_row([
                    timestamp,
                    req_user_id,
                    user_full_name,
                    ("Clock Off" if action in ("clockoff", "clockph") else "Claim Off") + (" (PH)" if is_ph else ""),
                    f"{current_off:.1f}",
                    add_subtract,
                    f"{final_off:.1f}",
                    query.from_user.full_name,
                    app_date or "-",
                    reason,
                    holiday_flag,
                    expiry,
                    f"{new_ph_total:.1f}"
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
                details = [
                    f"✅ {display_name}'s {(action.replace('ph',' PH').replace('off',' Off'))} approved by {query.from_user.full_name}.",
                    f"📅 Days: {days}",
                    f"📅 Application Date: {app_date or '-'}",
                    f"📝 Reason: {reason}",
                ]
                if is_ph:
                    details.append(f"🏝️ PH Off Total: {new_ph_total:.1f} day(s)")
                    if expiry != "N/A":
                        details.append(f"⏳ Expiry: {expiry}")
                    details.append(f"📊 Final Off (normal): {final_off:.1f} day(s)")
                else:
                    details.append(f"📊 Final Off: {final_off:.1f} day(s)")

                await context.bot.send_message(chat_id=group_id, text="\n".join(details))

            else:
                # Deny
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
                    text=f"❌ {display_name}'s {(action.replace('ph',' PH').replace('off',' Off'))} was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
                )

            # Clean up admin messages for this token
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

            pending_requests.pop(token, None)
            return

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
    telegram_app.add_handler(CommandHandler("clockPHOil", clockPHOil))
    telegram_app.add_handler(CommandHandler("claimPHOil", claimPHOil))
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
