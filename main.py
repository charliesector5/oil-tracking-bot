import os
import logging
import asyncio
import uuid
import calendar as calmod
from datetime import datetime, date, timedelta

import nest_asyncio
import gspread
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

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
log = logging.getLogger(__name__)

# ---------------- Env ----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

# ---------------- Flask ----------------
app = Flask(__name__)

@app.route("/")
def index():
    return "✅ Oil Tracking Bot is up."

@app.route("/health")
def health():
    return "✅ Health check passed."

# ---------------- Globals ----------------
telegram_app = None
worksheet = None
loop = asyncio.new_event_loop()

# Per-user conversational state
# user_state[user_id] = {
#   'token': 'abc123',
#   'flow': 'normal'|'ph'|'mass_normal'|'mass_ph'|'newuser',
#   'action': 'clockoff'|'claimoff'|'clockphoff'|'claimphoff'|'massclockoff'|'massclockphoff',
#   'stage': 'awaiting_days'|'awaiting_app_date'|'awaiting_reason'|...
#   'group_id': int,
#   'days': float, 'app_date': 'YYYY-MM-DD', 'reason': str,
#   # NEWUSER
#   'nu_normal_days': float,
#   'nu_ph_count': int, 'nu_idx': int, 'nu_entries': [{'date':'YYYY-MM-DD','reason':'...'}],
# }
user_state = {}

# Short tokens used in callback_data
# map token -> {'user_id': int, 'kind': 'flow'|'calendar', 'extra': {...}}
tokens = {}

# Pending approval requests (short token -> payload)
pending_requests = {}

MAX_REMARKS_LEN = 80

# ---------------- Helpers ----------------
def new_token(kind:str, user_id:int, extra:dict=None) -> str:
    t = uuid.uuid4().hex[:10]
    tokens[t] = {'user_id': user_id, 'kind': kind, 'extra': extra or {}}
    return t

def cancel_markup(flow_token:str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"x|{flow_token}")]])

def safe_float(x:str):
    try:
        return float(x)
    except:
        return None

def is_half_step(x:float) -> bool:
    return abs((x * 2) - round(x * 2)) < 1e-9  # multiples of 0.5

def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")

def ymd(dt:date) -> str:
    return dt.strftime("%Y-%m-%d")

def parse_date(s:str):
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None

def month_label(d:date) -> str:
    return d.strftime("%B %Y")

def ph_expiry(app_date_str:str) -> str:
    d = parse_date(app_date_str)
    if not d:
        d = date.today()
    return ymd(d + timedelta(days=365))

def latest_balance_rows(all_rows, user_id_str):
    # Returns last normal balance and last PH total (for display/append convenience)
    user_rows = [r for r in all_rows if len(r) >= 2 and r[1] == user_id_str]
    if not user_rows:
        return 0.0, 0.0
    last = user_rows[-1]
    current_off = safe_float(last[6]) or 0.0  # Final Off (col G, idx 6)
    # PH Off Total (col L, idx 11) could be 'N/A' — treat as 0.0
    ph_total = 0.0
    try:
        ph_total = float(last[11])
    except:
        ph_total = 0.0
    return current_off, ph_total

def compute_next_balances(action:str, current_off:float, days:float):
    if action in ("clockoff", "massclockoff"):
        return current_off + days, f"+{days}"
    elif action in ("claimoff",):
        return current_off - days, f"-{days}"
    # PH does not change normal current_off; keep as-is for G but we still write +/- into F for clarity
    elif action in ("clockphoff", "massclockphoff"):
        return current_off, f"+{days}"
    elif action in ("claimphoff",):
        return current_off, f"-{days}"
    else:
        return current_off, "0"

def must_be_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("OK", callback_data="noop|ok")]])

