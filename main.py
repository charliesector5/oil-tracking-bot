import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("APP_URL")  # Set in Render as your app domain

# Create Flask app
app = Flask(__name__)

# Create PTB application
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Example /start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")

application.add_handler(CommandHandler("start", start))

# Webhook endpoint to receive updates
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
async def webhook_handler():
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return "ok"

# Root route for status check
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!"

# Set webhook before first request
@app.before_serving
async def setup_webhook():
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}")

if __name__ == "__main__":
    # Use asyncio to run Flask with PTB
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
