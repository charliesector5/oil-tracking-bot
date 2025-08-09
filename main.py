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
user_state = {}

# pending approval requests stored server-side to keep callback_data short
# token -> {user_id, user_full_name, action, days, reason, group_id, ...}
pending_requests = {}

# track admin PMs per token to clean up when one admin handles it
# token -> [(admin_id, message_id), ...]
admin_message_refs = {}

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

# --- Helpers ---
def parse_date_yyyy_mm_dd(s: str):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None

def make_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel|current")]])

def safe_name_from_member(member):
    if not member or not getattr(member, "user", None):
        return None
    u = member.user
    return u.full_name or (f"@{u.username}" if u.username else None)

# --- Commands (existing) ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n\n"
        "/clockoff – Request to clock normal OIL\n"
        "/claimoff – Request to claim normal OIL\n"
        "/clockphoff – Clock Public Holiday OIL (PH)\n"
        "/claimphoff – Claim Public Holiday OIL (PH)\n"
        "/massclockoff – Admin: Mass clock normal OIL for all\n"
        "/massclockphoff – Admin: Mass clock PH OIL for all (with preview)\n"
        "/summary – Your current balance & PH details\n"
        "/history – Your past 5 logs\n"
        "/newuser – Import your old records (normal + PH)\n"
        "/startadmin – Start admin session in PM\n"
        "/help – Show this help message\n\n"
        "Tip: You can always tap ❌ Cancel or type -quit to abort.",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) >= 3 and row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[6]
            # Optional: PH summary could be added if your current sheet logic supports it
            await update.message.reply_text(
                f"📊 Current Off Balance: {balance} day(s)."
            )
        else:
            await update.message.reply_text("📊 No records found.")
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) >= 9 and row[1] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            # Timestamp | Action | Add/Subtract → Final | Application Date
            response = "\n".join([
                f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | {row[8]}" for row in last_5
            ])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

