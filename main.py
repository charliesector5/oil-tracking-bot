import os
import logging
import asyncio
import nest_asyncio
import gspread
import uuid
import calendar
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
from datetime import datetime, timedelta, date as dt_date

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
# user_state[user_id] = { action, stage, days, app_date, reason, group_id, calendar_year, calendar_month, calendar_message_id }
user_state = {}

# pending approval requests stored server-side to keep callback_data short
# token -> {user_id, user_full_name, action, days, reason, group_id, app_date, (mass=True, targets=[...])}
pending_requests = {}

# track admin PMs per token to clean up when one admin handles it
# token -> [(admin_id, message_id), ...]
admin_message_refs = {}

# Sheet column indices (after append)
COL_TIMESTAMP = 0
COL_TELEGRAM_ID = 1
COL_NAME = 2
COL_ACTION = 3
COL_CURRENT_OFF = 4
COL_ADD_SUB = 5
COL_FINAL_OFF = 6
COL_APPROVED_BY = 7
COL_APP_DATE = 8
COL_REMARKS = 9
COL_HOLIDAY_OFF = 10
COL_EXPIRY = 11
COL_PH_OFF_TOTAL = 12

# --- Helpers: Calendar UI ---
def _build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    cal = calendar.Calendar(firstweekday=0)  # Monday
    month_days = cal.monthdayscalendar(year, month)

    header = f"{calendar.month_name[month]} {year}"
    keyboard = [[InlineKeyboardButton(text=header, callback_data="noop")]]

    keyboard.append([InlineKeyboardButton(d, callback_data="noop") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])

    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                day_str = f"{year:04d}-{month:02d}-{day:02d}"
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal_pick|{day_str}"))
        keyboard.append(row)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    keyboard.append([
        InlineKeyboardButton("◀ Prev", callback_data=f"cal_prev|{prev_year:04d}-{prev_month:02d}"),
        InlineKeyboardButton("Type Manually", callback_data="cal_type"),
        InlineKeyboardButton("Next ▶", callback_data=f"cal_next|{next_year:04d}-{next_month:02d}")
    ])
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(keyboard)

