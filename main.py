from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import threading
import asyncio
import os

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://oil-tracking-bot.onrender.com

app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

@app.route(f"/{TOKEN}", methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"

@app.route("/")
def index():
    return "Bot is running!"

def set_webhook():
    async def _set():
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    asyncio.run(_set())

# Start webhook setup in a background thread
@app.before_first_request
def activate_webhook():
    threading.Thread(target=set_webhook).start()

# Example command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I'm alive.")

application.add_handler(CommandHandler("start", start))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
