import os
import logging
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import telegram

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram Bot Token
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Google Sheets setup
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = '/etc/secrets/service_account.json'

creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
client = gspread.authorize(creds)

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
sheet = client.open_by_key(SPREADSHEET_ID).sheet1

# Flask app setup
app = Flask(__name__)

# Telegram bot application
application = Application.builder().token(BOT_TOKEN).build()

# Telegram command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start from user: {user.id}")
    await update.message.reply_text(f"Hello {user.first_name}, welcome to the Off Tracking Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.datetime.now().isoformat()
    name = f"{user.first_name} {user.last_name or ''}".strip()
    logger.info(f"/clockoff by {user.id} ({user.username}) at {now}")

    try:
        sheet.append_row([
            datetime.datetime.now().strftime('%Y-%m-%d'),
            str(user.id),
            name,
            "Clock Off",
            "",  # Current off balance
            "",  # Add/Subtract
            "",  # Final off balance
            "",  # Approved by
            "",  # Remarks
            now
        ])
        await update.message.reply_text("Clocked off successfully.")
    except Exception as e:
        logger.error(f"Error writing to sheet: {e}")
        await update.message.reply_text("An error occurred while logging. Please try again.")

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# Webhook route
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
async def webhook():
    update = telegram.Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"

# Webhook startup
if __name__ == "__main__":
    application.run_polling()