def _valid_yyyy_mm_dd(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

def _safe_float(s, default=0.0):
    try:
        return float(s)
    except Exception:
        return default

async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return bool(member and (member.status in ("administrator", "creator")))
    except Exception:
        return False

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

# --- Commands ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n\n"
        "/clockoff – Request to clock OIL\n"
        "/claimoff – Request to claim OIL\n"
        "/clockphoff – Clock *Public Holiday* OIL (PH Off)\n"
        "/claimphoff – Claim *Public Holiday* OIL (PH Off)\n"
        "/massclockphoff – Admin: mass clock PH Off for all users found in the sheet\n"
        "/summary – See OIL & PH OIL balances and your PH entries\n"
        "/history – See your past 5 OIL logs\n"
        "/help – Show this help message",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) >= 7 and row[COL_TELEGRAM_ID] == str(user.id)]
        if not user_rows:
            await update.message.reply_text("📊 No records found.")
            return

        last_row = user_rows[-1]
        normal_balance = last_row[COL_FINAL_OFF]
        ph_total = last_row[COL_PH_OFF_TOTAL] if len(last_row) > COL_PH_OFF_TOTAL else "0"

        today = dt_date.today()
        active_entries = []
        for r in user_rows:
            try:
                is_ph_row = (len(r) > COL_HOLIDAY_OFF and r[COL_HOLIDAY_OFF].strip().lower() == "yes")
                is_ph_clock = (len(r) > COL_ACTION and r[COL_ACTION].startswith("Clock Off (PH)"))
                expiry_str = r[COL_EXPIRY] if len(r) > COL_EXPIRY else "N/A"
                if is_ph_row and is_ph_clock and expiry_str and expiry_str != "N/A":
                    exp_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                    if exp_dt >= today:
                        app_date = r[COL_APP_DATE] if len(r) > COL_APP_DATE else "-"
                        remark = r[COL_REMARKS] if len(r) > COL_REMARKS else ""
                        active_entries.append((app_date, expiry_str, remark))
            except Exception:
                continue

        lines = [
            f"📊 Balances:",
            f"• Normal Off: {normal_balance} day(s)",
            f"• PH Off: {ph_total} day(s)"
        ]
        if active_entries:
            lines.append("\n🏝️ Your active PH entries:")
            for app, exp, rem in active_entries:
                rem_part = f" — {rem}" if rem else ""
                lines.append(f"• {app} → expires {exp}{rem_part}")
        else:
            lines.append("\n🏝️ No active PH entries.")

        await update.message.reply_text("\n".join(lines))
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if row[COL_TELEGRAM_ID] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            response = "\n".join([
                f"{row[COL_TIMESTAMP]} | {row[COL_ACTION]} | {row[COL_ADD_SUB]} → {row[COL_FINAL_OFF]} | App: {row[COL_APP_DATE]} | {row[COL_REMARKS]}"
                for row in last_5
            ])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text("🕒 How many do you want to *clock off*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text("🧾 How many do you want to *claim off*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

async def clockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "clockph", "stage": "awaiting_days"}
    await update.message.reply_text("🏝️ How many *PH Off* days to *clock*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

async def claimphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_user.id] = {"action": "claimph", "stage": "awaiting_days"}
    await update.message.reply_text("🏝️ How many *PH Off* days to *claim*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

# --- NEW: Admin mass clock PH Off ---
async def massclockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await _is_admin(context, chat_id, user_id):
        await update.message.reply_text("⛔ Only admins can use /massclockphoff.")
        return
    user_state[user_id] = {"action": "massclockph", "stage": "awaiting_days", "group_id": chat_id}
    await update.message.reply_text("🏝️ Admin: How many *PH Off* days to *clock for everyone*? (0.5 to 3, in 0.5 increments)", parse_mode="Markdown")

# --- Conversation state machine ---
MAX_REMARKS_LEN = 80

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
            state["stage"] = "awaiting_date"
            today = dt_date.today()
            state["calendar_year"] = today.year
            state["calendar_month"] = today.month
            sent = await update.message.reply_text(
                "📅 Select the *Application Date* from the calendar below, or tap *Type Manually* to enter a date (YYYY-MM-DD).",
                parse_mode="Markdown",
                reply_markup=_build_calendar(today.year, today.month)
            )
            state["calendar_message_id"] = sent.message_id
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")

    elif state["stage"] == "awaiting_manual_date":
        if not _valid_yyyy_mm_dd(message):
            await update.message.reply_text("❌ Invalid date format. Please use YYYY-MM-DD.")
            return
        state["app_date"] = message
        state["stage"] = "awaiting_reason"
        await update.message.reply_text(f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")

    elif state["stage"] == "awaiting_reason":
        reason = message
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        state["reason"] = reason

        # For personal flows, save group id from the message chat
        if "group_id" not in state:
            state["group_id"] = update.message.chat_id

        await update.message.reply_text("📩 Your request has been submitted for approval.")
        await send_approval_request(update, context, state)
        user_state.pop(user_id, None)

# --- Admin approval ---
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    group_id = state["group_id"]
    try:
        all_data = worksheet.get_all_values()

        if state["action"] == "massclockph":
            # Build target list from the sheet (unique Telegram IDs)
            targets = {}
            for r in all_data:
                if len(r) > COL_TELEGRAM_ID and r[COL_TELEGRAM_ID].strip():
                    uid = r[COL_TELEGRAM_ID].strip()
                    name = r[COL_NAME] if len(r) > COL_NAME else ""
                    targets[uid] = name
            target_pairs = [(uid, targets[uid]) for uid in targets.keys()]
            count = len(target_pairs)

            token = uuid.uuid4().hex[:10]
            pending_requests[token] = {
                "user_id": user.id,
                "user_full_name": user.full_name,
                "action": state["action"],
                "days": state["days"],
                "reason": state["reason"],
                "group_id": group_id,
                "app_date": state.get("app_date", ""),
                "mass": True,
                "targets": target_pairs,  # list of (telegram_id_str, name_str)
            }
            admin_message_refs[token] = []

            # PM only the invoking admin for approval (avoid spamming all admins)
            try:
                msg = await context.bot.send_message(
                    chat_id=user.id,
                    text=(
                        f"🆕 *Mass Clock PH Off* Request\n\n"
                        f"👤 By: {user.full_name}\n"
                        f"👥 Targets: {count} users (from sheet)\n"
                        f"📅 Days: {state['days']}\n"
                        f"📅 Application Date: {state.get('app_date','') or '-'}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        "Choose: *Preview*, *Approve* or *Deny*."
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("👀 Preview", callback_data=f"preview|{token}"),
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{token}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{token}")
                    ]])
                )
                admin_message_refs[token].append((user.id, msg.message_id))
            except Exception as e:
                logger.warning(f"⚠️ Cannot PM admin {user.id}: {e}")
            return

        # Personal request path (existing)
        user_rows = [row for row in all_data if len(row) >= 7 and row[COL_TELEGRAM_ID] == str(user.id)]
        current_off = _safe_float(user_rows[-1][COL_FINAL_OFF]) if user_rows else 0.0

        delta = float(state["days"])
        is_ph = state["action"] in ("clockph", "claimph")

        if is_ph:
            new_off = current_off
            last_ph = _safe_float(user_rows[-1][COL_PH_OFF_TOTAL], 0.0) if (user_rows and len(user_rows[-1]) > COL_PH_OFF_TOTAL) else 0.0
            new_ph = last_ph + delta if state["action"] == "clockph" else last_ph - delta
            ph_note = f"\n🏝️ PH Off Total → {last_ph:.1f} ➜ {new_ph:.1f}"
        else:
            new_off = current_off + delta if state["action"] == "clockoff" else current_off - delta
            ph_note = ""

        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": state["days"],
            "reason": state["reason"],
            "group_id": group_id,
            "app_date": state.get("app_date", ""),
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
                        f"🆕 *{state['action'].replace('ph',' PH').title()}* Request\n\n"
                        f"👤 User: {user.full_name}\n"
                        f"📅 Days: {state['days']}\n"
                        f"📅 Application Date: {state.get('app_date','') or '-'}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"📊 Current Off: {current_off:.1f} day(s)\n"
                        f"📈 New Balance: {new_off:.1f} day(s){ph_note}\n\n"
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

