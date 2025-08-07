from flask import Flask, request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
import os
import asyncio
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import nest_asyncio

# --- Load environment variables ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# --- Flask app ---
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
user_state = {}

# --- Google Sheets Setup ---
def setup_google_sheets():
    global worksheet
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/credentials.json", scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID)
    worksheet = sheet.sheet1

# --- Bot Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome to the OIL Tracker Bot.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/clockoff - Request to Clock Off\n"
        "/claimoff - Request to Claim Off\n"
        "/summary - View current balance\n"
        "/history - View last 5 records"
    )

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_oil_request(update, context, action="Clock Off")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_oil_request(update, context, action="Claim Off")

# --- OIL Request Flow ---
async def handle_oil_request(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str = "Clock Off"):
    user = update.effective_user
    chat_id = update.effective_chat.id
    days = "1.0"
    reason = "Project"

    # Save user request state
    user_state[user.id] = {
        "full_name": user.full_name,
        "action": action,
        "days": days,
        "reason": reason,
        "chat_id": chat_id
    }

    request_id = f"{user.id}|{action}|{days}|{reason}"
    context.bot_data.setdefault("pending_requests", {})[request_id] = []

    approve_data = f"approve|{user.id}|{user.full_name}|{action}|{days}|{reason}|{chat_id}"
    deny_data = f"deny|{user.id}|{user.full_name}|{chat_id}"

    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=approve_data),
            InlineKeyboardButton("❌ Deny", callback_data=deny_data)
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    for admin_id in context.bot_data.get("admins", []):
        msg = await context.bot.send_message(
            chat_id=admin_id,
            text=(
                f"📥 *Approval Request* from {user.full_name}\n"
                f"🔹 Action: {action}\n"
                f"🔹 Days: {days}\n"
                f"📝 Reason: {reason}"
            ),
            parse_mode="Markdown",
            reply_markup=markup
        )
        context.bot_data["pending_requests"][request_id].append({
            "admin_id": admin_id,
            "message_id": msg.message_id
        })

# --- Admin Approval Callback ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split('|')

    if parts[0] == "approve":
        user_id, full_name, action, days, reason, group_chat_id = parts[1:]
        group_chat_id = int(group_chat_id)
        request_id = f"{user_id}|{action}|{days}|{reason}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            datetime.now().date(), user_id, full_name, action,
            "?", f"{'+' if action == 'Clock Off' else '-'}{days}",
            "?", query.from_user.full_name, reason, now
        ]
        worksheet.append_row(row)

        # Notify group chat
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=(
                f"✅ {full_name}'s {action} approved!\n"
                f"📅 Days: {days}\n"
                f"📝 Reason: {reason}"
            )
        )

        # Update all other admin messages
        for record in context.bot_data["pending_requests"].get(request_id, []):
            if record["admin_id"] != query.from_user.id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=record["admin_id"],
                        message_id=record["message_id"],
                        text=(
                            f"✅ {full_name}'s {action} for '{reason}' "
                            f"was *approved* by {query.from_user.full_name}."
                        ),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"❗Failed to update message for admin {record['admin_id']}: {e}")

        context.bot_data["pending_requests"].pop(request_id, None)

    elif parts[0] == "deny":
        user_id, full_name, group_chat_id = parts[1:]
        group_chat_id = int(group_chat_id)

        # Identify request
        request_id = None
        for key in context.bot_data.get("pending_requests", {}):
            if key.startswith(f"{user_id}|"):
                request_id = key
                break

        await context.bot.send_message(
            chat_id=group_chat_id,
            text=f"❌ {full_name}'s request has been *denied* by {query.from_user.full_name}.",
            parse_mode="Markdown"
        )

        # Update all other admin messages
        if request_id:
            for record in context.bot_data["pending_requests"].get(request_id, []):
                if record["admin_id"] != query.from_user.id:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=record["admin_id"],
                            message_id=record["message_id"],
                            text=(
                                f"❌ {full_name}'s request was *denied* "
                                f"by {query.from_user.full_name}."
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        print(f"❗Failed to update message for admin {record['admin_id']}: {e}")
            context.bot_data["pending_requests"].pop(request_id, None)

# --- Webhook Receiver ---
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        return "Bot not ready", 503

    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    loop.run_until_complete(telegram_app.process_update(update))
    return "OK", 200

# --- Run Bot ---
def run():
    global telegram_app
    setup_google_sheets()

    telegram_app = Application.builder().token(BOT_TOKEN).build()

    # Replace with your admin Telegram user IDs
    telegram_app.bot_data["admins"] = [123456789, 987654321]

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))

    telegram_app.run_webhook(
        listen="0.0.0.0",
        port=10000,
        url_path=BOT_TOKEN,
        webhook_url=f"{os.getenv('WEBHOOK_URL')}/{BOT_TOKEN}",
    )

if __name__ == "__main__":
    nest_asyncio.apply()
    run()