# ---------------- Calendar ----------------
async def show_calendar(chat_id:int, user_id:int, purpose:str, context:ContextTypes.DEFAULT_TYPE,
                        base_month:date=None, message_id=None):
    """
    purpose: identifies what we are picking the date for.
      One calendar per request, bound to a short token -> user_id + purpose
    """
    base = base_month or date.today().replace(day=1)
    token = new_token("calendar", user_id, extra={"purpose": purpose, "month": ymd(base)})
    kb = []

    # Title row
    kb.append([InlineKeyboardButton(f"📅 {month_label(base)}", callback_data=f"noop|{token}")])

    # Weekday header
    kb.append([InlineKeyboardButton(x, callback_data=f"noop|{token}") for x in ["Su","Mo","Tu","We","Th","Fr","Sa"]])

    # Calendar grid
    mcal = calmod.Calendar(firstweekday=6)  # Sunday
    days = list(mcal.itermonthdates(base.year, base.month))
    # 6 rows of 7 cells
    row = []
    for idx, d in enumerate(days):
        if d.month != base.month:
            txt = " "
            cb = f"noop|{token}"
        else:
            txt = str(d.day)
            cb = f"cal|{token}|{ymd(d)}"
        row.append(InlineKeyboardButton(txt, callback_data=cb))
        if (idx + 1) % 7 == 0:
            kb.append(row)
            row = []
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data=f"noop|{token}"))
        kb.append(row)

    # Nav / Manual / Cancel
    prev_month = (base.replace(day=1) - timedelta(days=1)).replace(day=1)
    next_month = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
    kb.append([
        InlineKeyboardButton("« Prev", callback_data=f"calnav|{token}|{ymd(prev_month)}"),
        InlineKeyboardButton("Manual entry", callback_data=f"manual|{token}"),
        InlineKeyboardButton("Next »", callback_data=f"calnav|{token}|{ymd(next_month)}"),
    ])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data=f"x|{user_state.get(user_id,{}).get('token','0')}")])

    text = "📅 **Select Application Date:**\n• Tap a date below, or\n• Tap **Manual entry**, then type `YYYY-MM-DD`."
    if message_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

async def handle_calendar_click(update:Update, context:ContextTypes.DEFAULT_TYPE, data:str):
    # data formats:
    #  cal|<ctoken>|YYYY-MM-DD
    #  calnav|<ctoken>|YYYY-MM-01
    #  manual|<ctoken>
    parts = data.split("|")
    kind = parts[0]
    ctoken = parts[1]
    info = tokens.get(ctoken)
    if not info or info["kind"] != "calendar":
        await update.callback_query.answer("Expired calendar.")
        return

    uid = info["user_id"]
    if uid not in user_state:
        await update.callback_query.answer("Session ended.")
        return

    st = user_state[uid]
    chat_id = update.effective_chat.id

    if kind == "calnav":
        base = parse_date(parts[2]) or date.today().replace(day=1)
        await show_calendar(chat_id, uid, info["extra"]["purpose"], context, base_month=base,
                            message_id=update.callback_query.message.message_id)
        await update.callback_query.answer()
        return

    if kind == "manual":
        st["stage"] = "awaiting_manual_date"
        await context.bot.send_message(chat_id=chat_id,
            text="⌨️ Please type the date in `YYYY-MM-DD`.", parse_mode="Markdown",
            reply_markup=cancel_markup(st["token"]))
        await update.callback_query.answer("Type a date.")
        return

    if kind == "cal":
        # Got a date; store and move on
        app_date = parts[2]
        st["app_date"] = app_date
        # Update the calendar message to freeze selection
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=update.callback_query.message.message_id,
            text=f"📅 Application Date selected: *{app_date}*",
            parse_mode="Markdown"
        )
        await update.callback_query.answer()

        # Where to go next depends on flow
        if st["flow"] in ("normal", "ph", "mass_normal", "mass_ph"):
            st["stage"] = "awaiting_reason"
            await context.bot.send_message(chat_id=chat_id,
                text=f"📝 Enter reason (max {MAX_REMARKS_LEN} chars):",
                reply_markup=cancel_markup(st["token"]))
            return

        if st["flow"] == "newuser":
            # There are two sub-cases in onboarding:
            # - picking date for normal import (if we ever add that)
            # - picking date for PH entry i (this is what we do)
            if st.get("nu_mode") == "ph_date":
                # we just selected the PH date after having a reason in st['nu_temp_reason']
                st["nu_entries"].append({"date": app_date, "reason": st.get("nu_temp_reason","Transfer")})
                st["nu_temp_reason"] = ""
                st["nu_idx"] += 1
                if st["nu_idx"] < st["nu_ph_count"]:
                    # next PH entry
                    st["nu_mode"] = "ph_reason"
                    await context.bot.send_message(chat_id=chat_id,
                        text=f"PH Entry {st['nu_idx']+1}/{st['nu_ph_count']} — Enter reason (max {MAX_REMARKS_LEN} chars):",
                        reply_markup=cancel_markup(st["token"]))
                else:
                    # Done; preview to admins
                    await send_newuser_preview(update, context, st)
            else:
                # Not expected right now
                st["stage"] = "awaiting_reason"
                await context.bot.send_message(chat_id=chat_id,
                    text=f"📝 Enter reason (max {MAX_REMARKS_LEN} chars):",
                    reply_markup=cancel_markup(st["token"]))
            return

# ---------------- Webhook ----------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        log.warning("⚠️ Telegram app not initialized.")
        return "Bot not ready", 503
    try:
        upd = Update.de_json(request.get_json(force=True), telegram_app.bot)
        log.info("📨 Incoming update: %s", request.get_json(force=True))
        fut = asyncio.run_coroutine_threadsafe(telegram_app.process_update(upd), loop)
        return "OK"
    except Exception:
        log.exception("❌ Error processing update")
        return "Internal Server Error", 500

