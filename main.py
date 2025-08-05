import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = f"https://oil-tracking-bot.onrender.com/{TOKEN}"

# Flask app
app = Flask(__name__)

# Google Sheets
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(creds)
worksheet = gc.open("Sector 5 Charlie Oil Record").worksheet("OIL Record")

# Telegram Application
telegram_app = Application.builder().token(TOKEN).build()

# Handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive!")

telegram_app.add_handler(CommandHandler("start", start))

# Webhook endpoint
@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "OK", 200

@app.route("/", methods=["GET", "HEAD"])
def health():
    logger.info("✅ Health check ping received")
    return "Healthy", 200

# Initialization inside Flask start
@app.before_first_request
def init_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.initialize())
    loop.run_until_complete(telegram_app.bot.set_webhook(url=WEBHOOK_URL))
    logger.info(f"✅ Webhook set to: {WEBHOOK_URL}")

# Expose Flask app as WSGI variable
