import os
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = f"/{TOKEN}"
WEBHOOK_URL = f"https://https://oil-tracking-bot.onrender.com/{TOKEN}"

app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

@app.post(WEBHOOK_PATH)
async def webhook() -> str:
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Your bot is alive.")

application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))

if __name__ == "__main__":
    # Set webhook on startup
    import asyncio
    async def run():
        await application.bot.set_webhook(WEBHOOK_URL)
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

    asyncio.run(run())
