import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://oil-tracking-bot.onrender.com{WEBHOOK_PATH}"

# Flask app
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return '✅ Bot is alive test.'

@flask_app.route(WEBHOOK_PATH, methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return 'OK'

# Telegram Application
application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Hello! The bot is now alive via webhook.")

application.add_handler(CommandHandler("start", start))

# Run Flask + Set Webhook
def run_flask():
    flask_app.run(host="0.0.0.0", port=8000)

async def main():
    from threading import Thread
    Thread(target=run_flask).start()

    # Set webhook only once during startup
    await application.bot.set_webhook(url=WEBHOOK_URL)

    # Start the bot
    await application.start()
    await application.updater.start_polling()  # Remove this if not needed

    # Keep it running
    await application.wait_until_closed()

if __name__ == "__main__":
    asyncio.run(main())
