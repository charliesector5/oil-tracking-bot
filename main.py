import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

app = Flask(__name__)

# Initialize the Telegram application
application = Application.builder().token(TOKEN).build()

# --- Telegram bot command handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL tracking bot!")

application.add_handler(CommandHandler("start", start))

# --- Flask Routes ---

@app.route('/')
def index():
    return 'Bot is running!'

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
async def webhook() -> str:
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"

@app.before_first_request
def setup_webhook():
    # Set the webhook once Flask app is ready
    asyncio.run(application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook/{TOKEN}"))

# --- Start the Flask app ---

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
