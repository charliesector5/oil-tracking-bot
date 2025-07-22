import os
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Get environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# Initialize Flask
web_app = Flask(__name__)

# Telegram webhook endpoint
@web_app.route("/webhook", methods=["POST"])
async def webhook():
    data = request.get_json(force=True)
    await application.process_update(Update.de_json(data, application.bot))
    return "ok"

# Define a simple command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Your bot is live.")

# Start the Telegram bot application
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))

# Main startup
if __name__ == "__main__":
    print("Setting webhook...")

    # Start webhook with built-in aiohttp server
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        webhook_url=f"{RENDER_EXTERNAL_URL}/webhook"
    )