# ==== NEW: /startadmin ====
async def startadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type != "private":
        await update.message.reply_text(
            "🔒 Please PM me to run /startadmin.\nOpen my profile and tap *Message*.",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text(
        "✅ Admin session started here.\nI’ll DM you approval requests and summaries.",
        reply_markup=make_cancel_keyboard()
    )

# ==== NEW: /newuser onboarding ====
# Flow:
#   ask_normal_days -> ask_ph_count -> loop (ask_ph_date_i -> ask_ph_reason_i) -> confirm -> send admin approval
# On approval:
#   1) Append one row for normal OIL import (if >0) with remarks 'Transfer from old record'
#   2) Append N rows for PH (Holiday Off Yes, Expiry = app_date + 365, Add/Subtract +0, Final Off unchanged)
MAX_REMARKS_LEN = 80

async def newuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {
        "flow": "onboarding",
        "stage": "ask_normal_days",
        "ph_total": 0,
        "ph_entries": [],  # list of {"date": date, "reason": str}
    }
    await update.message.reply_text(
        "🆕 *Onboarding: Import Old Records*\n\n"
        "1) How many *normal OIL days* to import? (Enter a number, e.g. 7.5 or 0 if none)",
        parse_mode="Markdown",
        reply_markup=make_cancel_keyboard()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Allow -quit to cancel any flow
    if update.message and update.message.text and update.message.text.strip().lower() == "-quit":
        uid = update.effective_user.id
        user_state.pop(uid, None)
        await update.message.reply_text("❌ Canceled.")
        return

    uid = update.effective_user.id
    message = (update.message.text or "").strip()

    # no active flow
    if uid not in user_state:
        return

    st = user_state[uid]

    # ===== Onboarding flow =====
    if st.get("flow") == "onboarding":
        if st["stage"] == "ask_normal_days":
            try:
                days = float(message)
                if days < 0:
                    await update.message.reply_text("❌ Please enter a non-negative number.")
                    return
                st["normal_days"] = days
                st["stage"] = "ask_ph_count"
                await update.message.reply_text(
                    "2) How many *PH OIL entries* to import? (Enter 0 if none)\n"
                    "_Each PH entry is 1 day with its own date and reason._",
                    parse_mode="Markdown",
                    reply_markup=make_cancel_keyboard()
                )
            except ValueError:
                await update.message.reply_text("❌ Invalid number. Please enter a number, e.g. 5 or 7.5.")
                return
            return

        if st["stage"] == "ask_ph_count":
            try:
                cnt = int(message)
                if cnt < 0:
                    await update.message.reply_text("❌ Please enter 0 or a positive integer.")
                    return
                st["ph_total"] = cnt
                if cnt == 0:
                    st["stage"] = "confirm_onboarding"
                    await show_onboarding_preview(update, context, st)
                else:
                    st["current_ph_index"] = 1
                    st["stage"] = "ask_ph_date"
                    await update.message.reply_text(
                        f"PH Entry {st['current_ph_index']}/{st['ph_total']} — "
                        "Enter *Application Date* (YYYY-MM-DD):",
                        parse_mode="Markdown",
                        reply_markup=make_cancel_keyboard()
                    )
            except ValueError:
                await update.message.reply_text("❌ Invalid integer. Please enter 0, 1, 2, ...")
            return

        if st["stage"] == "ask_ph_date":
            d = parse_date_yyyy_mm_dd(message)
            if not d:
                await update.message.reply_text("❌ Invalid date. Use YYYY-MM-DD.")
                return
            st["pending_ph_date"] = d
            st["stage"] = "ask_ph_reason"
            await update.message.reply_text(
                f"PH Entry {st['current_ph_index']}/{st['ph_total']} — "
                f"Enter *Reason* (max {MAX_REMARKS_LEN} chars):",
                parse_mode="Markdown",
                reply_markup=make_cancel_keyboard()
            )
            return

        if st["stage"] == "ask_ph_reason":
            reason = message
            if len(reason) > MAX_REMARKS_LEN:
                reason = reason[:MAX_REMARKS_LEN]
                await update.message.reply_text(f"✂️ Reason trimmed to {MAX_REMARKS_LEN} characters.")
            st["ph_entries"].append({
                "date": st["pending_ph_date"],
                "reason": reason
            })
            idx = st["current_ph_index"] + 1
            if idx <= st["ph_total"]:
                st["current_ph_index"] = idx
                st["stage"] = "ask_ph_date"
                await update.message.reply_text(
                    f"PH Entry {st['current_ph_index']}/{st['ph_total']} — "
                    "Enter *Application Date* (YYYY-MM-DD):",
                    parse_mode="Markdown",
                    reply_markup=make_cancel_keyboard()
                )
            else:
                st["stage"] = "confirm_onboarding"
                await show_onboarding_preview(update, context, st)
            return

        if st["stage"] == "confirm_onboarding":
            # accept simple "yes/no" if typed, otherwise ignore (buttons are in use)
            if message.lower() in ("yes", "y"):
                await submit_onboarding_for_approval(update, context, st)
                user_state.pop(uid, None)
                return
            if message.lower() in ("no", "n"):
                await update.message.reply_text("❌ Canceled.")
                user_state.pop(uid, None)
                return
            # else ignore. Users should use the inline buttons.

            return

    # ===== Other existing simple flows (if any) could be here =====
    # No-op for this file since we’re focusing on onboarding add

async def show_onboarding_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    uid = update.effective_user.id
    uname = update.effective_user.full_name
    normal_days = st.get("normal_days", 0.0)
    ph_entries = st.get("ph_entries", [])

    lines = [
        f"👤 *{uname}* (ID: {uid})",
        f"• Normal OIL to import: *{normal_days}* day(s) — _Reason: Transfer from old record_"
    ]
    if ph_entries:
        lines.append(f"• PH entries: *{len(ph_entries)}*")
        for i, e in enumerate(ph_entries, 1):
            lines.append(f"  {i}. {e['date']} — {e['reason']}")
    else:
        lines.append("• PH entries: *0*")

    await update.message.reply_text(
        "🧾 *Onboarding Preview*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Submit for Admin Approval", callback_data="ob_submit|go")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel|current")]
        ])
    )

