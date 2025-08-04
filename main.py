import os
import logging
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

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is alive!")

application.add_handler(CommandHandler("start", start))

# Webhook route
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook() -> tuple[str, int]:
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    logger.info(f"🔔 Received update: {update}")
    return "OK", 200

@app.route("/", methods=["HEAD", "GET"])
def index():
    logger.info("Health check ping received at /")
    return "Healthy", 200

# Run application
if __name__ == "__main__":
    import asyncio
    import requests

    async def run():
        await application.initialize()
        logger.info("🚀 Telegram application initialized.")

        # Set webhook
        webhook_url = f"https://oil-tracking-bot.onrender.com/{TOKEN}"
        set_webhook_url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"

        try:
            response = requests.post(set_webhook_url, json={"url": webhook_url})
            if response.ok:
                logger.info(f"✅ Webhook set successfully: {webhook_url}")
            else:
                logger.error(f"❌ Failed to set webhook: {response.text}")
        except Exception as e:
            logger.exception("💥 Exception occurred while setting webhook")

        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

    asyncio.run(run())
