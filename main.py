import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Initialize Flask and Telegram app
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()


# === Telegram Bot Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")


application.add_handler(CommandHandler("start", start))


# === Flask Webhook Endpoint ===
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Handle Telegram webhook updates."""
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return "ok", 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    return "Bot is running!", 200


# === Set Telegram Webhook at Startup ===
async def set_webhook():
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    print(f"✅ Webhook set to: {WEBHOOK_URL}/{TOKEN}")


if __name__ == "__main__":
    asyncio.run(set_webhook())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