async def submit_onboarding_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    user = update.effective_user
    chat_id = update.effective_chat.id  # group or private where initiated
    group_id = chat_id if update.effective_chat.type in ("group", "supergroup") else None
    # If started in PM, try to remember last group? For now, require onboarding start in group.
    if not group_id:
        await update.message.reply_text("⚠️ Please start /newuser inside the group so admins can be notified.")
        return

    # build token payload
    token = uuid.uuid4().hex[:10]
    pending_requests[token] = {
        "type": "onboard",
        "user_id": user.id,
        "user_full_name": user.full_name,
        "group_id": group_id,
        "normal_days": float(st.get("normal_days", 0.0)),
        "ph_entries": [
            {"date": e["date"].strftime("%Y-%m-%d"), "reason": e["reason"]}
            for e in st.get("ph_entries", [])
        ],
    }
    admin_message_refs[token] = []

    # send preview to admins
    try:
        admins = await context.bot.get_chat_administrators(group_id)
        # compute current and predicted balances just for display
        all_data = worksheet.get_all_values()
        rows = [row for row in all_data if len(row) >= 7 and row[1] == str(user.id)]
        current_off = float(rows[-1][6]) if rows else 0.0
        final_after_normal = current_off + float(st.get("normal_days", 0.0))

        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                lines = [
                    f"🆕 *Onboarding Import Request*",
                    f"👤 User: {user.full_name} ({user.id})",
                    f"• Normal OIL to import: *{st.get('normal_days', 0.0)}* day(s)",
                    f"  Reason: _Transfer from old record_",
                    f"  Balance: {current_off:.1f} → {final_after_normal:.1f}",
                ]
                ph_entries = st.get("ph_entries", [])
                if ph_entries:
                    lines.append(f"• PH entries: *{len(ph_entries)}*")
                    for i, ent in enumerate(ph_entries, 1):
                        # Show expiry as date + 365
                        d = parse_date_yyyy_mm_dd(ent["date"])
                        exp = (d + timedelta(days=365)).isoformat() if d else "N/A"
                        lines.append(f"  {i}. {ent['date']} (exp {exp}) — {ent['reason']}")
                else:
                    lines.append("• PH entries: *0*")

                msg = await context.bot.send_message(
                    chat_id=admin.user.id,
                    text="\n".join(lines),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve", callback_data=f"obapprove|{token}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"obdeny|{token}")
                    ]])
                )
                admin_message_refs[token].append((admin.user.id, msg.message_id))
            except Exception as e:
                logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")

        await update.message.reply_text("📩 Submitted to admins for approval.")
    except Exception:
        logger.exception("❌ Failed to notify admins")
        await update.message.reply_text("❌ Could not submit to admins. Please try again later.")

# --- Existing quick commands to show we still handle them (stubs/no changes here) ---
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text(
        "🕒 How many days do you want to *clock off*? (0.5 to 3, in 0.5 increments)",
        parse_mode="Markdown",
        reply_markup=make_cancel_keyboard()
    )

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text(
        "🧾 How many days do you want to *claim off*? (0.5 to 3, in 0.5 increments)",
        parse_mode="Markdown",
        reply_markup=make_cancel_keyboard()
    )

