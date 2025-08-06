import os
import logging
import pytz
import datetime
import gspread
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "OIL Record")
GOOGLE_CREDENTIALS_PATH = "/etc/secrets/credentials.json"

# Connect to Google Sheets
def connect_google_sheet():
    logger.info("📄 Connecting to Google Sheets...")
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            GOOGLE_CREDENTIALS_PATH, scope
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sheet.worksheet(WORKSHEET_NAME)
        logger.info("✅ Google Sheets initialized and worksheet loaded.")
        return worksheet
    except Exception as e:
        logger.error(f"❌ Google Sheets initialization failed: {e}")
        return None

worksheet = connect_google_sheet()

# Bot commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Use /clockoff to log your off-time.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.datetime.now(pytz.timezone("Asia/Singapore"))

    if worksheet:
        try:
            row = [
                now.strftime("%Y-%m-%d"),
                str(user.id),
                user.full_name,
                "Clock Off",
                "", "", "", "", "",
                now.strftime("%Y-%m-%d %H:%M:%S"),
            ]
            worksheet.append_row(row)
            await update.message.reply_text("✅ Your off-time has been logged.")
        except Exception as e:
            logger.error(f"❌ Error writing to Google Sheets: {e}")
            await update.message.reply_text("❌ Failed to log off-time.")
    else:
        await update.message.reply_text("⚠️ Google Sheet is not available.")

# Flask app to handle webhooks
app = Flask(__name__)

@app.route("/", methods=["GET", "HEAD"])
def health_check():
    return "Bot is running!", 200

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.process_update(update)
    return "OK", 200

# Main entry
async def main():
    global application
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clockoff", clockoff))

    # Set webhook
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook has been set.")
    logger.info("🟢 Starting Flask server to keep the app alive...")

if __name__ == "__main__":
    import nest_asyncio
    import asyncio
    nest_asyncio.apply()
    asyncio.run(main())
    app.run(host="0.0.0.0", port=10000)
