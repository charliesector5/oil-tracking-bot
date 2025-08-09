import os
import logging
import asyncio
import nest_asyncio
import gspread
import uuid
from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
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

# per-user conversation state
user_state = {}

# short-lived approval requests: token -> {...}
pending_requests = {}

# admin DM message refs for cleanup: token -> [(admin_id, msg_id)]
admin_message_refs = {}

# calendar sessions: token -> {...}
calendar_sessions = {}

MAX_REMARKS_LEN = 80

# --------------------------
# Helpers: keyboards
# --------------------------
def cancel_row(show_cancel: bool):
    if not show_cancel:
        return []
    return [InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")]

def build_calendar_keyboard(token: str, year: int, month: int, allow_manual: bool, show_cancel: bool):
    """
    Compact month grid. callback_data kept short:
      - day select:   cal|<tok>|<yyyymm>|<dd>
      - nav:          calnav|<tok>|prev  or  calnav|<tok>|next
      - manual input: calm|<tok>
      - cancel:       calcancel|<tok>
    """
    import calendar
    cal = calendar.Calendar(firstweekday=0)  # Monday=0
    yyyymm = f"{year:04d}{month:02d}"

    rows = []
    # Weekday header
    rows.append([InlineKeyboardButton(w, callback_data="noop") for w in ["Mo","Tu","We","Th","Fr","Sa","Su"]])

    # Weeks
    for week in cal.monthdayscalendar(year, month):
        btns = []
        for d in week:
            if d == 0:
                btns.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                btns.append(InlineKeyboardButton(
                    str(d),
                    callback_data=f"cal|{token}|{yyyymm}|{d:02d}"
                ))
        rows.append(btns)

    # Nav row
    rows.append([
        InlineKeyboardButton("«", callback_data=f"calnav|{token}|prev"),
        InlineKeyboardButton(f"{year}-{month:02d}", callback_data="noop"),
        InlineKeyboardButton("»", callback_data=f"calnav|{token}|next"),
    ])

    # Manual / Cancel row
    last_row = []
    if allow_manual:
        last_row.append(InlineKeyboardButton("✍️ Enter manually", callback_data=f"calm|{token}"))
    if show_cancel:
        last_row.append(InlineKeyboardButton("❌ Cancel", callback_data=f"calcancel|{token}"))
    if last_row:
        rows.append(last_row)

    return InlineKeyboardMarkup(rows)

async def send_month_calendar(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                              token: str, year: int, month: int,
                              allow_manual: bool, show_cancel: bool):
    kb = build_calendar_keyboard(token, year, month, allow_manual, show_cancel)
    await context.bot.send_message(
        chat_id=chat_id,
        text="📅 Select an application date:",
        reply_markup=kb
    )

def valid_date_str(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

# --------------------------
# Webhook
# --------------------------
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

# --------------------------
# Commands
# --------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n\n"
        "/clockoff – Request to clock normal OIL\n"
        "/claimoff – Request to claim normal OIL\n"
        "/summary – Your current balance\n"
        "/history – Your past 5 logs\n"
        "/startadmin – (PM only) Start admin DM session for approvals\n\n"
        "Tip: You can always tap ❌ Cancel or type -quit to abort.",
        parse_mode="Markdown"
    )

async def startadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("✅ Admin session started. I’ll DM you approval requests here.")
    else:
        await update.message.reply_text("ℹ️ Please PM me and send /startadmin there to start the admin session.")

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > 1 and row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[6]
            await update.message.reply_text(f"📊 Current Off Balance: {balance} day(s).")
        else:
            await update.message.reply_text("📊 No records found.")
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > 1 and row[1] == str(user.id)]
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
    uid = update.effective_user.id
    user_state[uid] = {"action": "clockoff", "stage": "awaiting_days", "group_id": update.message.chat_id}
    show_cancel = (update.effective_chat.type != "private")
    kb = InlineKeyboardMarkup([cancel_row(show_cancel)]) if show_cancel else None
    await update.message.reply_text(
        "🕒 How many days do you want to clock off? (0.5 to 3, in 0.5 steps)",
        reply_markup=kb
    )

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"action": "claimoff", "stage": "awaiting_days", "group_id": update.message.chat_id}
    show_cancel = (update.effective_chat.type != "private")
    kb = InlineKeyboardMarkup([cancel_row(show_cancel)]) if show_cancel else None
    await update.message.reply_text(
        "📄 How many days do you want to claim off? (0.5 to 3, in 0.5 steps)",
        reply_markup=kb
    )

