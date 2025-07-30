import os
import asyncio
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from dotenv import load_dotenv

# Load .env
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN") or "7592365034:AAGApLgD-my9Fek0rm5S81Gr5msiEoeE9Ek"
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://oil-tracking-bot.onrender.com"

app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

# === Conversation States ===
OFF_DAYS, REMARKS = range(2)

# === Start Command ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")

# === Clock Off Conversation ===
async def clock_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How many days of off would you like to clock? (Only multiples of 0.5)",
    )
    return OFF_DAYS

async def receive_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = float(update.message.text)
        if days <= 0 or days % 0.5 != 0:
            raise ValueError("Invalid number")

        context.user_data["days"] = days
        await update.message.reply_text("Got it. Any remarks to add?")
        return REMARKS

    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number (e.g., 0.5, 1, 1.5, etc.)")
        return OFF_DAYS

async def receive_remarks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remarks = update.message.text
    days = context.user_data["days"]

    # Placeholder response - SQLite integration will be added
    await update.message.reply_text(f"✅ {days} days of off clocked with remark: “{remarks}”.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Off clocking cancelled.")
    return ConversationHandler.END

# === Handlers ===
application.add_handler(CommandHandler("start", start))

clock_off_handler = ConversationHandler(
    entry_points=[CommandHandler("clockoff", clock_off)],
    states={
        OFF_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_days)],
        REMARKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remarks)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)
application.add_handler(clock_off_handler)

# === Webhook Route ===
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)

        async def process():
            await application.initialize()
            await application.process_update(update)

        asyncio.create_task(process())
        return "ok", 200

@app.route("/", methods=["GET", "HEAD"])
def index():
    asyncio.run(set_webhook())
    return "Bot is running!", 200

# === Set Webhook ===
async def set_webhook():
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    print(f"✅ Webhook set to: {WEBHOOK_URL}/{TOKEN}")

# === Main Entrypoint ===
if __name__ == "__main__":
    asyncio.run(set_webhook())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
