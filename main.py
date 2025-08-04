import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"

# Flask app
app = Flask(__name__)

# Google Sheets Setup
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(creds)
worksheet = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# Telegram Application
application = Application.builder().token(TOKEN).build()

# Telegram Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is alive!")

application.add_handler(CommandHandler("start", start))

# Webhook endpoint
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200

# Health check endpoint
@app.route("/", methods=["HEAD", "GET"])
def index():
    logger.info("Health check ping received at /")
    return "Healthy", 200

# Webhook registration and Telegram app init
@app.before_serving
async def before_serving():
    logger.info("🚀 Telegram bot initializing...")
    await application.initialize()

    webhook_url = f"https://oil-tracking-bot.onrender.com/{TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    logger.info(f"✅ Webhook set to: {webhook_url}")

# Start Flask app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
