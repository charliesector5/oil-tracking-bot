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
from datetime import datetime, timedelta

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
# token -> {user_id, user_full_name, action, days, reason, app_date, group_id}
pending_requests = {}

# track admin PMs per token to clean up when one admin handles it
# token -> [(admin_id, message_id), ...]
admin_message_refs = {}

# --- Helpers ---

MAX_REMARKS_LEN = 80  # safe cap for remarks length

def _date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def build_date_picker_keyboard(action: str) -> InlineKeyboardMarkup:
    """Quick-pick date keyboard with manual input + cancel.
    We include Today/Yesterday/Tomorrow/+7d to cover common flows.
    """
    today = _date_str(datetime.now())
    yesterday = _date_str(datetime.now() - timedelta(days=1))
    tomorrow = _date_str(datetime.now() + timedelta(days=1))
    plus7 = _date_str(datetime.now() + timedelta(days=7))

    rows = [
        [
            InlineKeyboardButton("📆 Today", callback_data=f"setdate|{today}"),
            InlineKeyboardButton("⬅️ Yesterday", callback_data=f"setdate|{yesterday}"),
        ],
        [
            InlineKeyboardButton("➡️ Tomorrow", callback_data=f"setdate|{tomorrow}"),
            InlineKeyboardButton("➕ +7 days", callback_data=f"setdate|{plus7}"),
        ],
        [
            InlineKeyboardButton("⌨️ Manual input (YYYY-MM-DD)", callback_data="manualdate"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow"),
        ]
    ]
    return InlineKeyboardMarkup(rows)

def cancel_flow(user_id: int):
    user_state.pop(user_id, None)

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
        "/help – Show this help message\n\n"
        "You can type `-quit` anytime to cancel.",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > 1 and row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            # Final Off is column 6 (0-based idx) per your new order
            # Order: Timestamp(0), Telegram ID(1), Name(2), Action(3), Current Off(4),
            # Add/Subtract(5), Final Off(6), Approved By(7), Application Date(8), Remarks(9), Holiday Off(10), Expiry(11)
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
        user_rows = [row for row in all_data if len(row) > 1 and row[1] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            # Use new order indices
            response = "\n".join([f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | AppDate {row[8]} | {row[9]}" for row in last_5])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text(
        "🕒 How many days do you want to *clock* off? (0.5 to 3, in 0.5 increments)\n"
        "Type `-quit` to cancel.",
        parse_mode="Markdown"
    )

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text(
        "🧾 How many days do you want to *claim* off? (0.5 to 3, in 0.5 increments)\n"
        "Type `-quit` to cancel.",
        parse_mode="Markdown"
    )

# --- Conversation state machine ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # support -quit from anywhere
    txt = (update.message.text or "").strip()
    uid = update.effective_user.id
    if txt.lower() == "-quit":
        if uid in user_state:
            cancel_flow(uid)
            await update.message.reply_text("✅ Cancelled. Nothing was saved.")
        else:
            await update.message.reply_text("Nothing to cancel.")
        return

    if uid not in user_state:
        return

    state = user_state[uid]
    stage = state.get("stage")

    if stage == "awaiting_days":
        try:
            days = float(txt)
            if days < 0.5 or days > 3 or (days * 10) % 5 != 0:
                raise ValueError()
            state["days"] = days
            state["stage"] = "awaiting_app_date"

            if state["action"] == "claimoff":
                prompt = (
                    "📅 *Select the Application Date* — the date you will *take* OIL (usually future).\n"
                    "Use the buttons or choose manual input.\n"
                    "Type `-quit` to cancel."
                )
            else:
                prompt = (
                    "📅 *Select the Application Date* — the date you *worked/clocked* OIL for (usually past or today).\n"
                    "Use the buttons or choose manual input.\n"
                    "Type `-quit` to cancel."
                )

            await update.message.reply_text(
                prompt, parse_mode="Markdown",
                reply_markup=build_date_picker_keyboard(state["action"])
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")

    elif stage == "awaiting_app_date_manual":
        # user types YYYY-MM-DD
        try:
            datetime.strptime(txt, "%Y-%m-%d")
            state["app_date"] = txt
            state["stage"] = "awaiting_reason"
            await update.message.reply_text(
                f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)\nType `-quit` to cancel."
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid date. Please enter in YYYY-MM-DD format.")

    elif stage == "awaiting_reason":
        reason = txt
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        state["reason"] = reason
        state["group_id"] = update.message.chat_id
        await update.message.reply_text("📩 Your request has been submitted for approval.")
        await send_approval_request(update, context, state)
        user_state.pop(uid, None)

# --- Admin approval ---
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    group_id = state["group_id"]
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > 1 and row[1] == str(user.id)]
        # Final Off column 6
        current_off = float(user_rows[-1][6]) if user_rows else 0.0
        delta = float(state["days"])
        new_off = current_off + delta if state["action"] == "clockoff" else current_off - delta

        # create token to keep callback_data short
        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": state["days"],
            "reason": state.get("reason", ""),
            "app_date": state.get("app_date", _date_str(datetime.now())),
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
                        f"📌 Application Date: {pending_requests[token]['app_date']}\n"
                        f"📝 Reason: {pending_requests[token]['reason']}\n\n"
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
    uid = query.from_user.id  # could be requester (date picking) or admin (approve/deny)

    try:
        # User-inline date selection flow
        if data.startswith("setdate|"):
            if uid not in user_state:
                await query.edit_message_text("⚠️ This flow has expired. Please start again.")
                return
            _, chosen = data.split("|", maxsplit=1)
            # validate date string
            try:
                datetime.strptime(chosen, "%Y-%m-%d")
            except ValueError:
                await query.edit_message_text("❌ Invalid date chosen. Please try again.")
                return
            user_state[uid]["app_date"] = chosen
            user_state[uid]["stage"] = "awaiting_reason"
            await query.edit_message_text(
                f"📌 Application Date set to *{chosen}*.\n"
                f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)\n"
                "Type `-quit` to cancel.",
                parse_mode="Markdown"
            )
            return

        if data == "manualdate":
            if uid not in user_state:
                await query.edit_message_text("⚠️ This flow has expired. Please start again.")
                return
            user_state[uid]["stage"] = "awaiting_app_date_manual"
            await query.edit_message_text(
                "⌨️ Please type the date in *YYYY-MM-DD* format.\nType `-quit` to cancel.",
                parse_mode="Markdown"
            )
            return

        if data.startswith("cancel|"):
            cancel_flow(uid)
            await query.edit_message_text("✅ Cancelled. Nothing was saved.")
            return

        # Admin approve/deny flow (tokenized)
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or user_id
            action = req["action"]
            days = float(req["days"])
            reason = req["reason"]
            app_date = req.get("app_date", _date_str(datetime.now()))
            group_id = int(req["group_id"])

            if action_type == "approve":
                now = datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

                all_data = worksheet.get_all_values()
                rows = [row for row in all_data if len(row) > 1 and row[1] == user_id]
                # indices by new order:
                # 0 Timestamp, 1 Telegram ID, 2 Name, 3 Action, 4 Current Off, 5 Add/Subtract,
                # 6 Final Off, 7 Approved By, 8 Application Date, 9 Remarks, 10 Holiday Off, 11 Expiry
                current_off = float(rows[-1][6]) if rows else 0.0
                final = current_off + days if action == "clockoff" else current_off - days
                add_subtract = f"+{days}" if action == "clockoff" else f"-{days}"

                worksheet.append_row([
                    timestamp,
                    user_id,
                    user_full_name,
                    "Clock Off" if action == "clockoff" else "Claim Off",
                    f"{current_off:.1f}",
                    add_subtract,
                    f"{final:.1f}",
                    query.from_user.full_name,
                    app_date,
                    reason,
                    "No",     # Holiday Off (normal OIL flow)
                    "N/A"     # Expiry
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
                        f"📌 Application Date: {app_date}\n"
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
                    text=(
                        f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n"
                        f"📌 Application Date: {app_date}\n"
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
