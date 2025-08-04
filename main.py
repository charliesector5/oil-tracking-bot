import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import asyncio
import httpx

# --- Logging setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"
WEBHOOK_URL = f"https://oil-tracking-bot.onrender.com/{TOKEN}"
PORT = int(os.getenv("PORT", 10000))

# --- Flask app ---
app = Flask(__name__)

# --- Google Sheets setup ---
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(creds)
worksheet = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# --- Telegram bot application ---
application = Application.builder().token(TOKEN).build()

# --- Command handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is alive!")

application.add_handler(CommandHandler("start", start))

# --- Flask route for Telegram webhook ---
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook() -> tuple[str, int]:
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.create_task(application.process_update(update))
    return "OK", 200

# --- Health check route ---
@app.route("/", methods=["GET", "HEAD"])
def index():
    logger.info("Health check ping received at /")
    return "Healthy", 200

# --- Set webhook via Telegram API ---
async def set_webhook():
    url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    payload = {"url": WEBHOOK_URL}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            data = response.json()
            if data.get("ok"):
                logger.info(f"✅ Webhook set successfully: {WEBHOOK_URL}")
            else:
                logger.error(f"❌ Failed to set webhook: {data}")
    except Exception as e:
        logger.exception("💥 Exception occurred while setting webhook")

# --- Main runner ---
async def run():
    logger.info("🚀 Telegram application initialized.")
    await application.initialize()
    await set_webhook()
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    asyncio.run(run())
