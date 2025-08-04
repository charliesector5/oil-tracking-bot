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

# Environment variables
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"

# Flask app
app = Flask(__name__)

# Google Sheets setup
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(creds)
worksheet = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# Telegram bot setup
application = Application.builder().token(TOKEN).build()

# /start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive and ready!")

application.add_handler(CommandHandler("start", start))

# Webhook route
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook() -> tuple[str, int]:
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200

# Health check
@app.route("/", methods=["GET", "HEAD"])
def index():
    logger.info("✅ Health check ping received at /")
    return "Healthy", 200

# Auto-initialize and set webhook on startup
@app.before_serving
async def before_serving():
    logger.info("🚀 Initializing Telegram bot...")
    await application.initialize()
    webhook_url = f"https://oil-tracking-bot.onrender.com/{TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    logger.info(f"✅ Webhook set to: {webhook_url}")

# Run Flask app with Gunicorn
if __name__ == "__main__":
    import asyncio
    asyncio.run(application.initialize())
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
