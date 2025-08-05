import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"
WEBHOOK_URL = f"https://oil-tracking-bot.onrender.com/{TOKEN}"

# Flask App
app = Flask(__name__)

# Google Sheets Setup
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(creds)
worksheet = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# Telegram Bot Setup
application = Application.builder().token(TOKEN).build()

# Telegram /start handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive and ready!")

application.add_handler(CommandHandler("start", start))

# Webhook endpoint for Telegram
@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200

# Health check route
@app.route("/", methods=["GET", "HEAD"])
def health_check():
    logger.info("✅ Health check ping received at /")
    return "OK", 200

# Asynchronous webhook setup
async def setup_webhook():
    await application.initialize()
    await application.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"✅ Webhook set to: {WEBHOOK_URL}")

# Run webhook setup in background
def trigger_async_setup():
    try:
        asyncio.get_event_loop().run_until_complete(setup_webhook())
    except RuntimeError:
        # If an event loop is already running (common in some WSGI setups), use a task
        asyncio.create_task(setup_webhook())

# Kick off webhook setup when this file is imported (not __main__)
trigger_async_setup()
