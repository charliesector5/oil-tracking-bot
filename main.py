import os
import logging
import json
from flask import Flask, request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)
import asyncio

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Telegram Application
application = Application.builder().token(TOKEN).build()

# ===========================
# Telegram Command Handlers
# ===========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/start command from user: {update.effective_user.id}")
    await update.message.reply_text("Hello! Welcome to the OIL Tracking Bot.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/clockoff command from user: {update.effective_user.id}")
    await update.message.reply_text("Clocked off successfully.")

# ===========================
# Register Handlers
# ===========================

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# ===========================
# Flask Routes
# ===========================

@app.route("/")
def index():
    logger.info("Health check received at /")
    return "OIL Bot is alive!", 200

@app.route(f"/{TOKEN}", methods=["POST"])
async def webhook():
    try:
        data = request.get_data()
        json_data = json.loads(data.decode("utf-8"))
        update = Update.de_json(json_data, application.bot)
        logger.info(f"Incoming update: {update.to_dict()}")
        await application.initialize()
        await application.process_update(update)
    except Exception as e:
        logger.exception("Webhook error:")
    return "", 200
    
# ===========================
# Setup Webhook Before Launch
# ===========================

async def setup_webhook():
    from httpx import AsyncClient
    async with AsyncClient() as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            json={"url": f"{WEBHOOK_URL}/{TOKEN}"}
        )
        logger.info(f"Webhook set to {WEBHOOK_URL}/{TOKEN}")
        logger.info(f"Webhook response: {response.text}")

# ===========================
# Start Everything
# ===========================

if __name__ == "__main__":
    logger.info("Starting bot setup...")

    asyncio.run(setup_webhook())
    logger.info("Webhook setup complete. Launching Flask app.")

    app.run(debug=False, host="0.0.0.0", port=10000)
