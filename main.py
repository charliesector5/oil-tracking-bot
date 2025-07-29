import os
import json
import asyncio
from datetime import datetime
from flask import Flask, request
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, ConversationHandler
)
from dotenv import load_dotenv
import pytz

# === Load environment ===
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# === Flask setup ===
app = Flask(__name__)

# === Telegram App ===
application = Application.builder().token(TOKEN).build()

# === Clockoff conversation states ===
DAYS, REMARKS = range(2)

# === JSON logging helper ===
def save_entry(data, file="clockoff_log.json"):
    if not os.path.exists(file):
        with open(file, "w") as f:
            json.dump([], f)
    with open(file, "r+") as f:
        log = json.load(f)
        log.append(data)
        f.seek(0)
        json.dump(log, f, indent=2)

# === /clockoff entry ===
async def clockoff_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ You're clocking off!\n"
        "How many days of off would you like to clock in?\n"
        "*(Reply in multiples of 0.5 — e.g., 0.5, 1, 1.5)*"
    )
    return DAYS

async def clockoff_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = float(update.message.text)
        if days <= 0 or days * 2 % 1 != 0:
            raise ValueError()
        context.user_data["off_days"] = days
        await update.message.reply_text("Please enter your remarks for this Off entry.")
        return REMARKS
    except ValueError:
        await update.message.reply_text("❌ Invalid input. Please enter a valid number in 0.5 steps.")
        return DAYS

async def clockoff_remarks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remarks = update.message.text
    days = context.user_data["off_days"]
    user = update.effective_user

    # Get SG time
    now = datetime.now(pytz.timezone("Asia/Singapore"))
    timestamp = now.strftime("%Y-%m-%d %H:%M")

    entry = {
        "user_id": user.id,
        "name": user.full_name,
        "off_days": days,
        "remarks": remarks,
        "timestamp": timestamp
    }

    save_entry(entry)

    await update.message.reply_text(
        f"📝 Off clocked successfully!\n"
        f"• Days: {days}\n"
        f"• Remarks: {remarks}\n"
        f"• Time: {timestamp}",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def clockoff_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Clockoff cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# === Register handlers ===
application.add_handler(CommandHandler("start", lambda update, ctx: update.message.reply_text("Hello! Bot is working.")))

clockoff_conv = ConversationHandler(
    entry_points=[CommandHandler("clockoff", clockoff_start)],
    states={
        DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, clockoff_days)],
        REMARKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, clockoff_remarks)],
    },
    fallbacks=[CommandHandler("cancel", clockoff_cancel)],
)
application.add_handler(clockoff_conv)

# === Webhook endpoints ===
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.create_task(application.process_update(update))
    return "ok", 200

@app.route("/", methods=["GET", "HEAD"])
def index():
    asyncio.run(set_webhook())
    return "Bot is running!", 200

# === Set webhook ===
async def set_webhook():
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    print(f"✅ Webhook set to: {WEBHOOK_URL}/{TOKEN}")

# === Start app ===
if __name__ == "__main__":
    asyncio.run(set_webhook())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
