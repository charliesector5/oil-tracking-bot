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
    except Exception as e:
        logger.exception("❌ Error processing update")
        return "Internal Server Error", 500

def _callback(fut):
    try:
        fut.result()
    except Exception as e:
        logger.exception("❌ Exception in Telegram handler task")

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📩 /start received from {update.effective_user.id}")
    await update.message.reply_text("👋 Welcome to the Oil Tracking Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"📩 /clockoff received from {user.id} ({user.full_name})")

    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        current_data = worksheet.get_all_values()
        user_rows = [row for row in current_data if row[1] == str(user.id)]

        if user_rows:
            last_row = user_rows[-1]
            current_off = float(last_row[6])  # Final number of off
        else:
            current_off = 0.0

        updated_off = current_off + 1.0

        worksheet.append_row([
            date,
            str(user.id),
            user.full_name,
            "Clock Off",
            f"{current_off:.1f}",
            "+1",
            f"{updated_off:.1f}",
            "System",
            "Clock off recorded",
            timestamp
        ])

        await update.message.reply_text(f"✅ Clock off recorded.\n📊 You now have {updated_off:.1f} off(s).")
        logger.info(f"✅ /clockoff recorded for {user.full_name} (Total: {updated_off})")

    except Exception as e:
        logger.exception("❌ Failed to record clock off")
        await update.message.reply_text("❌ Failed to record your clock off. Please try again later.")

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

    await telegram_app.initialize()
    logger.info("✅ Telegram Application initialized.")

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
