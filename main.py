import os
import logging
import asyncio
import nest_asyncio
import gspread
import uuid
import calendar
from datetime import datetime, timedelta, date as date_cls

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

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# Env
# =========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

# =========================
# Flask
# =========================
app = Flask(__name__)

@app.route('/')
def index():
    return "✅ Oil Tracking Bot is up."

@app.route('/health')
def health():
    return "✅ Health check passed."

# =========================
# Globals
# =========================
telegram_app = None
worksheet = None

loop = asyncio.new_event_loop()
executor = ThreadPoolExecutor()

# Per-user conversation state
user_state = {}  # {user_id: {...}}

# Tokenized approval requests (to keep callback_data short)
# token -> {user_id, user_full_name, action, days, reason, group_id, app_date, is_ph}
pending_requests = {}

# Track admin PM messages per token so we can fan-out edit when handled
# token -> [(admin_id, message_id), ...]
admin_message_refs = {}

# Calendar sessions (for date picking)
# token -> {user_id, year, month, purpose, action, days, reason, group_id}
calendar_sessions = {}

# Mass actions: preview & confirm
# token -> {admin_id, group_id, is_ph, days, app_date, reason, members:[{id,name}], stage}
mass_requests = {}

# =========================
# Constants
# =========================
MAX_REMARKS_LEN = 80
MIN_DAYS = 0.5
MAX_DAYS = 3.0

# =========================
# Helpers
# =========================

def is_admin(ctx: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int) -> bool:
    # NOTE: We'll check at runtime in handlers, but this helper is here if needed
    return True  # We still verify in the async path; this is a placeholder signature.

def valid_days_str(s: str) -> bool:
    try:
        v = float(s)
        if v <= 0:  # disallow zero/negative
            return False
        if v < MIN_DAYS or v > MAX_DAYS:
            return False
        # Steps of 0.5
        return (v * 10) % 5 == 0
    except Exception:
        return False

def parse_manual_date(s: str) -> str | None:
    # Expect YYYY-MM-DD
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").date()
        return dt.isoformat()
    except Exception:
        return None

