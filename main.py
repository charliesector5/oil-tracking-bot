import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler
)

# ENV
TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://yourbot.onrender.com

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask App
app = Flask(__name__)

# SQLite DB Setup
DB_PATH = "oil.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS oil_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            timestamp TEXT NOT NULL,
            days REAL NOT NULL,
            remark TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Telegram Bot App
application = ApplicationBuilder().token(TOKEN).build()

# Conversation states
SELECT_DAYS, ENTER_REMARK = range(2)
user_state = {}

# /start handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")

# /clockoff entry point
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_chat.id] = {}
    keyboard = [
        [InlineKeyboardButton("0.5", callback_data="0.5"),
         InlineKeyboardButton("1.0", callback_data="1.0"),
         InlineKeyboardButton("1.5", callback_data="1.5")],
        [InlineKeyboardButton("2.0", callback_data="2.0"),
         InlineKeyboardButton("3.0", callback_data="3.0")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("How many days of OIL to clock?", reply_markup=reply_markup)
    return SELECT_DAYS

# Callback for day selection
async def select_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_state[query.message.chat.id]['days'] = float(query.data)
    await query.edit_message_text("Please enter your remark for this clock off (or type 'NIL'):")
    return ENTER_REMARK

# Callback for remark input
async def enter_remark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    username = update.effective_user.username or "Unknown"
    remark = update.message.text.strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    days = user_state[user_id].get('days')

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO oil_log (user_id, username, timestamp, days, remark) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, timestamp, days, remark)
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ OIL clocked at {timestamp}\nDays: {days}\nRemark: {remark}"
        )
    except Exception as e:
        logger.error(f"DB error: {e}")
        await update.message.reply_text("⚠️ Failed to log OIL. Please try again.")

    user_state.pop(user_id, None)
    return ConversationHandler.END

# Cancel fallback
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OIL clocking cancelled.")
    user_state.pop(update.effective_chat.id, None)
    return ConversationHandler.END

# Register handlers
application.add_handler(CommandHandler("start", start))

application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("clockoff", clockoff)],
        states={
            SELECT_DAYS: [CallbackQueryHandler(select_days)],
            ENTER_REMARK: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_remark)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
)

# Flask webhook endpoint
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    
    async def handle_update():
        # This is the missing piece
        if not application.initialized:
            await application.initialize()
        await application.process_update(update)

    try:
        asyncio.run(handle_update())
    except Exception as e:
        logging.error(f"Exception on webhook: {e}")

    return "OK"

# Webhook setup (executed only once)
async def set_webhook():
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")

if __name__ == "__main__":
    asyncio.run(set_webhook())
    app.run(host="0.0.0.0", port=10000)
