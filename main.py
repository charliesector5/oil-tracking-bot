import os
import logging
import asyncio
import nest_asyncio
import gspread
import uuid
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
from datetime import datetime, timedelta

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

# conversational state per user while filling the form
# { user_id: { action, stage, days, app_date, reason, group_id } }
user_state = {}

# pending approval requests stored server-side to keep callback_data short
# token -> {user_id, user_full_name, action, days, app_date, reason, group_id, is_holiday}
pending_requests = {}

# track admin PMs per token to clean up when one admin handles it
# token -> [(admin_id, message_id), ...]
admin_message_refs = {}

# Column indices (0-based) for clarity when reading the sheet
COL_TIMESTAMP = 0
COL_TELEGRAM_ID = 1
COL_NAME = 2
COL_ACTION = 3
COL_CURR_OFF = 4
COL_ADD_SUB = 5
COL_FINAL_OFF = 6
COL_APPROVED_BY = 7
COL_APP_DATE = 8
COL_REMARKS = 9
COL_HOLIDAY = 10
COL_PH_TOTAL = 11
COL_EXPIRY = 12

# --- Webhook ---
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

# --- Utility ---
MAX_REMARKS_LEN = 80

def parse_date_yyyy_mm_dd(text: str) -> str | None:
    try:
        dt = datetime.strptime(text.strip(), "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None

def compute_expiry_from(app_date_str: str) -> str:
    try:
        base = datetime.strptime(app_date_str, "%Y-%m-%d")
        expiry = base + timedelta(days=365)
        return expiry.strftime("%Y-%m-%d")
    except Exception:
        return "N/A"

async def get_current_off_for_user(user_id: str) -> float:
    try:
        all_data = worksheet.get_all_values()
        rows = [row for row in all_data if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID] == str(user_id)]
        if rows:
            last = rows[-1]
            return float(last[COL_FINAL_OFF]) if last[COL_FINAL_OFF] else 0.0
    except Exception as e:
        logger.warning(f"⚠️ Could not read current off for {user_id}: {e}")
    return 0.0

# --- Commands ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n\n"
        "/clockoff – Request to clock OIL\n"
        "/claimoff – Request to claim OIL\n"
        "/clockphoff – Request to clock Public Holiday OIL (+365d expiry)\n"
        "/claimphoff – Request to claim Public Holiday OIL\n"
        "/massclockphoff – Admin only: mass clock PH OIL (preview + confirm)\n"
        "/summary – See your OIL & PH OIL details\n"
        "/history – See your past 5 OIL logs\n"
        "/help – Show this help message",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID] == str(user.id)]
        if not user_rows:
            await update.message.reply_text("📊 No records found.")
            return

        # current overall off is last row's Final Off
        balance = user_rows[-1][COL_FINAL_OFF] if len(user_rows[-1]) > COL_FINAL_OFF else "0.0"

        # PH breakdown
        ph_entries = []
        ph_total = 0.0
        for r in user_rows:
            if len(r) > COL_HOLIDAY and (r[COL_HOLIDAY] or "").strip().lower() == "yes":
                app_date = r[COL_APP_DATE] if len(r) > COL_APP_DATE else "-"
                add_sub = r[COL_ADD_SUB] if len(r) > COL_ADD_SUB else "0"
                expiry = r[COL_EXPIRY] if len(r) > COL_EXPIRY else "N/A"
                remarks = r[COL_REMARKS] if len(r) > COL_REMARKS else ""
                ph_entries.append(f"• {app_date}: {add_sub} (exp {expiry}) {('- ' + remarks) if remarks else ''}")
                try:
                    # For total, sum deltas where Holiday Off == Yes
                    ph_total += float(add_sub)
                except Exception:
                    pass

        lines = [f"📊 Current Off Balance: {balance} day(s)."]
        if ph_entries:
            lines.append(f"🏖 PH Off Total: {ph_total:.1f} day(s)")
            lines.append("🔎 PH Off Entries:")
            lines.extend(ph_entries)
        else:
            lines.append("🏖 PH Off Total: 0.0 day(s)")

        await update.message.reply_text("\n".join(lines))
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > COL_TELEGRAM_ID and row[COL_TELEGRAM_ID] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            def rowline(row):
                ts = row[COL_TIMESTAMP] if len(row) > COL_TIMESTAMP else ""
                act = row[COL_ACTION] if len(row) > COL_ACTION else ""
                add = row[COL_ADD_SUB] if len(row) > COL_ADD_SUB else ""
                fin = row[COL_FINAL_OFF] if len(row) > COL_FINAL_OFF else ""
                appd= row[COL_APP_DATE] if len(row) > COL_APP_DATE else ""
                return f"{ts} | {act} | {add} → {fin} | AppDate: {appd}"
            response = "\n".join(rowline(r) for r in last_5)
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