# ---------------- Command Handlers ----------------
async def help_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ *Oil Tracking Bot Help*\n\n"
        "/clockoff – Request to clock normal OIL\n"
        "/claimoff – Request to claim normal OIL\n"
        "/clockphoff – Clock Public Holiday OIL (PH)\n"
        "/claimphoff – Claim Public Holiday OIL (PH)\n"
        "/massclockoff – Admin: Mass clock normal OIL for all\n"
        "/massclockphoff – Admin: Mass clock PH OIL for all (with preview)\n"
        "/newuser – Onboard: import old records\n"
        "/startadmin – PM the bot to start admin inbox\n"
        "/summary – Your current balance & PH details\n"
        "/history – Your past 5 logs\n"
        "/help – Show this help message\n\n"
        "Tip: You can always tap ❌ *Cancel* to abort.",
        parse_mode="Markdown"
    )

async def startadmin_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    # Should be PM; if not, ask user to PM
    if update.effective_chat.type != "private":
        await update.message.reply_text("ℹ️ Please PM me and send /startadmin there.")
        return
    await update.message.reply_text("✅ Admin inbox ready. I’ll DM you approvals from group requests here.")

# Normal / PH entry points
async def clockoff_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, flow="normal", action="clockoff")

async def claimoff_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, flow="normal", action="claimoff")

async def clockphoff_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, flow="ph", action="clockphoff")

async def claimphoff_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, flow="ph", action="claimphoff")

# Mass
async def massclockoff_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_mass_preview(update, context, flow="mass_normal", action="massclockoff")

async def massclockphoff_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_mass_preview(update, context, flow="mass_ph", action="massclockphoff")

# Onboarding
async def newuser_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    # Must be in group to start (so we know which sheet/community)
    if chat.type == "private":
        await update.message.reply_text("ℹ️ Please use /newuser *in the group* to start onboarding.", parse_mode="Markdown")
        return

    # Already has rows?
    try:
        all_rows = worksheet.get_all_values()
        existing = any((len(r)>=2 and r[1]==str(user.id)) for r in all_rows)
        if existing:
            await update.message.reply_text("✅ You’re already registered; no need to import again.")
            return
    except Exception:
        log.exception("Failed reading sheet for newuser check")

    # Init state
    tok = new_token("flow", user.id)
    user_state[user.id] = {
        "token": tok,
        "flow": "newuser",
        "action": "newuser",
        "stage": "awaiting_nu_normal_days",
        "group_id": chat.id,
        "nu_normal_days": 0.0,
        "nu_ph_count": 0,
        "nu_idx": 0,
        "nu_entries": [],
        "nu_mode": "",  # ph_reason -> ph_date loop
    }
    await update.message.reply_text(
        "🆕 *Onboarding: Import Old Records*\n\n"
        "1) How many *normal OIL days* to import? (Enter any positive number, e.g. 7.5, or 0 if none)",
        parse_mode="Markdown",
        reply_markup=cancel_markup(tok)
    )

# Summary / History
async def summary_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_rows = worksheet.get_all_values()
        rows = [r for r in all_rows if len(r)>=2 and r[1]==str(user.id)]
        if not rows:
            await update.message.reply_text("📊 No records found.")
            return

        last = rows[-1]
        balance = last[6]  # Final Off
        # PH active entries: Holiday Off == "Yes" and expiry >= today
        today = today_str()
        ph_list = []
        for r in rows:
            if len(r) >= 13 and str(r[10]).strip().lower() == "yes":  # Holiday Off
                exp = r[12] if len(r) >= 13 else "N/A"
                app = r[8]
                reason = r[9]
                try:
                    if exp >= today:
                        ph_list.append(f"• {app}: +{r[5].lstrip('+')} (exp {exp}) - {reason}")
                except:
                    pass

        ph_total = 0.0
        try:
            val = float(last[11])
            ph_total = val
        except:
            # fallback: count active PH rows as +1/-1 if present
            for r in rows:
                if len(r) >= 13 and str(r[10]).strip().lower() == "yes":
                    try:
                        delta = float(r[5])
                    except:
                        try:
                            delta = float(r[5].replace('+',''))
                        except:
                            delta = 0.0
                    ph_total += delta

        text = f"📊 Current Off Balance: {balance} day(s).\n🏖 PH Off Total: {ph_total:.1f} day(s)"
        if ph_list:
            text += "\n🔎 PH Off Entries:\n" + "\n".join(ph_list)
        await update.message.reply_text(text)
    except Exception:
        log.exception("summary failed")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_rows = worksheet.get_all_values()
        rows = [r for r in all_rows if len(r)>=2 and r[1]==str(user.id)]
        if not rows:
            await update.message.reply_text("📜 No logs found.")
            return
        last5 = rows[-5:]
        body = "\n".join([f"{r[0]} | {r[3]} | {r[5]} → {r[6]} | {r[8]}" for r in last5])
        await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{body}")
    except Exception:
        log.exception("history failed")
        await update.message.reply_text("❌ Could not retrieve your logs.")

