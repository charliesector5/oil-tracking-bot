import logging
from flask import Flask, request
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
import os
import gspread
import datetime
import asyncio
from concurrent.futures import ThreadPoolExecutor

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Flask ---
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

@app.route(f"/{os.environ['BOT_TOKEN']}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        logger.warning("⚠️ Telegram app not yet initialized.")
        return "Bot not ready", 503

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        logger.info(f"📨 Incoming update: {update}")
        fut = loop.run_in_executor(executor, telegram_app.process_update, update)
        fut.add_done_callback(_callback)
        return "OK", 200
    except Exception as e:
        logger.exception("❌ Error processing update")
        return "Error", 500

def _callback(fut):
    try:
        fut.result()
    except Exception as e:
        logger.exception("❌ Exception in handler")

# --- Sheet Functions ---
def init_sheet():
    gc = gspread.service_account(filename='credentials.json')
    sh = gc.open_by_key(os.environ['GOOGLE_SHEET_ID'])
    return sh.sheet1

def get_current_off(telegram_id):
    records = worksheet.get_all_records()
    for row in records[::-1]:
        if str(row['Telegram ID']) == str(telegram_id):
            return float(row['Final Off'])
    return 0.0

def log_to_sheet(timestamp, telegram_id, name, action, current_off, delta, final_off, approved_by, remarks):
    worksheet.append_row([
        timestamp, telegram_id, name, action, current_off, delta, final_off, approved_by, remarks
    ])

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! Use /clockoff or /claimoff to begin.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {
        'action': 'Clock Off',
        'name': update.effective_user.full_name,
        'username': update.effective_user.username,
        'telegram_id': update.effective_user.id
    }
    await update.message.reply_text("📝 How many day(s) of Off do you want to clock?")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {
        'action': 'Claim Off',
        'name': update.effective_user.full_name,
        'username': update.effective_user.username,
        'telegram_id': update.effective_user.id
    }
    await update.message.reply_text("📝 How many day(s) of Off do you want to claim?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_state:
        return

    state = user_state[user_id]

    if 'days' not in state:
        try:
            days = float(update.message.text.strip())
            state['days'] = days
            await update.message.reply_text("📅 Which date is this Off for? (YYYY-MM-DD)")
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number of days (e.g. 0.5, 1, 2).")
        return

    if 'date' not in state:
        date_input = update.message.text.strip()
        try:
            datetime.datetime.strptime(date_input, "%Y-%m-%d")
            state['date'] = date_input
            await update.message.reply_text("🗒️ Any remarks? (Type 'nil' if none)")
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid date in YYYY-MM-DD format.")
        return

    if 'remarks' not in state:
        state['remarks'] = update.message.text.strip()
        await ask_admin_approval(update, context, state)
        return

async def ask_admin_approval(update, context, state):
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    for admin in admins:
        if admin.user.is_bot:
            continue
        try:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{state['telegram_id']}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{state['telegram_id']}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=admin.user.id,
                text=(
                    f"📋 New {state['action']} request:\n"
                    f"👤 {state['name']} ({state['username'] or state['telegram_id']})\n"
                    f"📅 Days: {state['days']}\n"
                    f"🗓️ Date: {state['date']}\n"
                    f"📝 Reason: {state['remarks']}"
                ),
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")
    await update.message.reply_text("🕐 Sent request to admin for approval.")
    return

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    decision, user_id = query.data.split(':')
    user_id = int(user_id)
    if user_id not in user_state:
        await query.edit_message_text("⚠️ This request has expired or is invalid.")
        return

    state = user_state.pop(user_id)
    approver_name = query.from_user.full_name

    current_off = get_current_off(user_id)
    delta = state['days'] if state['action'] == 'Clock Off' else -state['days']
    final_off = round(current_off + delta, 2)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log_to_sheet(
        timestamp=timestamp,
        telegram_id=user_id,
        name=state['name'],
        action=state['action'],
        current_off=current_off,
        delta=delta,
        final_off=final_off,
        approved_by=approver_name,
        remarks=state['remarks']
    )

    sender = state['username'] or state['name']
    await query.edit_message_text(
        f"✅ {sender}'s {state['action']} approved by {approver_name}.\n"
        f"📅 Days: {state['days']}\n"
        f"📝 Reason: {state['remarks']}\n"
        f"📊 Final: {final_off} day(s)"
    )

# --- Main ---
if __name__ == '__main__':
    TOKEN = os.environ['BOT_TOKEN']
    app_name = os.environ.get('RENDER_EXTERNAL_HOSTNAME')

    worksheet = init_sheet()
    telegram_app = Application.builder().token(TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(button))

    async def run_bot():
        await telegram_app.initialize()
        await telegram_app.start()
        logger.info("🤖 Bot polling started.")

    loop.run_until_complete(run_bot())
    app.run(host="0.0.0.0", port=10000)