# --- Entry commands (start conversation) ---
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text("🕒 How many days do you want to clock off? (0.5 to 3, in 0.5 increments)")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text("🧾 How many days do you want to claim off? (0.5 to 3, in 0.5 increments)")

async def clockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockphoff", "stage": "awaiting_days"}
    await update.message.reply_text("🏖 How many PH off days to clock? (0.5 to 3, in 0.5 increments)")

async def claimphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimphoff", "stage": "awaiting_days"}
    await update.message.reply_text("🏖 How many PH off days to claim? (0.5 to 3, in 0.5 increments)")

# Admin-only mass clock PH off
async def massclockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    # check admin
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admin_ids = {a.user.id for a in admins}
        if user.id not in admin_ids:
            await update.message.reply_text("⛔ Admins only.")
            return
    except Exception:
        await update.message.reply_text("⛔ Unable to verify admin status.")
        return

    user_state[user.id] = {"action": "massclockph", "stage": "awaiting_days", "group_id": chat.id}
    await update.message.reply_text("🏖 Mass clock PH Off — how many days? (0.5 to 3, in 0.5 increments)")

# --- Conversation state machine ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id not in user_state:
        return

    state = user_state[user_id]
    action = state["action"]

    # 1) days
    if state["stage"] == "awaiting_days":
        try:
            days = float(text)
            if days < 0.5 or days > 3 or (days * 10) % 5 != 0:
                raise ValueError()
            state["days"] = days
            state["stage"] = "awaiting_app_date"
            await update.message.reply_text("📅 Application Date? (YYYY-MM-DD)")
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")
        return

    # 2) application date
    if state["stage"] == "awaiting_app_date":
        app_date = parse_date_yyyy_mm_dd(text)
        if not app_date:
            await update.message.reply_text("❌ Invalid date. Please use YYYY-MM-DD (e.g., 2025-08-07).")
            return
        state["app_date"] = app_date
        state["stage"] = "awaiting_reason"
        await update.message.reply_text(f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")
        return

    # 3) reason
    if state["stage"] == "awaiting_reason":
        reason = text
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        state["reason"] = reason
        state["group_id"] = update.message.chat_id

        # Mass flow preview
        if action == "massclockph":
            await send_mass_ph_preview(update, context, state)
        else:
            await update.message.reply_text("📩 Your request has been submitted for approval.")
            await send_approval_request(update, context, state)

        user_state.pop(user_id, None)
        return

# --- Admin approval (single user flows) ---
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    group_id = state["group_id"]

    try:
        current_off = await get_current_off_for_user(str(user.id))
        delta = float(state["days"])
        new_off = current_off + delta if state["action"] in ("clockoff", "clockphoff") else current_off - delta

        is_holiday = state["action"] in ("clockphoff", "claimphoff")
        app_date = state["app_date"]
        expiry = compute_expiry_from(app_date) if is_holiday and state["action"] == "clockphoff" else "N/A"

        # create a short token to keep callback_data <= 64 bytes
        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": state["days"],
            "app_date": app_date,
            "reason": state["reason"],
            "group_id": group_id,
            "is_holiday": is_holiday,
        }
        admin_message_refs[token] = []

        admins = await context.bot.get_chat_administrators(group_id)
        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                msg = await context.bot.send_message(
                    chat_id=admin.user.id,
                    text=(
                        f"🆕 *{state['action'].title()} Request*\n\n"
                        f"👤 User: {user.full_name} ({user.id})\n"
                        f"📅 Days: {state['days']}\n"
                        f"📆 App Date: {app_date}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"🏷 Holiday Off: {'Yes' if is_holiday else 'No'}"
                        + (f"\n⏳ Expiry: {expiry}" if expiry != 'N/A' else "") +
                        f"\n\n📊 Current Off: {current_off:.1f} day(s)\n"
                        f"📈 New Balance: {new_off:.1f} day(s)\n\n"
                        "✅ Approve or ❌ Deny?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{token}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{token}")
                    ]])
                )
                admin_message_refs[token].append((admin.user.id, msg.message_id))
            except Exception as e:
                logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")
    except Exception:
        logger.exception("❌ Failed to fetch or notify admins")