# --------------------------
# Conversation handling
# --------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # universal quit
    if text.lower() == "-quit":
        user_state.pop(user_id, None)
        await update.message.reply_text("📕 Cancelled.")
        return

    if user_id not in user_state:
        return

    state = user_state[user_id]
    stage = state.get("stage")

    if stage == "awaiting_days":
        try:
            days = float(text)
            if days < 0.5 or days > 3 or (days * 10) % 5 != 0:
                raise ValueError()
            state["days"] = days
            # move to application date via calendar
            state["stage"] = "awaiting_app_date"
            token = uuid.uuid4().hex[:8]
            state["calendar_token"] = token
            # register calendar session separately
            calendar_sessions[token] = {
                "user_id": user_id,
                "chat_id": update.message.chat_id,
                "flow": state["action"],
            }
            # show calendar (no Cancel in PM)
            now = datetime.now()
            await send_month_calendar(
                context,
                update.message.chat_id,
                token,
                now.year,
                now.month,
                allow_manual=True,
                show_cancel=(update.effective_chat.type != "private"),
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")
        return

    if stage == "awaiting_app_date_typed":
        if not valid_date_str(text):
            await update.message.reply_text("❌ Invalid date. Please enter in YYYY-MM-DD.")
            return
        state["app_date"] = text
        state["stage"] = "awaiting_reason"
        await update.message.reply_text(f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")
        return

    if stage == "awaiting_reason":
        reason = text
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        state["reason"] = reason
        await update.message.reply_text("📩 Your request has been submitted for approval.")
        await send_approval_request(update, context, state)
        user_state.pop(user_id, None)
        return

# --------------------------
# Admin approval request
# --------------------------
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict):
    user = update.effective_user
    group_id = state["group_id"]
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > 1 and row[1] == str(user.id)]
        current_off = float(user_rows[-1][6]) if user_rows else 0.0
        delta = float(state["days"])
        new_off = current_off + delta if state["action"] == "clockoff" else current_off - delta

        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": state["days"],
            "reason": state["reason"],
            "group_id": group_id,
            "app_date": state.get("app_date"),
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
                        f"🆕 *{state['action'].title().replace('off',' Off')} Request*\n\n"
                        f"👤 User: {user.full_name}\n"
                        f"📅 Days: {state['days']}\n"
                        f"🗓 Application Date: {state.get('app_date','-')}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"📊 Current Off: {current_off:.1f} day(s)\n"
                        f"📈 New Balance (if approved): {new_off:.1f} day(s)\n\n"
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

