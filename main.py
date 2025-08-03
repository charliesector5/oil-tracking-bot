import os
import logging
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Google credentials
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = '/etc/secrets/credentials.json'

creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1L78iuAV5Z00mWzmqCo4Mtcax25cqqkMEcRBTnQWx_qQ/edit").sheet1

# Telegram bot token (stored as environment variable on Render)
BOT_TOKEN = os.environ.get("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("/start from user: %s", user.id)
    await update.message.reply_text("Welcome! Use /clockoff to clock your Off-in-Lieu.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.datetime.now().isoformat()

    # Log details to Google Sheets
    try:
        row = [
            datetime.datetime.now().strftime("%Y-%m-%d"),
            user.id,
            user.full_name,
            "Clocked Off",
            "", "", "", "", "",
            now
        ]
        sheet.append_row(row)
        logger.info("/clockoff by %s (%s) at %s", user.id, user.username, now)
        await update.message.reply_text("Clocked off successfully.")
    except Exception as e:
        logger.error("Failed to append to sheet: %s", e)
        await update.message.reply_text("An error occurred while logging your clock off.")

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set in environment.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clockoff", clockoff))

    logger.info("Bot started.")
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        webhook_path=f"/{BOT_TOKEN}",
        webhook_url=f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{BOT_TOKEN}",
    )

if __name__ == "__main__":
    main()
