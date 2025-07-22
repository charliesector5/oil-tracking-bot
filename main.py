import os
import logging
import asyncio

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Setup logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # e.g. https://your-bot.onrender.com

# Create Flask app
flask_app = Flask(__name__)

# Create Telegram application instance
application = Application.builder().token(TOKEN).build()


# Flask index route to prevent 404
@flask_app.route("/", methods=["GET", "HEAD"])
def index():
    return "Bot is running.", 200


# Webhook handler
@flask_app.post(f"/{TOKEN}")
async def webhook() -> str:
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        await application.process_update(update)
    return "OK", 200


# Telegram command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hello! I am alive.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Help is on the way!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(update.message.text)


# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))


# Async entry point
async def main():
    # Set webhook URL
    await application.bot.set_webhook(url=f"{APP_URL}/{TOKEN}")
    print(f"Webhook set to {APP_URL}/{TOKEN}")


if __name__ == "__main__":
    # Run the async main() once to set webhook
    asyncio.run(main())

    # Run Flask app
    flask_app.run(host="0.0.0.0", port=10000)