def month_calendar_kb(cal_token: str, year: int, month: int, include_manual=True, include_cancel=True) -> InlineKeyboardMarkup:
    # Build a full month grid with Prev/Next and optional Manual/Cancel
    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdatescalendar(year, month)

    header = [InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data=f"noop|{cal_token}")]
    nav = [
        InlineKeyboardButton("« Prev", callback_data=f"cal|{cal_token}|NAV|{year}-{month}-prev"),
        InlineKeyboardButton("Next »", callback_data=f"cal|{cal_token}|NAV|{year}-{month}-next"),
    ]

    rows = [header, nav]

    # Weekday labels
    wd_row = [InlineKeyboardButton(d, callback_data=f"noop|{cal_token}") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]]
    rows.append(wd_row)

    for week in weeks:
        btns = []
        for d in week:
            label = str(d.day)
            if d.month != month:
                # dim other-month days: still clickable but with parentheses
                label = f"({d.day})"
            btns.append(InlineKeyboardButton(label, callback_data=f"cal|{cal_token}|SEL|{d.isoformat()}"))
        rows.append(btns)

    tail = []
    if include_manual:
        tail.append(InlineKeyboardButton("⌨️ Manual", callback_data=f"cal|{cal_token}|MAN"))
    if include_cancel:
        tail.append(InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{cal_token}"))
    if tail:
        rows.append(tail)

    return InlineKeyboardMarkup(rows)

def format_display_name(member_obj, fallback_name: str, fallback_id: str) -> str:
    try:
        if member_obj and member_obj.user:
            u = member_obj.user
            if u.full_name:
                return u.full_name
            if u.username:
                return f"@{u.username}"
    except Exception:
        pass
    return fallback_name or fallback_id

def compute_expiry_if_ph(app_date_iso: str) -> str:
    try:
        d = datetime.strptime(app_date_iso, "%Y-%m-%d").date()
        exp = d + timedelta(days=365)
        return exp.isoformat()
    except Exception:
        return "N/A"

async def get_group_admin_ids(bot, group_id: int) -> set[int]:
    try:
        admins = await bot.get_chat_administrators(group_id)
        return {a.user.id for a in admins if not a.user.is_bot}
    except Exception:
        return set()

def unique_members_from_sheet() -> list[dict]:
    """
    Produce a unique list of members using Google Sheet rows.
    Expects Telegram ID in col 1, Name in col 2 based on your updated layout.
    Skips header or non-numeric IDs.
    Returns [{id:int, name:str}, ...]
    """
    try:
        all_rows = worksheet.get_all_values()
        seen = set()
        members = []
        for row in all_rows[1:]:  # skip header row
            if len(row) < 3:
                continue
            tid = row[1].strip()
            nm = row[2].strip()
            if not tid.isdigit():
                continue
            if tid in seen:
                continue
            seen.add(tid)
            members.append({"id": int(tid), "name": nm})
        return members
    except Exception:
        logger.exception("Failed to read members from sheet")
        return []

async def append_sheet_row(
    *,
    user_id: int,
    user_full_name: str,
    action: str,  # "Clock Off" / "Claim Off"
    current_off: float,
    delta: float,  # positive for clock, negative for claim
    approved_by: str,
    application_date: str,
    remarks: str,
    holiday_off: str,  # "Yes"/"No"
    expiry: str,       # "YYYY-MM-DD" or "N/A"
):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    add_subtract = f"{'+' if delta >= 0 else ''}{delta}"
    final = current_off + delta

    row = [
        timestamp,          # 0 Timestamp
        str(user_id),       # 1 Telegram ID
        user_full_name,     # 2 Name
        action,             # 3 Action
        f"{current_off:.1f}",# 4 Current Off
        add_subtract,       # 5 Add/Subtract
        f"{final:.1f}",     # 6 Final Off
        approved_by,        # 7 Approved By
        application_date,   # 8 Application Date
        remarks,            # 9 Remarks
        holiday_off,        # 10 Holiday Off (Yes/No)
        expiry,             # 11 Expiry (date or N/A)
    ]
    worksheet.append_row(row)
    return final

def get_user_current_off(user_id: int) -> float:
    try:
        all_rows = worksheet.get_all_values()
        rows = [r for r in all_rows if len(r) > 6 and r[1] == str(user_id)]
        if rows:
            return float(rows[-1][6])
    except Exception:
        logger.exception("Failed reading current off")
    return 0.0

# =========================
# Webhook endpoint
# =========================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        logger.warning("⚠️ Telegram app not yet initialized.")
        return "Bot not ready", 503

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        logger.info(f"📨 Incoming update: {request.get_json(force=True)}")
        fut = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
        fut.add_done_callback(_callback)
        return "OK"
    except Exception:
        logger.exception("❌ Error processing update")
        return "Internal Server Error", 500

def _callback(fut):
    try:
        fut.result()
    except Exception:
        logger.exception("❌ Exception in handler")

# =========================
# Commands & Handlers
# =========================

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
        "/help – Show this help message\n\n"
        "Tip: You can always tap ❌ Cancel or type -quit to abort.",
        parse_mode="Markdown"
    )