# ---------------- Flow starters ----------------
async def start_flow_days(update:Update, context:ContextTypes.DEFAULT_TYPE, flow:str, action:str):
    chat = update.effective_chat
    user = update.effective_user

    tok = new_token("flow", user.id)
    user_state[user.id] = {
        "token": tok,
        "flow": flow,
        "action": action,
        "stage": "awaiting_days",
        "group_id": chat.id
    }

    if flow == "normal" and action == "clockoff":
        pfx = "🕒"
        txt = "How many days do you want to *clock off*? (0.5 to 3, in 0.5 steps)"
    elif flow == "normal" and action == "claimoff":
        pfx = "🗂"
        txt = "How many days do you want to *claim off*? (0.5 to 3, in 0.5 steps)"
    elif flow == "ph" and action == "clockphoff":
        pfx = "🏝"
        txt = "How many *PH OIL* days to clock? (0.5 to 3, in 0.5 steps)"
    elif flow == "ph" and action == "claimphoff":
        pfx = "🏝"
        txt = "How many *PH OIL* days to claim? (0.5 to 3, in 0.5 steps)"
    else:
        pfx = "⏳"
        txt = "How many days?"

    await update.message.reply_text(
        f"{pfx} {txt}",
        parse_mode="Markdown",
        reply_markup=cancel_markup(tok)
    )

async def start_mass_preview(update:Update, context:ContextTypes.DEFAULT_TYPE, flow:str, action:str):
    chat = update.effective_chat
    user = update.effective_user

    # Admin check
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admin_ids = [a.user.id for a in admins if not a.user.is_bot]
        if user.id not in admin_ids:
            await update.message.reply_text("⛔️ Only admins can run mass actions.")
            return
    except Exception:
        log.exception("admin check failed")
        await update.message.reply_text("❌ Failed to check admin privileges.")
        return

    # Preview list based on sheet distinct users (skip header / malformed)
    try:
        rows = worksheet.get_all_values()
        seen = {}
        for r in rows:
            if len(r) >= 3 and r[1].isdigit():
                seen[r[1]] = r[2] or r[1]
        # Show names
        listing = "\n".join([f"• {name} ({uid})" for uid, name in seen.items()])
        if not listing:
            listing = "• (none yet – once people interact, they’ll appear here)"
        await update.message.reply_text(
            f"👥 Mass-action preview (from sheet):\n{listing}"
        )
    except Exception:
        log.exception("mass preview failed")
        await update.message.reply_text("❌ Could not build preview list.")
        return

    tok = new_token("flow", user.id)
    user_state[user.id] = {
        "token": tok,
        "flow": flow,
        "action": action,
        "stage": "awaiting_days",
        "group_id": chat.id,
    }

    if action == "massclockoff":
        await update.message.reply_text(
            "🧮 Days to *mass clock (normal)* for everyone? (0.5 to 3, in 0.5 steps)",
            parse_mode="Markdown",
            reply_markup=cancel_markup(tok)
        )
    else:
        await update.message.reply_text(
            "🧮 Days to *mass clock (PH)* for everyone? (0.5 to 3, in 0.5 steps)",
            parse_mode="Markdown",
            reply_markup=cancel_markup(tok)
        )

