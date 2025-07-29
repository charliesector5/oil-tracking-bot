import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://oil-tracking-bot.onrender.com"

# Initialize Flask and Telegram app
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()


# === Telegram Bot Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")


application.add_handler(CommandHandler("start", start))


# === Flask Webhook Endpoint ===
@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    """Handle Telegram updates."""
    update = Update.de_json(request.get_json(force=True), application.bot)

    async def handle():
        await application.initialize()  # ✅ Must be done before processing updates
        await application.process_update(update)

    asyncio.run(handle())
    return "ok", 200


# === Health Check ===
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200


# === Manual Webhook Setup ===
@app.route("/set-webhook", methods=["GET"])
def set_webhook_route():
    asyncio.run(set_webhook())
    return "Webhook set!", 200


# === Async Webhook Logic ===
async def set_webhook():
    await application.initialize()  # ✅ Required before setting webhook
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    print(f"✅ Webhook set to: {WEBHOOK_URL}/{TOKEN}")


# === Launch App ===
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