# -------- summary/history (same behavior as your working copy) --------
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > 6 and row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[6]
            # PH Off Total not maintained in this script (calculated elsewhere)
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
        user_rows = [row for row in all_data if len(row) > 8 and row[1] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            response = "\n".join([f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | {row[8]}" for row in last_5])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

# -------- Start flows --------
async def start_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, *, action: str, is_ph: bool):
    uid = update.effective_user.id
    user_state[uid] = {
        "action": "clockoff" if action == "clock" else "claimoff",
        "is_ph": is_ph,
        "stage": "awaiting_days",
        "group_id": update.message.chat_id,
    }
    await update.message.reply_text(
        "🕒 How many days? (0.5 to 3 in 0.5 steps)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
    )

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow(update, context, action="clock", is_ph=False)

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow(update, context, action="claim", is_ph=False)

async def clockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow(update, context, action="clock", is_ph=True)

async def claimphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow(update, context, action="claim", is_ph=True)

# -------- Mass flows (admin only) --------
async def massclock_prepare(update: Update, context: ContextTypes.DEFAULT_TYPE, *, is_ph: bool):
    # Check admin
    group_id = update.message.chat_id
    admin_ids = await get_group_admin_ids(context.bot, group_id)
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("⛔ Admins only.")
        return

    uid = update.effective_user.id
    user_state[uid] = {
        "stage": "mass_awaiting_days",
        "is_ph": is_ph,
        "group_id": group_id,
    }
    await update.message.reply_text(
        f"🧮 Mass clock {'PH ' if is_ph else ''}OIL\nHow many days per person? (0.5 to 3 in 0.5 steps)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
    )

async def massclockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await massclock_prepare(update, context, is_ph=False)

async def massclockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await massclock_prepare(update, context, is_ph=True)

# -------- Inline calendar launching --------
async def ask_for_application_date(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, purpose_note: str):
    # purpose_note helps wording for claim vs clock
    today = date_cls.today()
    cal_token = uuid.uuid4().hex[:10]
    user_state[uid]["stage"] = "awaiting_date"
    user_state[uid]["cal_token"] = cal_token
    # store current year/month, other context in calendar_sessions
    calendar_sessions[cal_token] = {
        "user_id": uid,
        "year": today.year,
        "month": today.month,
    }
    await update.message.reply_text(
        f"📅 Select Application Date\n({purpose_note})",
        reply_markup=month_calendar_kb(cal_token, today.year, today.month)
    )

# =========================
# Message handler (conversation state)
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    uid = update.effective_user.id

    # universal quit
    if text.lower() == "-quit":
        user_state.pop(uid, None)
        await update.message.reply_text("🚪 Cancelled.")
        return

    if uid not in user_state:
        return

    state = user_state[uid]
    stage = state.get("stage")

    # === normal/mass stages ===
    if stage in ("awaiting_days", "mass_awaiting_days"):
        if not valid_days_str(text):
            await update.message.reply_text(
                "❌ Invalid days. Enter a positive number between 0.5 and 3 in 0.5 steps.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
            )
            return
        state["days"] = float(text)
        # 2) Application date: different wording for claim/clock
        if stage == "awaiting_days":
            if state["action"] == "claimoff":
                note = "For claim off: date you want to *use* the off."
            else:
                note = "For clock off: date you *earned* or are earning the off."
            await ask_for_application_date(update, context, uid, note)
        else:
            # mass path
            state["stage"] = "mass_awaiting_date"
            today = date_cls.today()
            cal_token = uuid.uuid4().hex[:10]
            state["cal_token"] = cal_token
            calendar_sessions[cal_token] = {"user_id": uid, "year": today.year, "month": today.month}
            await update.message.reply_text(
                "📅 Select Application Date for MASS clock",
                reply_markup=month_calendar_kb(cal_token, today.year, today.month)
            )
        return

    if stage in ("awaiting_manual_date", "mass_awaiting_manual_date"):
        dt = parse_manual_date(text)
        if not dt:
            await update.message.reply_text(
                "❌ Invalid date. Please use YYYY-MM-DD (e.g., 2025-08-09).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
            )
            return
        state["app_date"] = dt
        # move to reason
        state["stage"] = "awaiting_reason" if stage == "awaiting_manual_date" else "mass_awaiting_reason"
        prompt = "📝 Remarks (max 80 chars)."
        await update.message.reply_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
        )
        return

    if stage in ("awaiting_reason", "mass_awaiting_reason"):
        reason = text
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        state["reason"] = reason

        if stage == "awaiting_reason":
            # Send approval requests to admins
            await update.message.reply_text("📩 Your request has been submitted for approval.")
            await send_approval_request(update, context, state)
            user_state.pop(uid, None)
        else:
            # mass preview
            members = unique_members_from_sheet()
            if not members:
                await update.message.reply_text("⚠️ No members found to mass clock.")
                user_state.pop(uid, None)
                return
            token = uuid.uuid4().hex[:10]
            mass_requests[token] = {
                "admin_id": uid,
                "group_id": state["group_id"],
                "is_ph": state["is_ph"],
                "days": state["days"],
                "app_date": state["app_date"],
                "reason": state["reason"],
                "members": members,
                "stage": "preview",
            }
            names_list = "\n".join([f"- {m['name']} ({m['id']})" for m in members])
            await update.message.reply_text(
                f"🔍 MASS PREVIEW ({'PH ' if state['is_ph'] else ''}OIL)\n"
                f"📅 Date: {state['app_date']}\n"
                f"🕒 Days each: {state['days']}\n"
                f"📝 Reason: {state['reason']}\n\n"
                f"👥 Members ({len(members)}):\n{names_list}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm", callback_data=f"mass|CONFIRM|{token}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"mass|CANCEL|{token}")
                ]])
            )
            user_state.pop(uid, None)
        return