# --- Callback handler ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    cb_from_user_id = query.from_user.id

    try:
        # Calendar nav/selection for conversational users
        st = user_state.get(cb_from_user_id)
        if data.startswith("cal_prev|") or data.startswith("cal_next|"):
            if not st or st.get("stage") != "awaiting_date":
                return
            _, y_m = data.split("|", maxsplit=1)
            y, m = map(int, y_m.split("-"))
            st["calendar_year"], st["calendar_month"] = y, m
            await query.edit_message_reply_markup(reply_markup=_build_calendar(y, m))
            return

        if data.startswith("cal_pick|"):
            if not st or st.get("stage") != "awaiting_date":
                return
            _, picked = data.split("|", maxsplit=1)
            if not _valid_yyyy_mm_dd(picked):
                await query.edit_message_text("❌ Invalid date picked.")
                return
            st["app_date"] = picked
            st["stage"] = "awaiting_reason"
            await query.edit_message_text(
                f"📅 Application Date selected: *{picked}*.\n\n📝 Now enter the *reason* (max {MAX_REMARKS_LEN} chars).",
                parse_mode="Markdown"
            )
            return

        if data == "cal_type":
            if not st or st.get("stage") != "awaiting_date":
                return
            st["stage"] = "awaiting_manual_date"
            await query.edit_message_text("⌨️ Please *type* the Application Date in the format: YYYY-MM-DD", parse_mode="Markdown")
            return

        if data == "cal_cancel":
            if st:
                user_state.pop(cb_from_user_id, None)
            await query.edit_message_text("❎ Cancelled.")
            return

        # Preview for mass
        if data.startswith("preview|"):
            _, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req or not req.get("mass"):
                await query.edit_message_text("⚠️ Nothing to preview for this request.")
                return

            targets = req.get("targets", [])
            if not targets:
                await context.bot.send_message(chat_id=cb_from_user_id, text="(No targets found in sheet.)")
            else:
                # chunk preview under Telegram 4096 limit
                lines = [f"👀 Preview ({len(targets)} users):"]
                chunks = []
                current = ""
                for uid, name in targets:
                    line = f"- {name or '-'} ({uid})\n"
                    if len(current) + len(line) > 3500:  # safety margin
                        chunks.append(current)
                        current = line
                    else:
                        current += line
                if current:
                    chunks.append(current)

                for i, ch in enumerate(chunks, 1):
                    header = f"👀 Preview {i}/{len(chunks)}"
                    await context.bot.send_message(chat_id=cb_from_user_id, text=f"{header}\n{ch}")

            # keep the original message with buttons
            return

        # Approvals / Denials (mass & personal)
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            is_mass = req.get("mass", False)

            if is_mass:
                # Mass PH clock
                if action_type == "approve":
                    app_date = req.get("app_date", "")
                    try:
                        app_dt = datetime.strptime(app_date, "%Y-%m-%d").date()
                        expiry_val = (app_dt + timedelta(days=365)).strftime("%Y-%m-%d")
                    except Exception:
                        expiry_val = "N/A"

                    targets = req.get("targets", [])
                    days = float(req["days"])
                    reason = req["reason"]
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    appended = 0

                    all_data = worksheet.get_all_values()
                    last_by_user = {}
                    for r in all_data:
                        if len(r) > COL_TELEGRAM_ID and r[COL_TELEGRAM_ID].strip():
                            last_by_user[r[COL_TELEGRAM_ID]] = r

                    for uid, name in targets:
                        last_row = last_by_user.get(uid)
                        current_off = _safe_float(last_row[COL_FINAL_OFF]) if last_row else 0.0
                        last_ph_total = _safe_float(last_row[COL_PH_OFF_TOTAL], 0.0) if (last_row and len(last_row) > COL_PH_OFF_TOTAL) else 0.0
                        new_ph_total = last_ph_total + days

                        worksheet.append_row([
                            timestamp,
                            uid,
                            name or "-",
                            "Clock Off (PH)",
                            f"{current_off:.1f}",
                            "+0",
                            f"{current_off:.1f}",
                            query.from_user.full_name,
                            app_date or "-",
                            reason,
                            "Yes",
                            expiry_val,
                            f"{new_ph_total:.1f}"
                        ])
                        appended += 1

                    await query.edit_message_text(f"✅ Mass PH Off clocked for {appended} users.")
                    await context.bot.send_message(
                        chat_id=req["group_id"],
                        text=(
                            f"✅ *Mass Clock PH Off* approved by {query.from_user.full_name}.\n"
                            f"👥 Users updated: {appended}\n"
                            f"📅 Days: {days}\n"
                            f"📅 Application Date: {app_date or '-'}\n"
                            f"📝 Reason: {reason}"
                        ),
                        parse_mode="Markdown"
                    )
                else:
                    await query.edit_message_text("❌ Mass request denied.")
                    await context.bot.send_message(
                        chat_id=req["group_id"],
                        text=f"❌ Mass PH Off clock was denied by {query.from_user.full_name}."
                    )

                # Clean up admin PMs for this token
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

            # --- Personal flows (existing) ---
            req_user_id = str(req["user_id"])
            user_full_name = req.get("user_full_name") or req_user_id
            action = req["action"]  # 'clockoff' | 'claimoff' | 'clockph' | 'claimph'
            days = float(req["days"])
            reason = req["reason"]
            group_id = int(req["group_id"])
            app_date = req.get("app_date", "")

            all_data = worksheet.get_all_values()
            rows = [row for row in all_data if len(row) >= 7 and row[COL_TELEGRAM_ID] == req_user_id]
            current_off = _safe_float(rows[-1][COL_FINAL_OFF]) if rows else 0.0
            last_ph_total = _safe_float(rows[-1][COL_PH_OFF_TOTAL], 0.0) if (rows and len(rows[-1]) > COL_PH_OFF_TOTAL) else 0.0

            is_ph = action in ("clockph", "claimph")

            if action_type == "approve":
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if is_ph:
                    final_off = current_off
                    add_subtract = "+0"
                    holiday_flag = "Yes"
                    new_ph_total = last_ph_total + days if action == "clockph" else last_ph_total - days
                    expiry = "N/A"
                    if action == "clockph":
                        try:
                            app_dt = datetime.strptime(app_date, "%Y-%m-%d").date()
                            expiry = (app_dt + timedelta(days=365)).strftime("%Y-%m-%d")
                        except Exception:
                            expiry = "N/A"
                else:
                    final_off = current_off + days if action == "clockoff" else current_off - days
                    add_subtract = f"+{days}" if action == "clockoff" else f"-{days}"
                    holiday_flag = "No"
                    expiry = "N/A"
                    new_ph_total = last_ph_total

                worksheet.append_row([
                    timestamp,
                    req_user_id,
                    user_full_name,
                    ("Clock Off" if action in ("clockoff", "clockph") else "Claim Off") + (" (PH)" if is_ph else ""),
                    f"{current_off:.1f}",
                    add_subtract,
                    f"{final_off:.1f}",
                    query.from_user.full_name,
                    app_date or "-",
                    reason,
                    holiday_flag,
                    expiry,
                    f"{new_ph_total:.1f}"
                ])

                display_name = user_full_name or req_user_id
                try:
                    member = await context.bot.get_chat_member(group_id, int(req_user_id))
                    if member and member.user:
                        if member.user.full_name:
                            display_name = member.user.full_name
                        elif member.user.username:
                            display_name = f"@{member.user.username}"
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {req_user_id} in group {group_id}: {e}")

                await query.edit_message_text("✅ Request approved and recorded.")
                details = [
                    f"✅ {display_name}'s {(action.replace('ph',' PH').replace('off',' Off'))} approved by {query.from_user.full_name}.",
                    f"📅 Days: {days}",
                    f"📅 Application Date: {app_date or '-'}",
                    f"📝 Reason: {reason}",
                ]
                if is_ph:
                    details.append(f"🏝️ PH Off Total: {new_ph_total:.1f} day(s)")
                    if expiry != "N/A":
                        details.append(f"⏳ Expiry: {expiry}")
                    details.append(f"📊 Final Off (normal): {final_off:.1f} day(s)")
                else:
                    details.append(f"📊 Final Off: {final_off:.1f} day(s)")

                await context.bot.send_message(chat_id=group_id, text="\n".join(details))
            else:
                display_name = user_full_name or req_user_id
                try:
                    member = await context.bot.get_chat_member(group_id, int(req_user_id))
                    if member and member.user:
                        if member.user.full_name:
                            display_name = member.user.full_name
                        elif member.user.username:
                            display_name = f"@{member.user.username}"
                except Exception as e:
                    logger.warning(f"⚠️ Could not resolve name for {req_user_id} in group {group_id}: {e}")

                await query.edit_message_text("❌ Request denied.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"❌ {display_name}'s {(action.replace('ph',' PH').replace('off',' Off'))} was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
                )

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
