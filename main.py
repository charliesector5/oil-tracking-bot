import os
import asyncio
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

app = Flask(__name__)
application: Application = Application.builder().token(TOKEN).build()

# Define the command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Welcome to the OIL tracking bot!")

# Add the handler
application.add_handler(CommandHandler("start", start))

@app.route("/", methods=["GET"])
def home():
    return "Bot is running!"

# Set webhook only once when the app starts
async def set_webhook_once():
    await application.bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    async def run():
        await set_webhook_once()
        await application.initialize()
        await application.start()
        await application.updater.start_polling()  # This won't receive messages when using webhook, but allows graceful init
        app.run(host="0.0.0.0", port=5000)

    asyncio.run(run())
