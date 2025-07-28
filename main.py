import os
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import asyncio

TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")

app = Flask(__name__)

application = ApplicationBuilder().token(TOKEN).build()


# === Command Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Your bot is up and running.")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(update.message.text)


application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))


# === Register Webhook on first request ===
@app.before_first_request
def register_webhook():
    async def set_webhook():
        webhook_url = f"{APP_URL}/{TOKEN}"
        await application.bot.set_webhook(url=webhook_url)
        print(f"✅ Webhook set to: {webhook_url}")
    
    asyncio.get_event_loop().create_task(set_webhook())


# === Flask Route for Telegram Webhook ===
@app.route(f"/{TOKEN}", methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok", 200


# === Root Route ===
@app.route("/")
def index():
    return "Bot is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
