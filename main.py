import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import asyncio
from functools import partial
import os

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load service account credentials from a JSON file
SERVICE_ACCOUNT_FILE = "service_account.json"
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
client = gspread.authorize(creds)

# Open the Google Sheet (use the correct name of your sheet)
SHEET_NAME = "OIL Tracking Sheet"
WORKSHEET_INDEX = 0  # use index or name
sheet = client.open(SHEET_NAME).get_worksheet(WORKSHEET_INDEX)

# Bot command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start from user: {user.id}")
    await update.message.reply_text("Welcome to the OIL Tracking Bot!")

# Synchronous helper to log to sheet
def log_to_sheets(sheet, row):
    sheet.append_row(row)

# Bot command: /clockoff
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now().isoformat()

    logger.info(f"/clockoff by {user.id} ({user.username}) at {now}")

    row = [
        datetime.now().strftime('%Y-%m-%d'),  # Date
        user.id,                              # Telegram ID
        user.full_name,                       # Name
        "Clock Off",                          # Action
        "", "", "", "",                       # Add/Subtract etc.
        "",                                   # Remarks
        now                                   # Timestamp
    ]

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, partial(log_to_sheets, sheet, row))
        await update.message.reply_text("Clocked off successfully.")
    except Exception as e:
        logger.error(f"Failed to clock off: {e}")
        await update.message.reply_text("❌ Failed to clock off. Please try again later.")

# Entry point
def main():
    # Load bot token from environment variable
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable not set.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clockoff", clockoff))

    # Run the bot using webhook (Render setup)
    import os
    PORT = int(os.environ.get("PORT", 5000))
    WEBHOOK_PATH = f"/{BOT_TOKEN}"
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://your-app.onrender.com

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
