import os
import json
import logging
import asyncio
import sqlite3
from datetime import datetime
from flask import Flask, request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ========== Setup ==========
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

# ========== Database Setup ==========
DB_FILE = "clockoff.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS clockoff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            timestamp TEXT
        )
        """)
        logger.info("Database initialized.")

init_db()

# ========== Command Handlers ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/start from user: {update.effective_user.id}")
    await update.message.reply_text("Hello! Welcome to the OIL Tracking Bot.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now().isoformat()

    logger.info(f"/clockoff by {user.id} ({user.username}) at {now}")

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
        INSERT INTO clockoff (user_id, username, first_name, last_name, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """, (
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            now
        ))

    await update.message.reply_text("Clocked off successfully.")

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"/summary requested by {user_id}")

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT timestamp FROM clockoff
        WHERE user_id = ?
        ORDER BY timestamp DESC
        LIMIT 5
        """, (user_id,))
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("No clock-off records found.")
    else:
        text = "Your recent clock-off times:\n"
        for row in rows:
            dt = datetime.fromisoformat(row[0])
            text += f"- {dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        await update.message.reply_text(text)

# ========== Register Handlers ==========
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))
application.add_handler(CommandHandler("summary", summary))

# ========== Flask Routes ==========
@app.route("/")
def index():
    logger.info("Health check ping received at /")
    return "OIL Bot is alive!", 200

@app.route(f"/{TOKEN}", methods=["POST"])
async def webhook():
    try:
        data = request.get_data()
        json_data = json.loads(data.decode("utf-8"))
        update = Update.de_json(json_data, application.bot)
        logger.info(f"Incoming update: {update.to_dict()}")
        await application.initialize()
        await application.process_update(update)
    except Exception as e:
        logger.exception("Webhook error:")
    return "", 200

# ========== Webhook Setup ==========
async def setup_webhook():
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            json={"url": f"{WEBHOOK_URL}/{TOKEN}"}
        )
        logger.info(f"Webhook set to {WEBHOOK_URL}/{TOKEN}")
        logger.info(f"Webhook response: {response.text}")

# ========== Main ==========
if __name__ == "__main__":
    logger.info("Starting OIL Bot Flask app with webhook setup...")
    asyncio.run(setup_webhook())
    app.run(host="0.0.0.0", port=10000)