# --- Callback handler (includes onboarding + cancel) ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        # universal cancel
        if data.startswith("cancel|"):
            uid = query.from_user.id
            user_state.pop(uid, None)
            await query.edit_message_text("❌ Canceled.")
            return

        # onboarding submit button
        if data == "ob_submit|go":
            # Convert the reply into a submit; we need the user's latest state
            uid = query.from_user.id
            st = user_state.get(uid)
            if st and st.get("flow") == "onboarding" and st.get("stage") == "confirm_onboarding":
                # Attempt to find the latest message chat (group) id is tricky from callback; skip and notify
                await query.edit_message_text("⚠️ Please send /newuser again in the group to submit.")
            else:
                await query.edit_message_text("⚠️ Onboarding session not found.")
            return

        # onboarding approval/deny
        if data.startswith("obapprove|") or data.startswith("obdeny|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            if req.get("type") != "onboard":
                await query.edit_message_text("⚠️ Invalid request type.")
                return

            user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or user_id
            group_id = int(req["group_id"])
            normal_days = float(req.get("normal_days", 0.0))
            ph_entries = req.get("ph_entries", [])

            if action_type == "obapprove":
                # Append rows to sheet
                now = datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

                # Get current balance
                all_data = worksheet.get_all_values()
                rows = [row for row in all_data if len(row) >= 7 and row[1] == user_id]
                current_off = float(rows[-1][6]) if rows else 0.0
                final = current_off

                appended_lines = []

                # 1) Normal OIL import (if any): affects balance
                if normal_days > 0:
                    add_subtract = f"+{normal_days}"
                    final = current_off + normal_days
                    # Columns: Timestamp, Telegram ID, Name, Action, Current Off, Add/Subtract,
                    #          Final Off, Approved By, Application Date, Remarks, Holiday Off, Expiry
                    worksheet.append_row([
                        now.strftime("%Y-%m-%d"),  # Timestamp (date-only ok)
                        user_id,
                        user_full_name,
                        "Clock Off",
                        f"{current_off:.1f}",
                        add_subtract,
                        f"{final:.1f}",
                        query.from_user.full_name,
                        now.strftime("%Y-%m-%d"),  # Application Date (today for import)
                        "Transfer from old record",
                        "No",                      # Holiday Off
                        "N/A"                      # Expiry
                    ])
                    appended_lines.append(f"• Normal OIL: +{normal_days} (→ {final:.1f})")
                    current_off = final  # step forward

                # 2) PH entries (each 1 day, does NOT change normal balance; mark Holiday Off=Yes)
                for ent in ph_entries:
                    d = parse_date_yyyy_mm_dd(ent["date"])
                    app_date_str = d.strftime("%Y-%m-%d") if d else now.strftime("%Y-%m-%d")
                    expiry = (d + timedelta(days=365)).strftime("%Y-%m-%d") if d else "N/A"
                    # keep Current/Final unchanged, Add/Subtract +0 to avoid normal balance changes
                    worksheet.append_row([
                        now.strftime("%Y-%m-%d"),
                        user_id,
                        user_full_name,
                        "Clock Off",
                        f"{current_off:.1f}",
                        "+0",
                        f"{current_off:.1f}",
                        query.from_user.full_name,
                        app_date_str,
                        ent["reason"][:MAX_REMARKS_LEN],
                        "Yes",         # Holiday Off
                        expiry
                    ])
                    appended_lines.append(f"• PH: {app_date_str} (exp {expiry}) — {ent['reason'][:MAX_REMARKS_LEN]}")

                # Acknowledge to admin PM (retain details)
                if appended_lines:
                    await query.edit_message_text(
                        "✅ Onboarding approved and recorded.\n\n" + "\n".join(appended_lines)
                    )
                else:
                    await query.edit_message_text("✅ Onboarding approved (no rows added).")

                # Announce in group with a nice name
                display_name = user_full_name
                try:
                    member = await context.bot.get_chat_member(group_id, int(user_id))
                    resolved = safe_name_from_member(member)
                    if resolved:
                        display_name = resolved
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {user_id} in group {group_id}: {e}")

                await context.bot.send_message(
                    chat_id=group_id,
                    text=(
                        f"✅ Onboarding import approved for {display_name} by {query.from_user.full_name}.\n" +
                        ("\n".join(appended_lines) if appended_lines else "No rows added.")
                    )
                )

            else:
                # Denied
                await query.edit_message_text("❌ Onboarding request denied.")
                await context.bot.send_message(
                    chat_id=int(req["group_id"]),
                    text=f"❌ Onboarding request for {user_full_name} was denied by {query.from_user.full_name}."
                )

            # Clean up other admin PMs
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

        # (Other approve/deny handlers for existing requests would live here)

    except Exception:
        logger.exception("❌ Failed to process callback")
        try:
            await query.edit_message_text("❌ Something went wrong.")
        except Exception:
            pass

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
    telegram_app.add_handler(CommandHandler("newuser", newuser))
    telegram_app.add_handler(CommandHandler("startadmin", startadmin))

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
