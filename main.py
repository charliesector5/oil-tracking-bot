import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ------------------- Logging Setup -------------------
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.info("🚀 Starting Oil Tracking Bot initialization")

# ------------------- Environment Variables -------------------
TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))  # Render assigns a port
WEBHOOK_PATH = f"/{TOKEN}"
WEBHOOK_URL = f"https://oil-tracking-bot.onrender.com{WEBHOOK_PATH}"

if not TOKEN:
    logger.error("❌ BOT_TOKEN not set in environment variables!")
    raise RuntimeError("BOT_TOKEN not set")

# ------------------- Google Sheets Setup -------------------
worksheet = None
try:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        '/etc/secrets/credentials.json', scope
    )
    gc = gspread.authorize(creds)
    worksheet = gc.open("Sector 5 Charlie Oil Record").worksheet("OIL Record")
    logger.info("✅ Google Sheets initialized and worksheet loaded.")
except Exception as e:
    logger.exception("❌ Failed to initialize Google Sheets")

# ------------------- Command Handlers -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"📥 /start triggered by {user.full_name} ({user.id})")
    await update.message.reply_text("✅ Bot is alive and connected!")

# ------------------- Main Runner -------------------
async def main():
    logger.info("⚙️ Building Telegram Application...")
    app = Application.builder().token(TOKEN).build()

    logger.info("📦 Registering command handlers...")
    app.add_handler(CommandHandler("start", start))

    logger.info(f"🌐 Setting webhook: {WEBHOOK_URL}")
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL
    )

# ------------------- Run Application -------------------
if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("❌ Bot crashed during startup")