# ---------------- Message handler (text replies) ----------------
async def handle_message(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in user_state:
        return
    st = user_state[user.id]
    text = (update.message.text or "").strip()
    log.info("handle_message: uid=%s stage=%s flow=%s action=%s text=%s",
             user.id, st.get("stage"), st.get("flow"), st.get("action"), text)

    # Global: manual date entry when stage asks for it
    if st["stage"] == "awaiting_manual_date":
        d = parse_date(text)
        if not d:
            await update.message.reply_text("❌ Invalid date. Please use YYYY-MM-DD.",
                                            reply_markup=cancel_markup(st["token"]))
            return
        st["app_date"] = ymd(d)
        # next hop
        if st["flow"] in ("normal","ph","mass_normal","mass_ph"):
            st["stage"] = "awaiting_reason"
            await update.message.reply_text(f"📝 Enter reason (max {MAX_REMARKS_LEN} chars):",
                                            reply_markup=cancel_markup(st["token"]))
            return
        if st["flow"] == "newuser" and st.get("nu_mode") == "ph_date":
            st["nu_entries"].append({"date": ymd(d), "reason": st.get("nu_temp_reason","Transfer")})
            st["nu_temp_reason"] = ""
            st["nu_idx"] += 1
            if st["nu_idx"] < st["nu_ph_count"]:
                st["nu_mode"] = "ph_reason"
                await update.message.reply_text(
                    f"PH Entry {st['nu_idx']+1}/{st['nu_ph_count']} — Enter reason (max {MAX_REMARKS_LEN} chars):",
                    reply_markup=cancel_markup(st["token"]))
            else:
                await send_newuser_preview(update, context, st)
            return

    # ---- Newuser path ----
    if st["flow"] == "newuser":
        if st["stage"] == "awaiting_nu_normal_days":
            val = safe_float(text)
            if val is None or val < 0:
                await update.message.reply_text("❌ Enter a non-negative number (e.g., 7.5 or 0).",
                                                reply_markup=cancel_markup(st["token"]))
                return
            st["nu_normal_days"] = val
            st["stage"] = "awaiting_nu_ph_count"
            await update.message.reply_text(
                "2) How many *PH entries* to import? (Enter an integer, e.g., 0, 1, 2 ...)",
                parse_mode="Markdown",
                reply_markup=cancel_markup(st["token"])
            )
            return

        if st["stage"] == "awaiting_nu_ph_count":
            try:
                cnt = int(text)
                if cnt < 0 or cnt > 50:
                    raise ValueError
            except:
                await update.message.reply_text("❌ Enter an integer between 0 and 50.",
                                                reply_markup=cancel_markup(st["token"]))
                return
            st["nu_ph_count"] = cnt
            st["nu_idx"] = 0
            st["nu_entries"] = []
            if cnt == 0:
                # no PH entries, send preview straight away
                await send_newuser_preview(update, context, st)
                return
            # Start PH entry loop: reason then date
            st["nu_mode"] = "ph_reason"
            await update.message.reply_text(
                f"PH Entry 1/{cnt} — Enter reason (max {MAX_REMARKS_LEN} chars):",
                reply_markup=cancel_markup(st["token"]))
            return

        if st.get("nu_mode") == "ph_reason":
            reason = text[:MAX_REMARKS_LEN]
            st["nu_temp_reason"] = reason
            st["nu_mode"] = "ph_date"
            st["stage"] = "awaiting_app_date"
            await show_calendar(update.effective_chat.id, user.id, purpose="nu_ph_date", context=context)
            return

    # ---- Normal / PH / Mass paths ----
    if st["stage"] == "awaiting_days":
        val = safe_float(text)
        if val is None or val <= 0:
            await update.message.reply_text("❌ Enter a *positive* number.",
                                            parse_mode="Markdown",
                                            reply_markup=cancel_markup(st["token"]))
            return
        # Only normal UI is restricted to 0.5, 3, 0.5 steps
        if st["flow"] in ("normal","ph","mass_normal","mass_ph"):
            if val < 0.5 or val > 3 or not is_half_step(val):
                await update.message.reply_text("❌ Use 0.5 to 3 (in 0.5 steps).",
                                                reply_markup=cancel_markup(st["token"]))
                return
        st["days"] = val
        st["stage"] = "awaiting_app_date"
        await show_calendar(update.effective_chat.id, user.id, purpose="app_date", context=context)
        return

    if st["stage"] == "awaiting_reason":
        reason = text[:MAX_REMARKS_LEN]
        st["reason"] = reason

        # Submit for approval (single-user flow) or run mass append
        if st["flow"] in ("normal","ph"):
            await send_approval_request(update, context, st)
            # Clear live state: the token lives until admin handles request
            user_state.pop(user.id, None)
            return

        if st["flow"] in ("mass_normal", "mass_ph"):
            await run_mass_append(update, context, st)
            user_state.pop(user.id, None)
            return

# ---------------- Admin approval - single user ----------------
async def send_approval_request(update:Update, context:ContextTypes.DEFAULT_TYPE, st:dict):
    chat_id = st["group_id"]
    user = update.effective_user

    try:
        all_rows = worksheet.get_all_values()
        current_off, ph_total_prev = latest_balance_rows(all_rows, str(user.id))
        days = float(st["days"])
        action = st["action"]
        app_date = st["app_date"]
        reason = st["reason"]

        final_off, add_sub = compute_next_balances(action, current_off, days)
        is_ph = action in ("clockphoff","claimphoff")
        expiry = ph_expiry(app_date) if is_ph and action=="clockphoff" else ("N/A" if not is_ph else ph_expiry(app_date))

        # Build server-side approval payload
        atok = new_token("approval", user.id, extra={"group_id": chat_id})
        pending_requests[atok] = {
            "user_id": user.id,
            "user_name": user.full_name,
            "action": action,
            "days": days,
            "app_date": app_date,
            "reason": reason,
            "current_off": current_off,
            "final_off": final_off,
            "add_sub": add_sub,
            "is_ph": is_ph,
            "expiry": expiry,
        }

        # DM all admins
        admins = await context.bot.get_chat_administrators(chat_id)
        sent_to = 0
        for a in admins:
            if a.user.is_bot:
                continue
            try:
                text = (
                    f"🆕 *{('PH ' if is_ph else '')}{action.replace('off',' Off').title()} Request*\n\n"
                    f"👤 User: {user.full_name} ({user.id})\n"
                    f"📅 Days: {days}\n"
                    f"🗓 Application Date: {app_date}\n"
                    f"📝 Reason: {reason}\n\n"
                    f"📊 Current Off: {current_off:.1f} day(s)\n"
                )
                if is_ph:
                    text += f"🏖 PH Expiry: {pending_requests[atok]['expiry']}\n"
                text += f"➡️ Final After: {final_off:.1f} day(s)"

                await context.bot.send_message(
                    chat_id=a.user.id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{atok}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{atok}"),
                    ]])
                )
                sent_to += 1
            except Exception as e:
                log.warning("Cannot DM admin %s: %s", a.user.id, e)
        if sent_to == 0:
            await update.message.reply_text("⚠️ I couldn’t DM any admin. Ask an admin to /startadmin in PM.")
        else:
            await update.message.reply_text("📩 Submitted for approval. An admin will review shortly.")
    except Exception:
        log.exception("send_approval_request failed")
        await update.message.reply_text("❌ Could not submit for approval.")

