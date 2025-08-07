import os
import logging
import asyncio
import nest_asyncio
import gspread
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
from datetime import datetime

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
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
user_state = {}
admin_message_refs = {}

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
            await update.message.reply_text("📅 When is the application date? (YYYY-MM-DD)")
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")

    elif state["stage"] == "awaiting_date":
        try:
            datetime.strptime(message, "%Y-%m-%d")
            state["app_date"] = message
            state["stage"] = "awaiting_reason"
            await update.message.reply_text("📝 What's the reason? (Max 20 characters)")
        except ValueError:
            await update.message.reply_text("❌ Invalid date. Please use YYYY-MM-DD format.")

    elif state["stage"] == "awaiting_reason":
        reason = message[:20]
        state["reason"] = reason
        state["group_id"] = update.message.chat_id
        await update.message.reply_text("📩 Your request has been submitted for approval.")
        await send_approval_request(update, context, state)
        user_state.pop(user_id)
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    admins = await context.bot.get_chat_administrators(state["group_id"])
    user_balance = get_current_balance(user.id)

    action = "Clock Off" if state["action"] == "clockoff" else "Claim Off"
    delta = f"+{state['days']}" if state["action"] == "clockoff" else f"-{state['days']}"
    new_balance = float(user_balance) + float(state["days"]) if state["action"] == "clockoff" else float(user_balance) - float(state["days"])

    text = (
        f"📝 *{action} Request*\n"
        f"👤 {user.full_name}\n"
        f"📅 Days: {state['days']}\n"
        f"📆 Application Date: {state['app_date']}\n"
        f"📝 Reason: {state['reason']}\n"
        f"📊 Final: {new_balance:.1f} day(s)"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{user.id}|{state['days']}|{state['reason']}|{state['action']}|{state['app_date']}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject|{user.id}")
    ]])

    for admin in admins:
        try:
            msg = await context.bot.send_message(chat_id=admin.user.id, text=text, parse_mode="Markdown", reply_markup=keyboard)
            admin_message_refs[msg.message_id] = user.id
        except Exception as e:
            logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")

def get_current_balance(user_id):
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if row[1] == str(user_id)]
        return float(user_rows[-1][6]) if user_rows else 0.0
    except:
        return 0.0

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')

    if data[0] == "reject":
        await query.edit_message_text("❌ Request rejected.")
        return

    user_id, days, reason, action, app_date = data[1], float(data[2]), data[3], data[4], data[5]
    approver = query.from_user.full_name

    current = get_current_balance(user_id)
    new_balance = current + days if action == "clockoff" else current - days
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Log to Google Sheet
    worksheet.append_row([
        timestamp,
        str(user_id),
        context.bot.get_chat_member(update.effective_chat.id, int(user_id)).user.full_name,
        action,
        f"{current:.1f}",
        f"{'+' if action == 'clockoff' else '-'}{days}",
        f"{new_balance:.1f}",
        approver,
        app_date,
        reason
    ])

    # Confirmation Message
    await query.edit_message_text(
        f"✅ {context.bot.get_chat_member(update.effective_chat.id, int(user_id)).user.full_name}'s {action} approved by {approver}.\n"
        f"📅 Days: {days}\n"
        f"📝 Reason: {reason}\n"
        f"📊 Final: {new_balance:.1f} day(s)"
    )

def init_app():
    global telegram_app, worksheet
    nest_asyncio.apply()

    # Telegram Bot
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("summary", summary))
    telegram_app.add_handler(CommandHandler("history", history))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    # Google Sheets
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
    gc = gspread.authorize(creds)
    worksheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

    # Webhook
    telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Bot is running and webhook is set.")

# --- Run ---
if __name__ == '__main__':
    init_app()
    from threading import Thread
    Thread(target=app.run, kwargs={"host": "0.0.0.0", "port": 10000}).start()
    loop.run_forever()
