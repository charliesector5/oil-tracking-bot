import os
import logging
import threading
import asyncio
from flask import Flask, request
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Google Sheets setup
logger.info("📄 Connecting to Google Sheets...")
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
credentials = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
logger.info("✅ Google Sheets initialized and worksheet loaded.")

# Asyncio loop
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Initialize Telegram bot
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

# Store ongoing conversations
user_conversations = {}

# /start handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"✅ /start triggered by {update.effective_user.id}")
    await update.message.reply_text("👋 Hello! I'm your OIL tracking bot.")

# /clockoff handler
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_conversations[user_id] = {"stage": "awaiting_days"}
    logger.info(f"✅ /clockoff triggered by {update.effective_user.username} ({user_id})")
    await update.message.reply_text("🕒 How many days are you clocking off? (0.5 to 3)")

# Message handler
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_conversations:
        return

    data = user_conversations[user_id]

    # Step 1: Get number of days
    if data["stage"] == "awaiting_days":
        try:
            days = float(update.message.text)
            if days < 0.5 or days > 3 or days % 0.5 != 0:
                raise ValueError()
            data["days"] = days
            data["stage"] = "awaiting_reason"
            await update.message.reply_text("📝 What's the reason? (Max 20 characters)")
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number between 0.5 to 3 (in 0.5 steps).")

    # Step 2: Get reason
    elif data["stage"] == "awaiting_reason":
        reason = update.message.text.strip()
        if len(reason) > 20:
            await update.message.reply_text("❌ Reason must be within 20 characters.")
            return

        data["reason"] = reason

        # Write to Google Sheets
        final_row = [
            "",  # Date - optional
            str(user_id),  # Telegram ID
            update.effective_user.full_name,  # Name
            "Clock Off",  # Action
            "",  # Current Off
            f"+{data['days']}",  # Add/Subtract
            "",  # Final Off
            "",  # Approved by
            reason,  # Remarks
            ""  # Timestamp
        ]
        sheet.append_row(final_row)
        await update.message.reply_text(f"✅ Clock off of {data['days']} day(s) recorded with reason: {reason}")
        logger.info(f"📝 Wrote row to sheet: {final_row}")

        # Clear conversation
        user_conversations.pop(user_id)

# Register handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("clockoff", clockoff))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

# Webhook route
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    logger.info(f"📨 Incoming update: {update.to_dict()}")
    loop.call_soon_threadsafe(
        asyncio.create_task,
        telegram_app.process_update(update)
    )
    return "OK"

# Health check
@app.route("/", methods=["GET", "HEAD"])
def index():
    return "Bot is running."

# Thread for Telegram webhook
def run_telegram():
    async def start_bot():
        await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
        logger.info("🌐 Webhook set.")
    loop.run_until_complete(start_bot())

threading.Thread(target=run_telegram).start()

# Run Flask app
if __name__ == "__main__":
    logger.info("🟢 Starting Flask server to keep the app alive...")
    app.run(host="0.0.0.0", port=10000)
