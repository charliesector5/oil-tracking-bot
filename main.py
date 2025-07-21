import os
from flask import Flask, request
from threading import Thread
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load token from Render Environment Variable
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = "https://oil-tracking-bot.onrender.com/webhook"

# === Flask app for webhook and uptime check ===
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "✅ Bot is alive."

@flask_app.route('/webhook', methods=['POST'])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return 'OK'

# === Telegram bot logic ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Your bot is alive 🎉")

# === Telegram bot app setup ===
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))

# === Flask + Webhook runner ===
def run():
    flask_app.run(host="0.0.0.0", port=8080)

def main():
    Thread(target=run).start()

    # Set Telegram webhook once app starts
    import asyncio
    asyncio.run(application.bot.set_webhook(WEBHOOK_URL))

if __name__ == '__main__':
    main()
