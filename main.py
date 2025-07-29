import os
import json
import asyncio
from datetime import datetime
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
import pytz

# === Load Environment Variables ===
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

DATA_FILE = "data/clockoff_log.json"
TIMEZONE = pytz.timezone("Asia/Singapore")


# === Ensure Data Directory ===
os.makedirs("data", exist_ok=True)
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump([], f)


# === Telegram Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is working.")


async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    remark = " ".join(context.args) if context.args else ""

    log_entry = {
        "user_id": user.id,
        "name": user.full_name,
        "timestamp": now,
        "remark": remark
    }

    # Append to JSON file
    try:
        with open(DATA_FILE, "r+") as f:
            data = json.load(f)
            data.append(log_entry)
            f.seek(0)
            json.dump(data, f, indent=2)
        await update.message.reply_text(f"✅ OIL clocked at {now}.\nRemark: {remark or 'NIL'}")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to log OIL. Please try again.")
        print(f"[ERROR] Failed to write JSON: {e}")


application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))


# === Webhook Integration ===
@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)

    async def handle():
        await application.initialize()
        await application.process_update(update)

    asyncio.run(handle())
    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200


@app.route("/set-webhook", methods=["GET"])
def set_webhook_route():
    asyncio.run(set_webhook())
    return "Webhook set!", 200


async def set_webhook():
    await application.initialize()
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    print(f"✅ Webhook set to: {WEBHOOK_URL}/{TOKEN}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
