import os
import logging
import asyncio
import nest_asyncio
import gspread
from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Load environment and credentials ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

# --- Flask App ---
app = Flask(__name__)

@app.route('/')
def index():
    return "✅ Oil Tracking Bot is up."

@app.route('/health')
def health():
    return "✅ Health check passed."

# --- Globals ---
telegram_app = None
worksheet = None
loop = asyncio.new_event_loop()
executor = ThreadPoolExecutor()
user_state = {}  # To track ongoing conversations

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        logger.warning("⚠️ Telegram app not yet initialized.")
        return "Bot not ready", 503

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        logger.info(f"📨 Incoming update: {request.get_json(force=True)}")
        future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
        future.add_done_callback(_callback)
        return "OK"
    except Exception as e:
        logger.exception("❌ Error processing update")
        return "Internal Server Error", 500

def _callback(fut):
    try:
        fut.result()
    except Exception as e:
        logger.exception("❌ Exception in Telegram handler task")

# --- Telegram Handlers ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n"
        "\n"
        "/clockoff – Request to clock OIL\n"
        "/claimoff – Request to claim OIL\n"
        "/summary – See how much OIL you have left\n"
        "/history – See your past 5 OIL logs\n"
        "/help – Show this help message\n",
        parse_mode="Markdown"
    )

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_state[user_id] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text("🕒 How many days do you want to clock off? (0.5 to 3, increments of 0.5)")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_state[user_id] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text("🧾 How many days do you want to claim off? (0.5 to 3, increments of 0.5)")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_state:
        return  # Ignore if not in a flow

    state = user_state[user_id]
    message_text = update.message.text.strip()

    if state["stage"] == "awaiting_days":
        try:
            days = float(message_text)
            if days < 0.5 or days > 3 or (days * 10) % 5 != 0:
                raise ValueError()
            state["days"] = days
            state["stage"] = "awaiting_reason"
            await update.message.reply_text("📝 What's the reason? (Max 20 characters)")
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 and 3 in 0.5 steps.")

    elif state["stage"] == "awaiting_reason":
        reason = message_text[:20]
        state["reason"] = reason

        now = datetime.now()
        date = now.strftime("%Y-%m-%d")
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        user = update.effective_user

        try:
            current_data = worksheet.get_all_values()
            user_rows = [row for row in current_data if row[1] == str(user.id)]

            if user_rows:
                last_row = user_rows[-1]
                current_off = float(last_row[6])  # Final Off
            else:
                current_off = 0.0

            delta = state["days"]
            action = state["action"]
            add_subtract = f"+{delta}" if action == "clockoff" else f"-{delta}"
            final_off = current_off + delta if action == "clockoff" else current_off - delta

            worksheet.append_row([
                date,
                str(user.id),
                user.full_name,
                "Clock Off" if action == "clockoff" else "Claim Off",
                f"{current_off:.1f}",
                add_subtract,
                f"{final_off:.1f}",
                "System",
                reason,
                timestamp
            ])

            await update.message.reply_text(
                f"✅ {action.replace('off', ' Off').title()} of {delta} day(s) recorded.\n📊 You now have {final_off:.1f} off(s)."
            )
            logger.info(f"📝 {action} written to sheet for {user.full_name}: {delta} day(s)")

        except Exception:
            logger.exception("❌ Failed to write to Google Sheets")
            await update.message.reply_text("❌ Something went wrong. Try again later.")

        user_state.pop(user_id)

# --- Initialization ---
async def init_app():
    global telegram_app, worksheet

    logger.info("📄 Connecting to Google Sheets...")
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sheet.sheet1
        logger.info("✅ Google Sheets initialized and worksheet loaded.")
    except Exception as e:
        logger.error(f"❌ Google Sheets init failed: {e}")
        return

    logger.info("⚙️ Initializing Telegram Application...")
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).get_updates_http_version("1.1").build()
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook has been set.")

# --- Run Everything ---
if __name__ == "__main__":
    nest_asyncio.apply()

    def run_loop():
        loop.run_forever()

    import threading
    threading.Thread(target=run_loop, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    logger.info("🟢 Starting Flask server to keep the app alive...")
    app.run(host="0.0.0.0", port=10000)