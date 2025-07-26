from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio
import os

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # full HTTPS Render URL

app = Flask(__name__)

application = Application.builder().token(TOKEN).build()

@app.route(f"/{TOKEN}", methods=["POST"])
async def webhook_handler():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"

@app.route("/")
def index():
    return "Bot is running!"

# Set webhook at startup
@app.before_first_request
def set_webhook():
    asyncio.create_task(application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I'm alive.")

application.add_handler(CommandHandler("start", start))

if __name__ == "__main__":
    app.run(port=5000)
