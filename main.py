import os
import logging
import sqlite3
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Load from .env if running locally
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_ACTUAL_BOT_TOKEN_HERE")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

DB_FILE = "oil.db"

# Initialize Flask
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is alive!"

# SQLite DB Setup
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS oil (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            username TEXT,
            type TEXT,
            remark TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Clock Off", callback_data="clock_off")],
        [InlineKeyboardButton("Claim Off", callback_data="claim_off")],
        [InlineKeyboardButton("View History", callback_data="history")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Welcome to the OIL Tracker!", reply_markup=reply_markup)

# Callback for button presses
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    action = query.data

    if action == "clock_off":
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT INTO oil (user_id, username, type) VALUES (?, ?, ?)",
                         (str(user.id), user.username, "Clock Off"))
        await query.edit_message_text("✅ You have clocked off.")

    elif action == "claim_off":
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT INTO oil (user_id, username, type) VALUES (?, ?, ?)",
                         (str(user.id), user.username, "Claim Off"))
        await query.edit_message_text("📝 Your off has been claimed.")

    elif action == "history":
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute("SELECT type, remark, timestamp FROM oil WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10",
                                  (str(user.id),))
            records = cursor.fetchall()
        if records:
            history = "\n".join(f"{t} - {r or 'No remark'} - {ts}" for t, r, ts in records)
        else:
            history = "No records found."
        await query.edit_message_text(f"📋 Your history:\n\n{history}")

# Run both Flask and Telegram
def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Start Telegram Bot (non-blocking)
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

    # Run Flask app (blocking)
    flask_app.run(host="0.0.0.0", port=8080)

if __name__ == "__main__":
    main()
