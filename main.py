import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN") or "7592365034:AAGApLgD-my9Fek0rm5S81Gr5msiEoeE9Ek"
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://oil-tracking-bot.onrender.com"

app = Flask(__name__)

# Telegram application (v20+ async)
application = Application.builder().token(TOKEN).build()


# === Telegram Bot Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")


application.add_handler(CommandHandler("start", start))


# === Flask Endpoint ===
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Handle incoming Telegram updates via webhook."""
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.create_task(application.process_update(update))
        return "ok", 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    asyncio.run(set_webhook())  # ✅ Safe to run in synchronous context
    return "Bot is running!", 200


# === Set Webhook on Startup ===
async def set_webhook():
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    print(f"✅ Webhook set to: {WEBHOOK_URL}/{TOKEN}")


if __name__ == "__main__":
    import uvicorn
    asyncio.run(set_webhook())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
