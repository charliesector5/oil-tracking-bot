from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("RENDER_EXTERNAL_URL")  # e.g., "https://your-bot.onrender.com"

app = Flask(__name__)

application = ApplicationBuilder().token(TOKEN).build()

# Example command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello from your bot!")

application.add_handler(CommandHandler("start", start))

# Flask route to receive webhooks
@app.route(f"/{TOKEN}", methods=["POST"])
async def webhook_handler():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "OK"

# Set webhook on startup
@app.before_first_request
def setup_webhook():
    application.bot.set_webhook(f"{APP_URL}/{TOKEN}")

if __name__ == "__main__":
    app.run(port=10000, host="0.0.0.0")
