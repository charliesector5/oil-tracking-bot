import os
import logging
import asyncio
import nest_asyncio
import gspread
from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Env ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

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
admin_message_refs = {}

MAX_REMARKS_LEN = 80       # full remarks cap (for admin text + Sheets)
CB_REASON_MAX = 10         # safe substring for callback_data to stay <64 bytes

def valid_date_str(s: str) -> bool:
    try:
        if len(s) != 10:
            return False
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

async def resolve_display_name(context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int, fallback: str) -> str:
    """
    Try to get the user's display name in the group; fall back to provided name
    or user_id if needed.
    """
    name = fallback or str(user_id)
    try:
        member = await context.bot.get_chat_member(group_id, user_id)
        if member and member.user:
            if member.user.full_name:
                name = member.user.full_name
            elif member.user.username:
                name = f"@{member.user.username}"
    except Exception as e:
        logger.warning(f"⚠️ Could not resolve display name for {user_id} in {group_id}: {e}")
    return name

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
    except Exception:
        logger.exception("❌ Error processing update")
        return "Internal Server Error", 500

def _callback(fut):
    try:
        fut.result()
    except Exception:
        logger.exception("❌ Exception in handler")

# --- Commands ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n\n"
        "/clockoff – Request to clock OIL\n"
        "/claimoff – Request to claim OIL\n"
        "/summary – See how much OIL you have left\n"
        "/history – See your past 5 OIL logs\n"
        "/help – Show this help message",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[6]
            await update.message.reply_text(f"📊 Your current off balance: {balance} day(s).")
        else:
            await update.message.reply_text("📊 No records found.")
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if row[1] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            response = "\n".join([f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | {row[8]}" for row in last_5])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text("🕒 How many days do you want to clock off? (0.5 to 3, in 0.5 increments)")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text("🧾 How many days do you want to claim off? (0.5 to 3, in 0.5 increments)")

# --- Conversation handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message.text.strip()

    if user_id not in user_state:
        return

    state = user_state[user_id]

    if state["stage"] == "awaiting_days":
        try:
            days = float(message)
            if days < 0.5 or days > 3 or (days * 10) % 5 != 0:
                raise ValueError()
            state["days"] = days
            state["stage"] = "awaiting_app_date"
            await update.message.reply_text("📅 Application Date? Please use YYYY-MM-DD (e.g., 2025-08-15)")
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")

    elif state["stage"] == "awaiting_app_date":
        if not valid_date_str(message):
            await update.message.reply_text("❌ Invalid date format. Use YYYY-MM-DD (e.g., 2025-08-15).")
            return
        state["application_date"] = message
        state["stage"] = "awaiting_reason"
        await update.message.reply_text(f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")

    elif state["stage"] == "awaiting_reason":
        reason = message
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        state["reason"] = reason
        state["group_id"] = update.message.chat_id
        await update.message.reply_text("📩 Your request has been submitted for approval.")
        await send_approval_request(update, context, state)
        user_state.pop(user_id, None)

# --- Admin approval flow ---
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    group_id = state["group_id"]
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if row[1] == str(user.id)]
        current_off = float(user_rows[-1][6]) if user_rows else 0.0
        delta = float(state["days"])
        new_off = current_off + delta if state["action"] == "clockoff" else current_off - delta

        admins = await context.bot.get_chat_administrators(group_id)
        admin_message_refs[user.id] = []

        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                # Keep callback_data short: trim reason for callback only
                reason_cb = state["reason"][:CB_REASON_MAX]
                msg = await context.bot.send_message(
                    chat_id=admin.user.id,
                    text=(
                        f"🆕 *{state['action'].title()} Request*\n\n"
                        f"👤 User: {user.full_name} ({user.id})\n"
                        f"📅 Days: {state['days']}\n"
                        f"📆 Application Date: {state['application_date']}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"📊 Current Off: {current_off:.1f} day(s)\n"
                        f"📈 New Balance: {new_off:.1f} day(s)\n\n"
                        "✅ Approve or ❌ Deny?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "✅ Approve",
                            callback_data=f"approve|{user.id}|{state['action']}|{state['days']}|{reason_cb}|{group_id}|{state['application_date']}"
                        ),
                        InlineKeyboardButton(
                            "❌ Deny",
                            callback_data=f"deny|{user.id}|{reason_cb}|{group_id}|{state['application_date']}"
                        )
                    ]])
                )
                admin_message_refs[user.id].append((admin.user.id, msg.message_id))
            except Exception as e:
                logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")
    except Exception:
        logger.exception("❌ Failed to fetch or notify admins")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        if data.startswith("approve|"):
            # approve|user_id|action|days|reason_short|group_id|app_date
            _, uid, action, days, reason_short, group_id, app_date = data.split("|", maxsplit=6)
            user_id = uid
            group_id = int(group_id)

            # Pull full reason/name by looking up the message sender (safe fallback if not in state)
            # Prefer full name from chat membership for announcement
            display_name = await resolve_display_name(context, group_id, int(user_id), fallback="")

            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

            all_data = worksheet.get_all_values()
            rows = [row for row in all_data if row[1] == str(user_id)]
            current_off = float(rows[-1][6]) if rows else 0.0
            delta = float(days)
            final = current_off + delta if action == "clockoff" else current_off - delta
            add_subtract = f"+{delta}" if action == "clockoff" else f"-{delta}"

            # Use user's latest known full name if available via get_chat_member
            user_full_name = display_name if display_name else str(user_id)

            # Append to Sheet with NEW order:
            # Timestamp, Telegram ID, Name, Action, Current Off, Add/Subtract, Final Off, Approved By, Application Date, Remarks
            # We only have reason_short here, but admin PM had the full reason (capped at 80).
            # Since we trimmed only for callback, we reuse the same trimmed value in row to avoid callback-size issues;
            # if you prefer full remark here, we need server-side storage (token method).
            worksheet.append_row([
                timestamp,
                str(user_id),
                user_full_name,
                "Clock Off" if action == "clockoff" else "Claim Off",
                f"{current_off:.1f}",
                add_subtract,
                f"{final:.1f}",
                query.from_user.full_name,
                app_date,
                reason_short
            ])

            await query.edit_message_text("✅ Request approved and recorded.")
            await context.bot.send_message(
                chat_id=group_id,
                text=(
                    f"✅ {display_name or user_id}'s {action.replace('off', ' Off')} approved by {query.from_user.full_name}.\n"
                    f"📅 Days: {days}\n"
                    f"📆 Application Date: {app_date}\n"
                    f"📝 Reason: {reason_short}\n"
                    f"📊 Final: {final:.1f} day(s)"
                )
            )

        elif data.startswith("deny|"):
            # deny|user_id|reason_short|group_id|app_date
            _, uid, reason_short, group_id, app_date = data.split("|", maxsplit=4)
            user_id = uid
            group_id = int(group_id)
            display_name = await resolve_display_name(context, group_id, int(user_id), fallback="")

            await query.edit_message_text("❌ Request denied.")
            await context.bot.send_message(
                chat_id=group_id,
                text=(
                    f"❌ {display_name or user_id}'s request was denied by {query.from_user.full_name}.\n"
                    f"📆 Application Date: {app_date}\n"
                    f"📝 Reason: {reason_short}"
                )
            )

        # Clean up all admin messages for this user (same behavior as before)
        # Note: key is user_id because structure remains aligned with your working version
        uid_key = int(uid) if 'uid' in locals() and uid.isdigit() else None
        if uid_key and uid_key in admin_message_refs:
            for admin_id, msg_id in admin_message_refs[uid_key]:
                if admin_id != query.from_user.id:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=admin_id,
                            message_id=msg_id,
                            text=f"⚠️ Request already handled by {query.from_user.full_name}.",
                        )
                    except Exception:
                        pass
            del admin_message_refs[uid_key]

    except Exception:
        logger.exception("❌ Failed to process callback")
        await query.edit_message_text("❌ Something went wrong.")

# --- Init ---
async def init_app():
    global telegram_app, worksheet

    logger.info("🔐 Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    worksheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    logger.info("✅ Google Sheets ready.")

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).get_updates_http_version("1.1").build()
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("summary", summary))
    telegram_app.add_handler(CommandHandler("history", history))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook set.")

# --- Run ---
if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    logger.info("🟢 Starting Flask...")
    app.run(host="0.0.0.0", port=10000)
