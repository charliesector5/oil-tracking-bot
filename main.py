import os
import logging
import asyncio
import threading
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ------------------- Logging -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------- Constants -------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ BOT_TOKEN is not set in environment variables.")

WEBHOOK_URL = f"https://oil-tracking-bot.onrender.com/{TOKEN}"

# ------------------- Flask App -------------------
app = Flask(__name__)

# ------------------- Google Sheets Setup -------------------
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

try:
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        '/etc/secrets/credentials.json', scope
    )
    gc = gspread.authorize(creds)
    worksheet = gc.open("Sector 5 Charlie Oil Record").worksheet("OIL Record")
    logger.info("✅ Google Sheets initialized.")
except Exception as e:
    logger.error(f"❌ Failed to initialize Google Sheets: {e}")
    worksheet = None

# ------------------- Telegram Bot Setup -------------------
telegram_app = Application.builder().token(TOKEN).build()

# ------------------- Command Handlers -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive!")

telegram_app.add_handler(CommandHandler("start", start))

# ------------------- Flask Routes -------------------
@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "OK", 200

@app.route("/", methods=["GET", "HEAD"])
def health():
    logger.info("✅ Health check ping received")
    return "Healthy", 200

# ------------------- Webhook Initialization Thread -------------------
def setup_webhook():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(telegram_app.initialize())
        loop.run_until_complete(telegram_app.bot.set_webhook(url=WEBHOOK_URL))
        logger.info(f"✅ Webhook set to: {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"❌ Failed to set webhook: {e}")

threading.Thread(target=setup_webhook).start()

# ------------------- Gunicorn Entrypoint -------------------
application = app  # Gunicorn will look for `application`
