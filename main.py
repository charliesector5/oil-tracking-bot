import os
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Flask ---
from flask import Flask, request
from concurrent.futures import ThreadPoolExecutor

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

# --- Load Environment Variables ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")

# --- Google Sheets Setup ---
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID)
worksheet = sheet.get_worksheet(0)

# --- Telegram Helpers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hi! Use /clockoff or /claimoff to manage your OIL.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_oil_request(update, context, action="Clock Off")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_oil_request(update, context, action="Claim Off")

async def handle_oil_request(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user = update.effective_user
    chat = update.effective_chat
    user_state[user.id] = {
        "step": "days",
        "action": action,
        "name": user.full_name,
        "telegram_id": user.id,
        "chat_id": chat.id,
    }
    await context.bot.send_message(chat.id, f"📅 How many days do you want to {action.lower()}?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in user_state:
        return

    state = user_state[user.id]
    text = update.message.text.strip()

    if state["step"] == "days":
        try:
            state["days"] = float(text)
            state["step"] = "date"
            await update.message.reply_text("📆 Enter the application date in YYYY-MM-DD format:")
        except ValueError:
            await update.message.reply_text("⚠️ Please enter a valid number (e.g., 0.5, 1, 2.5).")
    elif state["step"] == "date":
        try:
            datetime.strptime(text, "%Y-%m-%d")
            state["application_date"] = text
            state["step"] = "reason"
            await update.message.reply_text("📝 Any remarks? (Type 'nil' if none)")
        except ValueError:
            await update.message.reply_text("⚠️ Invalid date format. Please use YYYY-MM-DD.")
    elif state["step"] == "reason":
        state["remarks"] = text
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data="approve"),
                InlineKeyboardButton("❌ Deny", callback_data="deny"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await context.bot.send_message(
            chat_id=state["chat_id"],
            text=f"🔔 {state['name']} requested to *{state['action']}*.\n📅 Days: {state['days']}\n📝 Reason: {state['remarks']}\n📆 Application Date: {state['application_date']}",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        state["msg_id"] = msg.message_id
        user_state[user.id] = state

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_user = update.effective_user.full_name

    for user_id, state in user_state.items():
        if "msg_id" in state and state["msg_id"] == query.message.message_id:
            change = +state["days"] if state["action"] == "Clock Off" else -state["days"]
            current = get_latest_off(user_id)
            final = current + change
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            worksheet.append_row([
                timestamp,
                state["telegram_id"],
                state["name"],
                state["action"],
                current,
                f"{'+' if change > 0 else ''}{change}",
                final,
                admin_user if query.data == "approve" else "Rejected",
                state["application_date"],
                state["remarks"]
            ])

            status = "approved" if query.data == "approve" else "denied"
            reply = (
                f"✅ {state['name']}'s {state['action']} approved by {admin_user}.\n"
                f"📅 Days: {state['days']}\n"
                f"📝 Reason: {state['remarks']}\n"
                f"📊 Final: {final} day(s)"
                if query.data == "approve"
                else f"❌ {state['name']}'s {state['action']} was denied by {admin_user}.\n📝 Reason: {state['remarks']}"
            )

            await context.bot.edit_message_text(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=reply
            )

            await context.bot.send_message(chat_id=state["chat_id"], text=reply)
            del user_state[user_id]
            break

def get_latest_off(user_id):
    records = worksheet.get_all_values()
    filtered = [r for r in records if r[1] == str(user_id) and r[6]]
    if filtered:
        try:
            return float(filtered[-1][6])
        except ValueError:
            return 0.0
    return 0.0

# --- Main Bot Entry ---
def main():
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(button))

    loop.create_task(telegram_app.run_polling())

# --- Run Flask App ---
if __name__ == '__main__':
    with loop:
        loop.run_in_executor(executor, main)
        app.run(host='0.0.0.0', port=10000)