# --------------------------
# Callback handler
# --------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        # -------- Calendar navigation & selection --------
        if data.startswith("cal|"):
            # cal|<tok>|<yyyymm>|<dd>
            _, tok, yyyymm, dd = data.split("|")
            sess = calendar_sessions.get(tok)
            if not sess:
                await query.edit_message_text("⚠️ Calendar session expired. Please start again.")
                return
            y = int(yyyymm[:4]); m = int(yyyymm[4:])
            d = int(dd)
            app_date = datetime(y, m, d).strftime("%Y-%m-%d")
            uid = sess["user_id"]
            st = user_state.get(uid)
            if not st:
                await query.edit_message_text("⚠️ Session ended. Please start again.")
                calendar_sessions.pop(tok, None)
                return
            st["app_date"] = app_date
            st["stage"] = "awaiting_reason"
            calendar_sessions.pop(tok, None)
            # keep the calendar message but mark chosen date
            await query.edit_message_text(f"📅 Selected: {app_date}")
            await context.bot.send_message(chat_id=sess["chat_id"], text=f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")
            return

        if data.startswith("calnav|"):
            # calnav|<tok>|prev|next
            _, tok, where = data.split("|")
            sess = calendar_sessions.get(tok)
            if not sess:
                await query.edit_message_text("⚠️ Calendar session expired. Please start again.")
                return
            # infer current title from message text or fallback to now
            title = query.message.reply_markup.inline_keyboard[-2][1].text if query.message.reply_markup else None
            try:
                cur_year, cur_month = map(int, title.split("-")) if title else (datetime.now().year, datetime.now().month)
            except Exception:
                cur_year, cur_month = (datetime.now().year, datetime.now().month)
            # compute new month
            base = datetime(cur_year, cur_month, 15)
            if where == "prev":
                new = base - timedelta(days=31)
            else:
                new = base + timedelta(days=31)
            kb = build_calendar_keyboard(tok, new.year, new.month, allow_manual=True,
                                         show_cancel=(query.message.chat.type != "private"))
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if data.startswith("calm|"):
            # switch to manual typing
            _, tok = data.split("|")
            sess = calendar_sessions.get(tok)
            if not sess:
                await query.edit_message_text("⚠️ Calendar session expired. Please start again.")
                return
            uid = sess["user_id"]
            st = user_state.get(uid)
            if st:
                st["stage"] = "awaiting_app_date_typed"
            calendar_sessions.pop(tok, None)
            await query.edit_message_text("✍️ Please type the application date in YYYY-MM-DD.")
            return

        if data.startswith("calcancel|"):
            _, tok = data.split("|")
            sess = calendar_sessions.pop(tok, None)
            if sess:
                user_state.pop(sess["user_id"], None)
            await query.edit_message_text("📕 Cancelled.")
            return

        # block "noop" buttons
        if data == "noop" or data == "cancel|flow":
            return

        # -------- Approval actions --------
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or user_id
            action = req["action"]
            days = float(req["days"])
            reason = req["reason"]
            group_id = int(req["group_id"])
            app_date = req.get("app_date") or datetime.now().strftime("%Y-%m-%d")

            if action_type == "approve":
                now = datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
                # fetch current
                all_data = worksheet.get_all_values()
                rows = [row for row in all_data if len(row) > 1 and row[1] == user_id]
                current_off = float(rows[-1][6]) if rows else 0.0
                final = current_off + days if action == "clockoff" else current_off - days
                add_subtract = f"+{days}" if action == "clockoff" else f"-{days}"

                # Append order:
                # Timestamp, Telegram ID, Name, Action, Current Off, Add/Subtract,
                # Final Off, Approved By, Application Date, Remarks, Holiday Off, Expiry
                worksheet.append_row([
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    user_id,
                    user_full_name,
                    "Clock Off" if action == "clockoff" else "Claim Off",
                    f"{current_off:.1f}",
                    add_subtract,
                    f"{final:.1f}",
                    query.from_user.full_name,
                    app_date,
                    reason,
                    "No",   # Holiday Off
                    "N/A"   # Expiry
                ])

                # Resolve display name for broadcast
                display_name = user_full_name
                try:
                    member = await context.bot.get_chat_member(group_id, int(user_id))
                    if member and member.user:
                        display_name = member.user.full_name or (f"@{member.user.username}" if member.user.username else display_name)
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {user_id} in group {group_id}: {e}")

                # Keep the admin PM message, but annotate result
                await query.edit_message_text(
                    f"✅ Approved.\n\n"
                    f"User: {display_name}\n"
                    f"Action: {'Clock Off' if action=='clockoff' else 'Claim Off'}\n"
                    f"Days: {days}\n"
                    f"Application Date: {app_date}\n"
                    f"Reason: {reason}\n"
                    f"Final Balance: {final:.1f}"
                )

                await context.bot.send_message(
                    chat_id=group_id,
                    text=(f"✅ {display_name}'s {action.replace('off',' Off')} approved by {query.from_user.full_name}.\n"
                          f"📅 Days: {days}\n"
                          f"🗓 Application Date: {app_date}\n"
                          f"📝 Reason: {reason}\n"
                          f"📊 Final: {final:.1f} day(s)")
                )
            else:
                # deny
                display_name = user_full_name
                try:
                    member = await context.bot.get_chat_member(group_id, int(user_id))
                    if member and member.user:
                        display_name = member.user.full_name or (f"@{member.user.username}" if member.user.username else display_name)
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {user_id} in group {group_id}: {e}")

                await query.edit_message_text(
                    f"❌ Denied.\n\n"
                    f"User: {display_name}\n"
                    f"Action: {'Clock Off' if action=='clockoff' else 'Claim Off'}\n"
                    f"Days: {days}\n"
                    f"Application Date: {app_date}\n"
                    f"Reason: {reason}"
                )
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
                )

            # Clean up other admin PMs for this token
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

            pending_requests.pop(token, None)
            return

    except Exception:
        logger.exception("❌ Failed to process callback")
        try:
            await query.edit_message_text("❌ Something went wrong.")
        except Exception:
            pass

# --------------------------
# Init & Run
# --------------------------
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
    telegram_app.add_handler(CommandHandler("startadmin", startadmin))
    telegram_app.add_handler(CommandHandler("summary", summary))
    telegram_app.add_handler(CommandHandler("history", history))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook set.")

if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    logger.info("🟢 Starting Flask...")
    app.run(host="0.0.0.0", port=10000)
