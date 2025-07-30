import os
import logging
import asyncio
import sqlite3
import json
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask
app = Flask(__name__)

# Create SQLite DB
DB_FILE = "off_tracking.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clocked_off (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                off_days REAL,
                remarks TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

# Initialize database
init_db()

# Create Telegram application
application = Application.builder().token(TOKEN).build()
initialized = False  # Flag to avoid re-initializing

# Telegram Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracking Bot! Use /clockoff to clock your off.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(
        [["0.5", "1.0", "1.5"], ["2.0", "2.5", "3.0"]],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    await update.message.reply_text("How many days of off to clock (in multiples of 0.5)?", reply_markup=reply_markup)

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# Webhook route
@app.post(f"/{TOKEN}")
async def webhook():
    global initialized
    try:
        if not initialized:
            await application.initialize()
            await application.start()
            initialized = True

        data = request.get_data()
        update = Update.de_json(json.loads(data), application.bot)
        await application.process_update(update)
    except Exception as e:
        logger.exception(f"Webhook error: {e}")
    return jsonify(success=True)

# Root endpoint (for Render health check)
@app.get("/")
def index():
    return "OIL Tracking Bot is live."

# Set webhook when app starts
async def set_webhook():
    url = f"{WEBHOOK_URL}/{TOKEN}"
    async with application:
        await application.bot.set_webhook(url=url)
        logger.info(f"Webhook set to {url}")

if __name__ == "__main__":
    asyncio.run(set_webhook())
    app.run(host="0.0.0.0", port=10000)
