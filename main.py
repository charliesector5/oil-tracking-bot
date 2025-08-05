import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import asyncio

# Logging
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

# Telegram /start Command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive and ready!")

application.add_handler(CommandHandler("start", start))

# Webhook Route
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook() -> tuple[str, int]:
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200

# Health Check
@app.route("/", methods=["GET", "HEAD"])
def index():
    logger.info("✅ Health check ping received at /")
    return "Healthy", 200

# Run Flask and Telegram Webhook Setup
if __name__ == "__main__":
    async def run():
        logger.info("🚀 Initializing Telegram bot and setting webhook...")
        await application.initialize()
        await application.bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"✅ Webhook set to: {WEBHOOK_URL}")
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

    asyncio.run(run())