# =========================
# Admin approval request PMs
# =========================
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict):
    user = update.effective_user
    group_id = state["group_id"]
    try:
        current_off = get_user_current_off(user.id)
        delta = float(state["days"])
        new_off = current_off + delta if state["action"] == "clockoff" else current_off - delta

        # create a short token to keep callback_data <= 64 bytes
        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": delta,
            "reason": state["reason"],
            "group_id": group_id,
            "app_date": state.get("app_date") or datetime.now().strftime("%Y-%m-%d"),
            "is_ph": bool(state.get("is_ph")),
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
                        f"🆕 *{('PH ' if state.get('is_ph') else '')}{state['action'].title()} Request*\n\n"
                        f"👤 User: {user.full_name} ({user.id})\n"
                        f"📅 Days: {delta}\n"
                        f"📆 Application Date: {pending_requests[token]['app_date']}\n"
                        f"📝 Reason: {state['reason']}\n\n"
                        f"📊 Current Off: {current_off:.1f} day(s)\n"
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

# =========================
# Callback handler
# =========================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        # --- Cancel buttons from prompts ---
        if data.startswith("cancel|usr|"):
            # user-level cancel
            _, _, uid_str = data.split("|")
            try:
                uid = int(uid_str)
                user_state.pop(uid, None)
            except Exception:
                pass
            await query.edit_message_text("🚪 Cancelled.")
            return

        # Calendar handlers
        if data.startswith("cal|"):
            _, cal_token, kind, payload = data.split("|", maxsplit=3)
            sess = calendar_sessions.get(cal_token)
            if not sess:
                await query.edit_message_text("⚠️ Calendar expired. Please restart.")
                return

            uid = sess["user_id"]
            st = user_state.get(uid)
            if not st:
                await query.edit_message_text("⚠️ Session ended.")
                calendar_sessions.pop(cal_token, None)
                return

            if kind == "NAV":
                year, month, direction = payload.split("-")[0], payload.split("-")[1], payload.split("-")[2]
                y = int(year); m = int(month)
                if direction == "prev":
                    m -= 1
                    if m == 0:
                        m = 12; y -= 1
                else:
                    m += 1
                    if m == 13:
                        m = 1; y += 1
                sess["year"] = y
                sess["month"] = m
                await query.edit_message_reply_markup(reply_markup=month_calendar_kb(cal_token, y, m))
                return

            if kind == "MAN":
                # switch to manual entry stage
                if st.get("stage") == "awaiting_date":
                    st["stage"] = "awaiting_manual_date"
                elif st.get("stage") == "mass_awaiting_date":
                    st["stage"] = "mass_awaiting_manual_date"
                await query.edit_message_text(
                    "⌨️ Enter date manually (YYYY-MM-DD):",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
                )
                calendar_sessions.pop(cal_token, None)
                return

            if kind == "SEL":
                sel_date = payload  # YYYY-MM-DD
                if st.get("stage") == "awaiting_date":
                    st["app_date"] = sel_date
                    st["stage"] = "awaiting_reason"
                    await query.edit_message_text(f"📆 Application Date: {sel_date}")
                    await context.bot.send_message(
                        chat_id=st["group_id"],
                        text="📝 Remarks (max 80 chars).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
                    )
                elif st.get("stage") == "mass_awaiting_date":
                    st["app_date"] = sel_date
                    st["stage"] = "mass_awaiting_reason"
                    await query.edit_message_text(f"📆 Application Date for MASS: {sel_date}")
                    await context.bot.send_message(
                        chat_id=st["group_id"],
                        text="📝 Remarks for MASS (max 80 chars).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|usr|{uid}")]])
                    )
                calendar_sessions.pop(cal_token, None)
                return

        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", maxsplit=1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            user_id = int(req["user_id"])
            user_full_name = req.get("user_full_name") or str(user_id)
            action = req["action"]  # "clockoff"/"claimoff"
            days = float(req["days"])
            reason = req["reason"]
            group_id = int(req["group_id"])
            app_date = req["app_date"]
            is_ph = bool(req.get("is_ph"))

            # compute current_off again at decision time
            current_off = get_user_current_off(user_id)

            if action_type == "approve":
                # delta sign: positive for clock, negative for claim
                delta = days if action == "clockoff" else -days
                holiday_off = "Yes" if is_ph else "No"
                expiry = "N/A"
                if is_ph and action == "clockoff":
                    expiry = compute_expiry_if_ph(app_date)

                final = await append_sheet_row(
                    user_id=user_id,
                    user_full_name=user_full_name,
                    action=("Clock Off" if action == "clockoff" else "Claim Off") + (" (PH)" if is_ph else ""),
                    current_off=current_off,
                    delta=delta,
                    approved_by=query.from_user.full_name,
                    application_date=app_date,
                    remarks=reason,
                    holiday_off=holiday_off,
                    expiry=expiry
                )

                # Post to group with the user's display name
                try:
                    member = await context.bot.get_chat_member(group_id, user_id)
                except Exception:
                    member = None
                display_name = format_display_name(member, user_full_name, str(user_id))

                await query.edit_message_text("✅ Request approved and recorded.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=(
                        f"✅ {display_name}'s {('PH ' if is_ph else '')}{action.replace('off', ' Off')} approved by {query.from_user.full_name}.\n"
                        f"📅 Days: {days}\n"
                        f"📆 Application Date: {app_date}\n"
                        f"📝 Reason: {reason}\n"
                        f"📊 Final: {final:.1f} day(s)"
                    )
                )
            else:
                # deny
                try:
                    member = await context.bot.get_chat_member(group_id, user_id)
                except Exception:
                    member = None
                display_name = format_display_name(member, user_full_name, str(user_id))
                await query.edit_message_text("❌ Request denied.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
                )

            # fan-out cleanup
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

        # Mass confirm/cancel
        if data.startswith("mass|"):
            _, kind, token = data.split("|", maxsplit=2)
            mreq = mass_requests.get(token)
            if not mreq:
                await query.edit_message_text("⚠️ Mass request expired.")
                return
            if kind == "CANCEL":
                mass_requests.pop(token, None)
                await query.edit_message_text("🚪 Mass request cancelled.")
                return

            if kind == "CONFIRM":
                # Execute mass clock
                is_ph = mreq["is_ph"]
                days = float(mreq["days"])
                app_date = mreq["app_date"]
                reason = mreq["reason"]
                group_id = mreq["group_id"]
                admin_name = query.from_user.full_name

                # Validate days still positive and valid
                if days <= 0 or days < MIN_DAYS or days > MAX_DAYS or (days * 10) % 5 != 0:
                    await query.edit_message_text("❌ Invalid days for mass clock.")
                    mass_requests.pop(token, None)
                    return

                # Mass clock is "Clock Off" only (as per design)
                success = 0
                for m in mreq["members"]:
                    uid = m["id"]
                    uname = m["name"]
                    curr = get_user_current_off(uid)
                    delta = days  # mass clock is always adding
                    holiday_off = "Yes" if is_ph else "No"
                    expiry = compute_expiry_if_ph(app_date) if is_ph else "N/A"
                    try:
                        _ = await append_sheet_row(
                            user_id=uid,
                            user_full_name=uname,
                            action="Clock Off" + (" (PH)" if is_ph else ""),
                            current_off=curr,
                            delta=delta,
                            approved_by=admin_name,
                            application_date=app_date,
                            remarks=reason,
                            holiday_off=holiday_off,
                            expiry=expiry
                        )
                        success += 1
                    except Exception:
                        logger.exception(f"Mass append failed for {uid}")

                await query.edit_message_text(f"✅ Mass clock completed for {success}/{len(mreq['members'])} members.")
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"📣 Mass clock {'PH ' if is_ph else ''}OIL done by {admin_name}. Members updated: {success}."
                )
                mass_requests.pop(token, None)
                return

        # NOOP safe handler (header buttons)
        if data.startswith("noop|"):
            return

    except Exception:
        logger.exception("❌ Failed to process callback")
        try:
            await query.edit_message_text("❌ Something went wrong.")
        except Exception:
            pass

# =========================
# Init
# =========================
async def init_app():
    global telegram_app, worksheet

    logger.info("🔐 Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    worksheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    logger.info("✅ Google Sheets ready.")

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).get_updates_http_version("1.1").build()

    # Core commands
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("summary", summary))
    telegram_app.add_handler(CommandHandler("history", history))

    # Individual flows
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(CommandHandler("clockphoff", clockphoff))
    telegram_app.add_handler(CommandHandler("claimphoff", claimphoff))

    # Mass admin flows
    telegram_app.add_handler(CommandHandler("massclockoff", massclockoff))
    telegram_app.add_handler(CommandHandler("massclockphoff", massclockphoff))

    # Conversation text (for days/manual date/reason and -quit)
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # All callbacks (calendar, approve/deny, cancel, mass confirm)
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook set.")

# =========================
# Run
# =========================
if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    logger.info("🟢 Starting Flask...")
    app.run(host="0.0.0.0", port=10000)
