import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN") or "7592365034:AAGApLgD-my9Fek0rm5S81Gr5msiEoeE9Ek"
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://oil-tracking-bot.onrender.com"

# Initialize Flask and Telegram Application
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()


# === Telegram Bot Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")


application.add_handler(CommandHandler("start", start))


# === Telegram Webhook Endpoint ===
@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    """Handle Telegram updates."""
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return "ok", 200


# === Health Check Route ===
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200


# === Manual Webhook Setup Trigger ===
@app.route("/set-webhook", methods=["GET"])
def set_webhook_route():
    asyncio.run(set_webhook())
    return "Webhook set!", 200


# === Webhook Logic ===
async def set_webhook():
    webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    print(f"✅ Webhook set to: {webhook_url}")


# === Run Flask App ===
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
