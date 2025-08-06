import os
import logging
import asyncio
import nest_asyncio
import gspread
from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

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

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        logger.warning("⚠️ Telegram app not yet initialized.")
        return "Bot not ready", 503

    try:
        update_data = request.get_json(force=True)
        logger.info(f"📨 Incoming update: {update_data}")
        update = Update.de_json(update_data, telegram_app.bot)
        fut = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)

        def _callback(fut):
            try:
                fut.result()
            except Exception as e:
                logger.exception("❌ Exception in Telegram handler task")

        fut.add_done_callback(_callback)
        return "OK"
    except Exception as e:
        logger.exception("❌ Error processing update")
        return "Internal Server Error", 500

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📩 /start received from {update.effective_user.id}")
    await update.message.reply_text("👋 Welcome to the Oil Tracking Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        now = datetime.now()
        row = [
            now.strftime("%Y-%m-%d"),                             # Date
            user.id,                                              # Telegram ID
            f"{user.first_name} {user.last_name or ''}".strip(), # Name
            "Clockoff",                                           # Action
            "0",                                                  # Current number of off (placeholder)
            "+1",                                                 # Add/Subtract (Clocking off adds)
            "0",                                                  # Final number of off (placeholder)
            "",                                                   # Approved by
            "",                                                   # Remarks
            now.strftime("%Y-%m-%d %H:%M:%S")                     # Timestamp
        ]

        worksheet.append_row(row)
        logger.info(f"📝 Logged /clockoff for {user.id} into Google Sheets.")
        await update.message.reply_text("✅ Clock off recorded in Google Sheet.")
    except Exception as e:
        logger.exception("❌ Failed to log /clockoff to Google Sheets.")
        await update.message.reply_text("⚠️ Failed to record clock off.")

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
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))

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

    # Delay init to ensure loop is alive first
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))

    logger.info("🟢 Starting Flask server to keep the app alive...")
    app.run(host="0.0.0.0", port=10000)
