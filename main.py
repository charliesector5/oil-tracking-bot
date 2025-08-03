import os
import logging
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Google Sheets setup
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = '/etc/secrets/credentials.json'
creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1L78iuAV5Z00mWzmqCo4Mtcax25cqqkMEcRBTnQWx_qQ/edit").sheet1

# Telegram bot token
BOT_TOKEN = os.getenv("BOT_TOKEN")

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("/start from user: %s", user.id)
    await update.message.reply_text("Welcome! Use /clockoff to clock your Off-in-Lieu.")

# /clockoff command
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.datetime.now().isoformat()

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
        logger.error("Failed to log clock off: %s", e)
        await update.message.reply_text("Something went wrong while logging your clock off.")

# Main function
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not found.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clockoff", clockoff))

    # Webhook URL setup
    render_url = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if not render_url:
        logger.error("RENDER_EXTERNAL_HOSTNAME not set.")
        return

    webhook_url = f"https://{render_url}/{BOT_TOKEN}"
    await app.bot.set_webhook(url=webhook_url)

    logger.info("Bot started.")

    await app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )

# Entry point
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