# --- Mass PH preview and confirm ---
async def send_mass_ph_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    group_id = state["group_id"]
    days = state["days"]
    app_date = state["app_date"]
    reason = state["reason"]

    # Build target list from the sheet (unique Telegram IDs), skip headers/non-numeric IDs
    try:
        all_data = worksheet.get_all_values()
    except Exception:
        logger.exception("❌ Failed to read sheet for mass preview")
        await update.message.reply_text("❌ Failed to read the sheet.")
        return

    targets = {}
    for r in all_data:
        if len(r) <= COL_TELEGRAM_ID:
            continue
        uid = (r[COL_TELEGRAM_ID] or "").strip()
        name = (r[COL_NAME] if len(r) > COL_NAME else "").strip()

        headerish = {"telegram id", "id"}
        name_headerish = {"name", "user", "name (telegram id)"}

        if uid.lower() in headerish or name.lower() in name_headerish:
            continue
        if not uid.isdigit():
            continue

        targets[uid] = name or "-"

    target_pairs = sorted(((uid, targets[uid]) for uid in targets.keys()),
                          key=lambda x: (x[1].lower(), x[0]))
    count = len(target_pairs)

    if count == 0:
        await update.message.reply_text("⚠️ No users found in the sheet to mass clock.")
        return

    # Store mass request by token
    token = uuid.uuid4().hex[:10]
    pending_requests[token] = {
        "action": "massclockph_confirm",
        "group_id": group_id,
        "days": days,
        "app_date": app_date,
        "reason": reason,
        "targets": target_pairs,  # list of (uid, name)
    }

    preview_lines = ["🔎 *Mass PH Off Preview* (Name — Telegram ID)"]
    preview_lines += [f"- {name} ({uid})" for uid, name in target_pairs[:50]]
    if count > 50:
        preview_lines.append(f"... and {count - 50} more.")

    await update.message.reply_text(
        "\n".join(preview_lines) + f"\n\nDays: {days}\nApp Date: {app_date}\nReason: {reason}\n\nProceed?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"massconfirm|{token}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"masscancel|{token}")
        ]])
    )

