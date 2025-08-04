import os
import logging
import asyncio
import httpx
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"
WEBHOOK_PATH = f"/{TOKEN}"
WEBHOOK_URL = f"https://oil-tracking-bot.onrender.com{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

# --- Flask App ---
app = Flask(__name__)

# --- Google Sheets ---
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(creds)
worksheet = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# --- Telegram Bot Application ---
application = Application.builder().token(TOKEN).build()

# --- Telegram Command Handler ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive and ready!")

application.add_handler(CommandHandler("start", start))

# --- Telegram Webhook Route ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook() -> tuple[str, int]:
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200

# --- Health Check Route ---
@app.route("/", methods=["GET", "HEAD"])
def health():
    logger.info("💡 Health check ping received at /")
    return "Healthy", 200

# --- Set Webhook on Startup ---
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
                logger.error(f"❌ Webhook failed: {data}")
    except Exception as e:
        logger.exception("💥 Exception during webhook setup")

# --- Telegram Application Init (non-blocking) ---
@app.before_first_request
def before_first_request():
    logger.info("🚀 Telegram bot initializing...")
    loop = asyncio.get_event_loop()
    loop.create_task(application.initialize())
    loop.create_task(set_webhook())

# --- Run Flask App (Gunicorn will use this file as entrypoint) ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
