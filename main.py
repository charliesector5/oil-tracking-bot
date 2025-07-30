import logging
import os
import sqlite3
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from dotenv import load_dotenv

# --- Load .env ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# --- Flask App ---
app = Flask(__name__)

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- SQLite Setup ---
DB_PATH = "off_records.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS off_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                username TEXT,
                off_days REAL,
                remarks TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

# --- Bot App ---
application = Application.builder().token(TOKEN).build()

# --- Conversation States ---
ASK_DAYS, ASK_REMARKS = range(2)

# --- Bot Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome to the OIL Tracking Bot!\nUse /clockoff to log your off.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("How many days of off do you want to clock? (Only in 0.5 increments)")
    return ASK_DAYS

async def ask_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        off_days = float(update.message.text)
        if off_days <= 0 or off_days % 0.5 != 0:
            raise ValueError
        context.user_data['off_days'] = off_days
        await update.message.reply_text("Enter remarks (or type 'None'):")
        return ASK_REMARKS
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number in 0.5 increments.")
        return ASK_DAYS

async def ask_remarks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remarks = update.message.text
    user = update.effective_user
    off_days = context.user_data['off_days']

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO off_entries (user_id, username, off_days, remarks) VALUES (?, ?, ?, ?)",
            (user.id, user.username or user.full_name, off_days, remarks)
        )
        conn.commit()

    await update.message.reply_text(f"✅ Off clocked: {off_days} days\n📝 Remarks: {remarks}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Clock-off cancelled.")
    return ConversationHandler.END

# --- Webhook Endpoint ---
@app.route(f"/{TOKEN}", methods=["POST"])
async def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        await application.process_update(update)
    except Exception as e:
        logger.exception("Webhook error: %s", e)
    return "OK"

# --- Health Check ---
@app.route("/", methods=["GET", "HEAD"])
def index():
    return "✅ OIL Bot is live."

# --- Main Entrypoint ---
if __name__ == "__main__":
    init_db()

    application.add_handler(CommandHandler("start", start))
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("clockoff", clockoff)],
        states={
            ASK_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_days)],
            ASK_REMARKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_remarks)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)

    async def start_bot():
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")

    asyncio.run(start_bot())

    app.run(host="0.0.0.0", port=10000)