# --- Callback handler ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        # Single-user approve/deny
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            # Distinguish mass request early
            if req.get("action") == "massclockph_confirm":
                await query.edit_message_text("⚠️ Use the mass confirm/cancel buttons for the mass request.")
                return

            user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or user_id
            action = req["action"]
            days = float(req["days"])
            app_date = req["app_date"]
            reason = req["reason"]
            group_id = int(req["group_id"])
            is_holiday = bool(req.get("is_holiday"))

            # Resolve display name for group announcement
            display_name = user_full_name or user_id
            try:
                member = await context.bot.get_chat_member(group_id, int(user_id))
                if member and member.user:
                    if member.user.full_name:
                        display_name = member.user.full_name
                    elif member.user.username:
                        display_name = f"@{member.user.username}"
            except Exception as e:
                logger.warning(f"⚠️ Could not resolve name for {user_id} in group {group_id}: {e}")

            if action_type == "approve":
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                current_off = await get_current_off_for_user(user_id)
                final = current_off + days if action in ("clockoff", "clockphoff") else current_off - days
                add_subtract = f"+{days}" if action in ("clockoff", "clockphoff") else f"-{days}"
                expiry = compute_expiry_from(app_date) if (is_holiday and action == "clockphoff") else "N/A"
                holiday_flag = "Yes" if is_holiday else "No"
                ph_total_val = f"{days}" if is_holiday else "N/A"

                # Append with new order:
                # Timestamp, Telegram ID, Name, Action, Current Off, Add/Subtract, Final Off,
                # Approved By, Application Date, Remarks, Holiday Off, PH Off Total, Expiry
                worksheet.append_row([
                    timestamp, user_id, display_name,
                    ("Clock Off" if action in ("clockoff", "clockphoff") else "Claim Off"),
                    f"{current_off:.1f}", add_subtract, f"{final:.1f}",
                    query.from_user.full_name, app_date, reason, holiday_flag, ph_total_val, expiry
                ])

                await query.edit_message_text("✅ Request approved and recorded.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=(
                        f"✅ {display_name}'s {('PH ' if is_holiday else '')}{action.replace('off',' Off')} "
                        f"approved by {query.from_user.full_name}.\n"
                        f"📅 Days: {days}\n"
                        f"📆 App Date: {app_date}"
                        + (f"\n⏳ Expiry: {expiry}" if expiry != "N/A" else "") +
                        f"\n📝 Reason: {reason}\n"
                        f"📊 Final: {final:.1f} day(s)"
                    )
                )
            else:
                await query.edit_message_text("❌ Request denied.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
                )

            # Clean up all admin messages for this token
            if token in admin_message_refs:
                for admin_id, msg_id in admin_message_refs[token]:
                    if admin_id != query.from_user.id:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=admin_id,
                                message_id=msg_id,
                                text=f"⚠️ Request already handled by {query.from_user.full_name}.",
                            )
                        except Exception:
                            pass
                del admin_message_refs[token]

            # Remove the pending request
            pending_requests.pop(token, None)
            return

        # Mass confirm/cancel
        if data.startswith("massconfirm|") or data.startswith("masscancel|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This mass request has expired or was already handled.")
                return

            if action_type == "masscancel":
                pending_requests.pop(token, None)
                await query.edit_message_text("🚫 Mass PH Off clocking canceled.")
                return

            # Confirm mass clock
            group_id = int(req["group_id"])
            days = float(req["days"])
            app_date = req["app_date"]
            reason = req["reason"]
            targets = req["targets"]  # list of (uid, name)
            expiry = compute_expiry_from(app_date)
            count_success = 0
            for uid, name in targets:
                try:
                    user_id = str(uid)
                    display_name = name or user_id
                    current_off = await get_current_off_for_user(user_id)
                    final = current_off + days
                    add_subtract = f"+{days}"

                    worksheet.append_row([
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, display_name,
                        "Clock Off",
                        f"{current_off:.1f}", add_subtract, f"{final:.1f}",
                        query.from_user.full_name, app_date, reason, "Yes", f"{days}", expiry
                    ])
                    count_success += 1
                except Exception as e:
                    logger.warning(f"⚠️ Failed to append for {uid}: {e}")

            pending_requests.pop(token, None)
            await query.edit_message_text(f"✅ Mass PH Off clocked for {count_success} member(s).")
            await context.bot.send_message(
                chat_id=group_id,
                text=f"🏖 PH Off clocked for {count_success} member(s).\nDays: {days} | App Date: {app_date} | Expiry: {expiry}"
            )
            return

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
    telegram_app.add_handler(CommandHandler("clockphoff", clockphoff))
    telegram_app.add_handler(CommandHandler("claimphoff", claimphoff))
    telegram_app.add_handler(CommandHandler("massclockphoff", massclockphoff))
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
