import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === Load BOT TOKEN ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://your-render-service.onrender.com{WEBHOOK_PATH}"

# === Flask App for uptime or Render healthcheck ===
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return '✅ Bot is alive.'

# === Telegram Bot Application ===
application = Application.builder().token(BOT_TOKEN).build()

@flask_app.route(WEBHOOK_PATH, methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return 'OK'

# === Telegram Command Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Hello! The bot is now alive via webhook!")

application.add_handler(CommandHandler("start", start))

# === Run Flask server and setup webhook ===
def run_flask():
    flask_app.run(host="0.0.0.0", port=8000)

async def main():
    # Start Flask in a separate thread
    from threading import Thread
    Thread(target=run_flask).start()

    # Set the webhook
    await application.bot.set_webhook(url=WEBHOOK_URL)

if __name__ == '__main__':
    asyncio.run(main())
