import os
import logging
import asyncio
import nest_asyncio
import gspread
from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
)
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Load environment and credentials ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

# --- Flask App ---
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

# --- Telegram Webhook ---
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        logger.warning("⚠️ Telegram app not yet initialized.")
        return "Bot not ready", 503

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        logger.info(f"📨 Incoming update: {update.to_dict()}")
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
        return "OK"
    except Exception as e:
        logger.exception("❌ Error processing update")
        return "Internal Server Error", 500

# --- Conversation States ---
CHOOSING_DAYS, ENTERING_REASON = range(2)

# --- Command: /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📩 /start received from {update.effective_user.id}")
    await update.message.reply_text("👋 Welcome to the Oil Tracking Bot!")

# --- Conversation: /clockoff ---
async def clockoff_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📩 /clockoff initiated by {update.effective_user.id}")
    reply_keyboard = [["0.5", "1", "1.5"], ["2", "2.5", "3"]]
    await update.message.reply_text(
        "🕒 How many days would you like to clock off? (in increments of 0.5, max 3)",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return CHOOSING_DAYS

async def receive_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    try:
        days = float(user_input)
        if days not in [0.5, 1, 1.5, 2, 2.5, 3]:
            raise ValueError
        context.user_data["days"] = days
        await update.message.reply_text("📝 What's the reason? (max 20 characters)", reply_markup=ReplyKeyboardRemove())
        return ENTERING_REASON
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number: 0.5, 1, 1.5, 2, 2.5, or 3.")
        return CHOOSING_DAYS

async def receive_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    if len(reason) > 20:
        await update.message.reply_text("❌ Reason too long. Please keep it under 20 characters.")
        return ENTERING_REASON

    user = update.effective_user
    days = context.user_data.get("days", 0)

    row = [
        datetime.now().strftime("%Y-%m-%d"),
        str(user.id),
        f"{user.first_name} {user.last_name or ''}".strip(),
        "Clock Off",
        "",  # Current off
        f"+{days}",  # Add/Subtract
        "",  # Final off
        "",  # Approved by
        reason,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]

    try:
        worksheet.append_row(row)
        logger.info(f"✅ Logged clock off for {user.id} - {days} day(s) - {reason}")
        await update.message.reply_text("✅ Your clock off has been recorded.")
    except Exception as e:
        logger.exception("❌ Failed to write to Google Sheets")
        await update.message.reply_text("❌ Failed to write to the sheet. Please try again later.")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Clock off cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --- Telegram & Sheets Initialization ---
async def init_app():
    global telegram_app, worksheet

    logger.info("📄 Connecting to Google Sheets...")
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sheet.sheet1
        logger.info("✅ Google Sheets initialized and worksheet loaded.")
    except Exception as e:
        logger.error(f"❌ Google Sheets init failed: {e}")
        return

    logger.info("⚙️ Initializing Telegram Application...")
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).get_updates_http_version("1.1").build()

    # Handlers
    telegram_app.add_handler(CommandHandler("start", start))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("clockoff", clockoff_start)],
        states={
            CHOOSING_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_days)],
            ENTERING_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reason)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    telegram_app.add_handler(conv_handler)

    logger.info("🌐 Setting Telegram webhook...")
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook has been set.")

# --- Run Everything ---
if __name__ == "__main__":
    nest_asyncio.apply()

    def run_loop():
        loop.run_forever()

    import threading
    threading.Thread(target=run_loop, daemon=True).start()

    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))

    logger.info("🟢 Starting Flask server to keep the app alive...")
    app.run(host="0.0.0.0", port=10000)