# ---------------- Mass append ----------------
async def run_mass_append(update:Update, context:ContextTypes.DEFAULT_TYPE, st:dict):
    chat_id = st["group_id"]
    user = update.effective_user
    action = st["action"]
    is_ph = action == "massclockphoff"

    try:
        # Admins already checked. Build distinct user list from sheet.
        rows = worksheet.get_all_values()
        people = {}
        for r in rows:
            if len(r) >= 3 and r[1].isdigit():
                people[r[1]] = r[2] or r[1]

        if not people:
            await update.message.reply_text("ℹ️ No people found in the sheet yet.")
            return

        # Perform append per person
        appended = 0
        for uid, name in people.items():
            # Compute current balance per person from last row
            user_rows = [r for r in rows if len(r)>=2 and r[1]==uid]
            if user_rows:
                last = user_rows[-1]
                cur = safe_float(last[6]) or 0.0
                try:
                    prev_ph_total = float(last[11])
                except:
                    prev_ph_total = 0.0
            else:
                cur = 0.0
                prev_ph_total = 0.0

            days = float(st["days"])
            if days <= 0:
                continue  # safety

            final_off, add_sub = compute_next_balances(action, cur, days)
            app_date = st["app_date"]
            reason = st["reason"]

            holiday_flag = "Yes" if is_ph else "No"
            expiry = ph_expiry(app_date) if is_ph else "N/A"
            ph_total = prev_ph_total + days if is_ph else "N/A"

            now = datetime.now()
            row = [
                now.strftime("%Y-%m-%d %H:%M:%S"),
                uid,
                name,
                ("Clock Off" if action=="massclockoff" else "Clock Off (PH)"),
                f"{cur:.1f}",
                add_sub,
                f"{final_off:.1f}",
                user.full_name,
                app_date,
                reason[:MAX_REMARKS_LEN],
                holiday_flag,
                f"{ph_total}" if is_ph else "N/A",
                expiry
            ]
            worksheet.append_row(row)
            appended += 1

        await update.message.reply_text(f"✅ Mass append complete: {appended} people updated.")
    except Exception:
        log.exception("mass append failed")
        await update.message.reply_text("❌ Mass append failed.")

