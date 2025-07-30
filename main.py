import os
import json
import logging
import sqlite3
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.ext import Dispatcher
from telegram.ext import CallbackContext
from telegram.ext import Defaults
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Initialize database
DB_PATH = "clockoff.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS clockoff_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Telegram bot application
application = Application.builder().token(TOKEN).build()

# ===========================
# Telegram Command Handlers
# ===========================

def start(update: Update, context: CallbackContext):
    logger.info(f"/start from user: {update.effective_user.id}")
    update.message.reply_text("Hello! Welcome to the OIL Tracking Bot.")

def clockoff(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    username = update.effective_user.username
    logger.info(f"/clockoff by user: {user_id} ({username})")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO clockoff_records (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

    update.message.reply_text("Clocked off successfully.")

def summary(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    logger.info(f"/summary requested by user: {user_id}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT timestamp FROM clockoff_records WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5", (user_id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        update.message.reply_text("No clock-off records found.")
    else:
        messages = [f"Recent clock-off times:"]
        for r in rows:
            messages.append(f"- {r[0]}")
        update.message.reply_text("\n".join(messages))

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))
application.add_handler(CommandHandler("summary", summary))

# ===========================
# Flask Routes
# ===========================

@app.route("/")
def index():
    logger.info("Health check received at /")
    return "OIL Bot is alive!", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data.decode("utf-8"))
        update = Update.de_json(data, application.bot)
        application.update_queue.put(update)
        logger.info(f"Incoming update: {update.to_dict()}")
    except Exception as e:
        logger.exception("Webhook error:")
    return "", 200

# ===========================
# Webhook Setup
# ===========================

def setup_webhook():
    import httpx
    response = httpx.post(
        f"https://api.telegram.org/bot{TOKEN}/setWebhook",
        json={"url": f"{WEBHOOK_URL}/{TOKEN}"}
    )
    logger.info(f"Webhook set to {WEBHOOK_URL}/{TOKEN}")
    logger.info(f"Response: {response.text}")

# ===========================
# Start Everything
# ===========================

if __name__ == "__main__":
    logger.info("Starting bot setup...")
    setup_webhook()
    logger.info("Webhook setup complete. Launching Flask app.")
    app.run(host="0.0.0.0", port=10000)
