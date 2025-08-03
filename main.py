import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# === Logging Setup ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Constants ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")  # Set this in Render as environment variable
SPREADSHEET_NAME = "Sector 5 Charlie OIL Record"
worksheet_name = "OIL RECORD"

# === Google Sheets Setup ===
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_file("/etc/secrets/credentials.json", scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open(SPREADSHEET_NAME).worksheet(worksheet_name)

# === Telegram Bot Commands ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Use /clockoff <current> <+/-> <final> <approved_by> <remarks> to log OIL."
    )

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 5:
            await update.message.reply_text(
                "❗ Format:\n/clockoff <current> <+/-> <final> <approved_by> <remarks>"
            )
            return

        user = update.effective_user
        date = datetime.now().strftime("%Y-%m-%d")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        new_row = [
            date,
            user.id,
            user.full_name,
            "Clock Off",
            args[0],             # Current Off
            args[1],             # Add/Subtract
            args[2],             # Final Off
            args[3],             # Approved By
            " ".join(args[4:]),  # Remarks
            timestamp
        ]

        sheet.append_row(new_row)
        await update.message.reply_text("✅ OIL clock-off recorded successfully.")
        logger.info(f"Recorded clock-off: {new_row}")
    except Exception as e:
        logger.error(f"Error in /clockoff: {e}")
        await update.message.reply_text("⚠️ Failed to record. Please try again.")

# === Flask App Setup for Webhook ===
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET", "HEAD"])
def health_check():
    logger.info("Health check ping received at /")
    return "OK", 200

@flask_app.route("/webhook", methods=["POST"])
async def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "OK", 200

# === Telegram Application Setup ===
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# === Run Webhook ===
if __name__ == "__main__":
    logger.info("Bot started.")
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        webhook_url=f"{WEBHOOK_URL}/webhook"
    )