# ---------------- Onboarding: preview & approval ----------------
async def send_newuser_preview(update:Update, context:ContextTypes.DEFAULT_TYPE, st:dict):
    user = update.effective_user
    gid = st["group_id"]

    normal_days = st.get("nu_normal_days", 0.0)
    ph_entries = st.get("nu_entries", [])

    # Build approval payload
    atok = new_token("approval", user.id, extra={"group_id": gid, "newuser": True})
    pending_requests[atok] = {
        "user_id": user.id,
        "user_name": user.full_name,
        "action": "newuser",
        "normal_days": normal_days,
        "ph_entries": ph_entries,  # list of {date, reason}
    }

    # Send preview to admins
    admins = await context.bot.get_chat_administrators(gid)
    body = f"👤 *{user.full_name}* ({user.id})\n"
    body += f"• Normal OIL to import: {normal_days}\n"
    if ph_entries:
        body += f"• PH entries ({len(ph_entries)}):\n"
        for e in ph_entries:
            body += f"  - {e['date']} — {e['reason']}\n"
    else:
        body += "• PH entries: (none)\n"

    sent = 0
    for a in admins:
        if a.user.is_bot:
            continue
        try:
            await context.bot.send_message(
                chat_id=a.user.id,
                text="🆕 *Onboarding Request*\n\n" + body,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve Import", callback_data=f"approve|{atok}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"deny|{atok}"),
                ]])
            )
            sent += 1
        except Exception as e:
            log.warning("Cannot DM admin %s: %s", a.user.id, e)

    await update.message.reply_text(
        "📩 Submitted to admins for verification. You’ll be notified after approval."
        if sent else "⚠️ I couldn’t DM any admin. Ask an admin to /startadmin in PM."
    )
    # keep token; clear live state
    user_state.pop(user.id, None)

# ---------------- Callback handler ----------------
async def handle_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()  # pre-answer so the spinner stops quickly

    try:
        if data.startswith("x|"):
            # Cancel button: x|<flow_token>
            ftok = data.split("|",1)[1]
            finfo = tokens.get(ftok)
            uid = update.effective_user.id
            # Clear state if belongs to user
            if uid in user_state and user_state[uid].get("token")==ftok:
                user_state.pop(uid, None)
            await q.edit_message_text("🧹 Cancelled.")
            return

        if data.startswith("approve|") or data.startswith("deny|"):
            action, atok = data.split("|",1)
            payload = pending_requests.get(atok)
            if not payload:
                await q.edit_message_text("⚠️ This request has already been handled.")
                return

            approver = update.effective_user.full_name
            gid = tokens.get(atok, {}).get("extra", {}).get("group_id") or payload.get("group_id")

            if payload.get("action") == "newuser":
                # Apply onboarding import
                await handle_newuser_apply(update, context, payload, action=="approve", approver)
            else:
                # Single-user normal/PH
                await handle_single_apply(update, context, payload, action=="approve", approver)

            # Mark the admin PM as resolved but retain details
            status = "✅ Approved" if action=="approve" else "❌ Denied"
            await q.edit_message_text(
                f"{status} by {approver}.\n\n"
                f"(Details retained above in this thread.)"
            )

            # Cleanup
            pending_requests.pop(atok, None)
            return

        # Calendar handlers
        if data.startswith("cal|") or data.startswith("calnav|") or data.startswith("manual|"):
            await handle_calendar_click(update, context, data)
            return

        # harmless noop
        if data.startswith("noop|"):
            return

    except Exception:
        log.exception("callback handling failed")
        try:
            await q.edit_message_text("❌ Something went wrong.")
        except:
            pass

# ---------------- Apply functions ----------------
async def handle_single_apply(update:Update, context:ContextTypes.DEFAULT_TYPE, p:dict, approved:bool, approver_name:str):
    chat_id = tokens.get(update.callback_query.data.split("|",1)[1], {}).get("extra", {}).get("group_id") or p.get("group_id")

    uid = p["user_id"]
    uname = p["user_name"]
    action = p["action"]
    days = float(p["days"])
    app_date = p["app_date"]
    reason = p["reason"]
    is_ph = p["is_ph"]

    # Resolve name in group (best-effort)
    display = uname
    try:
        member = await context.bot.get_chat_member(chat_id, uid)
        if member and member.user:
            display = member.user.full_name or (f"@{member.user.username}" if member.user.username else uname)
    except Exception:
        pass

    if not approved:
        # Notify group
        try:
            await context.bot.send_message(chat_id=chat_id,
                text=f"❌ {display}'s request was denied by {approver_name}.\n📝 Reason: {reason}")
        except:
            pass
        return

    # Approved: append to sheet
    try:
        all_rows = worksheet.get_all_values()
        current_off, prev_ph_total = latest_balance_rows(all_rows, str(uid))
        final_off, add_sub = compute_next_balances(action, current_off, days)

        now = datetime.now()
        holiday_flag = "Yes" if is_ph else "No"
        expiry = ph_expiry(app_date) if is_ph else "N/A"
        ph_total = (prev_ph_total + days) if (is_ph and "clock" in action) else \
                   (prev_ph_total - days) if (is_ph and "claim" in action) else "N/A"

        row = [
            now.strftime("%Y-%m-%d %H:%M:%S"), # Timestamp
            str(uid),                          # Telegram ID
            uname,                             # Name
            "Clock Off" if action=="clockoff" else
            "Claim Off" if action=="claimoff" else
            "Clock Off (PH)" if action=="clockphoff" else
            "Claim Off (PH)",                  # Action
            f"{current_off:.1f}",              # Current Off
            add_sub,                           # Add/Subtract
            f"{final_off:.1f}",                # Final Off
            approver_name,                     # Approved By
            app_date,                          # Application Date
            reason[:MAX_REMARKS_LEN],          # Remarks
            holiday_flag,                      # Holiday Off
            f"{ph_total}" if is_ph else "N/A", # PH Off Total
            expiry                             # Expiry
        ]
        worksheet.append_row(row)

        # Notify group
        msg = (
            f"✅ {display}'s {action.replace('off',' Off')} approved by {approver_name}.\n"
            f"📅 Days: {days}\n"
            f"🗓 Application Date: {app_date}\n"
            f"📝 Reason: {reason}\n"
            f"📊 Final: {final_off:.1f} day(s)"
        )
        if is_ph:
            msg += f"\n🏖 PH Expiry: {expiry}"
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception:
        log.exception("sheet append failed (single)")
        await context.bot.send_message(chat_id=chat_id, text="❌ Failed to record the approved request.")

