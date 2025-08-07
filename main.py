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
            response = "\n".join([f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | {row[9]}" for row in last_5])
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
            state["stage"] = "awaiting_application_date"
            await update.message.reply_text("📅 What's the application date? (Format: YYYY-MM-DD)")
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")

    elif state["stage"] == "awaiting_application_date":
        try:
            datetime.strptime(message, "%Y-%m-%d")
            state["application_date"] = message
            state["stage"] = "awaiting_reason"
            await update.message.reply_text("📝 What's the reason? (Max 20 characters)")
        except ValueError:
            await update.message.reply_text("❌ Invalid date. Use format YYYY-MM-DD.")

    elif state["stage"] == "awaiting_reason":
        reas