async def handle_newuser_apply(update:Update, context:ContextTypes.DEFAULT_TYPE, p:dict, approved:bool, approver_name:str):
    gid = tokens.get(update.callback_query.data.split("|",1)[1], {}).get("extra", {}).get("group_id") or p.get("group_id")
    uid = p["user_id"]
    uname = p["user_name"]
    normal_days = float(p.get("normal_days", 0.0))
    ph_entries = p.get("ph_entries", [])

    # Resolve display
    display = uname
    try:
        member = await context.bot.get_chat_member(gid, uid)
        if member and member.user:
            display = member.user.full_name or (f"@{member.user.username}" if member.user.username else uname)
    except Exception:
        pass

    if not approved:
        try:
            await context.bot.send_message(chat_id=gid, text=f"❌ Onboarding for {display} was denied by {approver_name}.")
        except:
            pass
        return

    try:
        all_rows = worksheet.get_all_values()
        current_off, prev_ph_total = latest_balance_rows(all_rows, str(uid))

        now = datetime.now()
        appended = 0

        # 1) Import normal days (as a single Clock Off row), if any
        if normal_days > 0:
            final_off = current_off + normal_days
            row = [
                now.strftime("%Y-%m-%d %H:%M:%S"),
                str(uid),
                uname,
                "Clock Off",
                f"{current_off:.1f}",
                f"+{normal_days}",
                f"{final_off:.1f}",
                approver_name,
                today_str(),
                "Transfer from old record",
                "No",
                "N/A",
                "N/A"
            ]
            worksheet.append_row(row)
            current_off = final_off
            appended += 1

        # 2) Import PH entries (each 1 day, with its provided date and reason)
        ph_total = prev_ph_total
        for e in ph_entries:
            app_date = e["date"]
            reason = e["reason"][:MAX_REMARKS_LEN]
            expiry = ph_expiry(app_date)
            ph_total += 1.0
            row = [
                now.strftime("%Y-%m-%d %H:%M:%S"),
                str(uid),
                uname,
                "Clock Off (PH)",
                f"{current_off:.1f}",
                "+1.0",
                f"{current_off:.1f}",
                approver_name,
                app_date,
                reason,
                "Yes",
                f"{ph_total}",
                expiry
            ]
            worksheet.append_row(row)
            appended += 1

        await context.bot.send_message(
            chat_id=gid,
            text=f"✅ Onboarding import for {display} completed by {approver_name}. ({appended} rows added)"
        )
    except Exception:
        log.exception("newuser apply failed")
        await context.bot.send_message(chat_id=gid, text="❌ Failed to record onboarding rows.")

# ---------------- Init ----------------
async def init_app():
    global telegram_app, worksheet

    log.info("🔐 Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    worksheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    log.info("✅ Google Sheets ready.")

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).get_updates_http_version("1.1").build()

    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(CommandHandler("startadmin", startadmin_cmd))

    telegram_app.add_handler(CommandHandler("clockoff", clockoff_cmd))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff_cmd))
    telegram_app.add_handler(CommandHandler("clockphoff", clockphoff_cmd))
    telegram_app.add_handler(CommandHandler("claimphoff", claimphoff_cmd))

    telegram_app.add_handler(CommandHandler("massclockoff", massclockoff_cmd))
    telegram_app.add_handler(CommandHandler("massclockphoff", massclockphoff_cmd))

    telegram_app.add_handler(CommandHandler("newuser", newuser_cmd))

    telegram_app.add_handler(CommandHandler("summary", summary_cmd))
    telegram_app.add_handler(CommandHandler("history", history_cmd))

    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    log.info("🚀 Webhook set.")

# ---------------- Run ----------------
if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    log.info("🟢 Starting Flask...")
    app.run(host="0.0.0.0", port=10000)