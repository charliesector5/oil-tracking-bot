# main.py
import os
import logging
import asyncio
import nest_asyncio
from datetime import datetime, date, timedelta
from uuid import uuid4
from typing import Dict, Any, List, Tuple, Optional

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from flask import Flask, request
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Env / Globals
# -----------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

if not BOT_TOKEN or not WEBHOOK_URL or not GOOGLE_SHEET_ID:
    log.warning("Environment variables missing. BOT_TOKEN/WEBHOOK_URL/GOOGLE_SHEET_ID are required.")

app = Flask(__name__)

telegram_app = None
worksheet = None

# Async infra
loop = asyncio.new_event_loop()

# In-memory state
user_state: Dict[int, Dict[str, Any]] = {}
pending_payloads: Dict[str, Dict[str, Any]] = {}  # key -> payload for admin approve/deny

# -----------------------------------------------------------------------------
# Helpers: Google Sheets
# -----------------------------------------------------------------------------
def gsheet_init():
    global worksheet
    log.info("🔐 Connecting to Google Sheets…")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    worksheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    log.info("✅ Google Sheets ready.")

def get_all_rows() -> List[List[str]]:
    try:
        return worksheet.get_all_values()
    except Exception:
        log.exception("Failed to read sheet")
        return []

def last_off_for_user(user_id: str) -> float:
    """Return latest Final Off for a user (normal off balance)."""
    rows = get_all_rows()
    urows = [r for r in rows if len(r) > 1 and r[1] == str(user_id)]
    if not urows:
        return 0.0
    try:
        return float(urows[-1][6])  # column G Final Off
    except Exception:
        return 0.0

def compute_ph_entries_active(user_id: str) -> Tuple[float, List[Dict[str, Any]]]:
    """
    Return (ph_total_left, active_entries_list).
    active_entries_list: list of dicts with keys: date, expiry, reason, qty
    Logic: FIFO across rows marked Holiday Off == 'Yes'.
    """
    rows = get_all_rows()
    ph_events = []
    for r in rows[1:]:
        if len(r) < 13:
            continue
        rid, action = r[1], r[3]
        is_ph = (len(r) >= 11 and (r[10].strip().lower() in ("yes", "y", "true", "1")))  # K: Holiday Off
        if rid != str(user_id) or not is_ph:
            continue
        qty_raw = r[5].strip() if len(r) > 5 else ""
        qty = 0.0
        if qty_raw:
            try:
                qty = float(qty_raw.replace("+", ""))
            except Exception:
                qty = 0.0
            if qty_raw.startswith("-"):
                qty = -abs(qty)
        app_date = r[8].strip() if len(r) > 8 else ""
        expiry = r[12].strip() if len(r) > 12 else ""
        reason = r[9].strip() if len(r) > 9 else ""  # J remarks
        ph_events.append({
            "action": action,
            "qty": qty,
            "app_date": app_date,
            "expiry": expiry,
            "reason": reason
        })

    clocks = []
    for e in ph_events:
        if e["qty"] > 0:
            clocks.append({
                "date": e["app_date"],
                "expiry": e["expiry"],
                "reason": e["reason"],
                "qty": e["qty"]
            })

    def dparse(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return date(2100, 1, 1)
    clocks.sort(key=lambda x: dparse(x["expiry"]))

    claims_total = sum(-e["qty"] for e in ph_events if e["qty"] < 0)
    for c in clocks:
        if claims_total <= 0:
            break
        use = min(c["qty"], claims_total)
        c["qty"] -= use
        claims_total -= use

    active = [c for c in clocks if c["qty"] > 0.0001]
    total_left = sum(c["qty"] for c in active)
    return (round(total_left, 3), active)

def append_row(
    user_id: str,
    user_name: str,
    action: str,
    current_off: float,
    add_subtract: float,
    final_off: float,
    approved_by: str,
    application_date: str,
    remarks: str,
    is_ph: bool,
    ph_total: float,
    expiry: Optional[str]
):
    """
    Append one row in this order (matching your current sheet):
    A Time Stamp (now)
    B Telegram ID
    C Name
    D Action
    E Current Off
    F Add/Subtract
    G Final Off
    H Approved By
    I Application Date
    J Remarks
    K Holiday Off (Yes/No)
    L PH Off Total (number)
    M Expiry (YYYY-MM-DD or '')
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now,                               # A Time Stamp
        str(user_id),                      # B
        user_name or "",                   # C
        action,                            # D
        f"{current_off:.1f}",              # E
        f"{'+' if add_subtract >= 0 else ''}{add_subtract:.1f}",  # F
        f"{final_off:.1f}",                # G
        approved_by,                       # H
        application_date,                  # I
        remarks,                           # J
        "Yes" if is_ph else "No",          # K
        f"{ph_total:.1f}" if is_ph else "",# L
        expiry or ""                       # M
    ]
    worksheet.append_row(row)

# -----------------------------------------------------------------------------
# Helpers: Telegram UI bits
# -----------------------------------------------------------------------------
def cancel_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{session_id}")]])

def bold(s: str) -> str:
    return f"*{s}*"

# --- Quiet send helpers (group messages are silent, PMs normal) ---
def _is_group(chat_type: str) -> bool:
    return chat_type in ("group", "supergroup")

async def reply_quiet(update: Update, text: str, **kwargs):
    if update.effective_chat and _is_group(update.effective_chat.type):
        kwargs.setdefault("disable_notification", True)
    return await update.message.reply_text(text, **kwargs)

async def send_group_quiet(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    kwargs.setdefault("disable_notification", True)
    return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)

# -----------------------------------------------------------------------------
# Helpers: Admin PM summary
# -----------------------------------------------------------------------------
def _label_from_action(action: str) -> str:
    if action == "clockoff": return "Clock Off"
    if action == "claimoff": return "Claim Off"
    if action == "clockphoff": return "Clock PH Off"
    if action == "claimphoff": return "Claim PH Off"
    return action

def build_admin_summary_text(p: dict, approved: bool, approver_name: str, final_off: float | None) -> str:
    t = "✅ Approved" if approved else "❌ Denied"
    if p["type"] == "single":
        label = _label_from_action(p["action"])
        lines = [
            f"{t}",
            f"{label} — {p['user_name']} ({p['user_id']})",
            f"Days: {p['days']} | Date: {p['app_date']}",
            f"Reason: {p.get('reason','') or '—'}",
        ]
        if p.get("is_ph") and p.get("expiry"):
            lines.append(f"Expiry: {p['expiry']}")
        if final_off is not None and approved:
            lines.append(f"Final Off: {final_off:.1f}")
        lines.append(f"Approved by: {approver_name}")
        return "\n".join(lines)

    if p["type"] == "mass":
        label = "Mass Clock PH" if p["is_ph"] else "Mass Clock"
        return "\n".join([
            f"{t}",
            f"{label}",
            f"Days per user: {p['days']}",
            f"Approved by: {approver_name}"
        ])

    if p["type"] == "newuser":
        return "\n".join([
            f"{t}",
            f"Onboarding — {p['user_name']} ({p['user_id']})",
            f"Normal OIL: {p.get('normal_days',0)}",
            f"PH entries: {len(p.get('ph_entries',[]))}",
            f"Approved by: {approver_name}"
        ])

    return f"{t} by {approver_name}"

async def update_all_admin_pm(context: ContextTypes.DEFAULT_TYPE, payload: dict, summary_text: str):
    for admin_id, msg_id in payload.get("admin_msgs", []):
        try:
            await context.bot.edit_message_text(
                chat_id=admin_id,
                message_id=msg_id,
                text=summary_text
            )
        except Exception:
            try:
                await context.bot.send_message(chat_id=admin_id, text=summary_text)
            except Exception:
                pass

# -----------------------------------------------------------------------------
# Helpers: Calendar & Validation
# -----------------------------------------------------------------------------
def month_start(d: date) -> date:
    return date(d.year, d.month, 1)

def month_add(d: date, delta_months: int) -> date:
    y = d.year + (d.month - 1 + delta_months) // 12
    m = (d.month - 1 + delta_months) % 12 + 1
    return date(y, m, 1)

def build_calendar(
    session_id: str,
    cur: date,
    min_date: Optional[date] = None,
    max_date: Optional[date] = None
) -> InlineKeyboardMarkup:
    """
    session_id ties callbacks to a user flow.
    callback_data patterns:
      - noop|<sid>
      - cal|<sid>|YYYY-MM-DD
      - calnav|<sid>|YYYY-MM-01
      - manual|<sid>
      - cancel|<sid>
    Only dates within [min_date, max_date] are clickable.
    """
    header = [InlineKeyboardButton(f"📅 {cur.strftime('%B %Y')}", callback_data=f"noop|{session_id}")]
    weekdays = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
    week_hdr = [InlineKeyboardButton(d, callback_data=f"noop|{session_id}") for d in weekdays]

    first = month_start(cur)
    start_wd = first.weekday()  # Mon=0..Sun=6
    start_offset = (start_wd + 1) % 7  # make Sunday=0..Sat=6

    next_m = month_add(first, 1)
    days_in_month = (next_m - first).days

    rows = []
    row = []
    for _ in range(start_offset):
        row.append(InlineKeyboardButton(" ", callback_data=f"noop|{session_id}"))
    day = 1
    while day <= days_in_month:
        while len(row) < 7 and day <= days_in_month:
            d = date(cur.year, cur.month, day)
            in_range = True
            if min_date and d < min_date:
                in_range = False
            if max_date and d > max_date:
                in_range = False
            if in_range:
                row.append(InlineKeyboardButton(
                    f"{day}",
                    callback_data=f"cal|{session_id}|{d.strftime('%Y-%m-%d')}"
                ))
            else:
                row.append(InlineKeyboardButton("·", callback_data=f"noop|{session_id}"))
            day += 1
        if len(row) < 7:
            while len(row) < 7:
                row.append(InlineKeyboardButton(" ", callback_data=f"noop|{session_id}"))
        rows.append(row)
        row = []

    prev_month = month_add(first, -1)
    next_month = month_add(first, +1)
    allow_prev = (min_date is None) or (prev_month >= date(min_date.year, min_date.month, 1))
    allow_next = (max_date is None) or (next_month <= date(max_date.year, max_date.month, 1))

    nav = [
        InlineKeyboardButton("« Prev", callback_data=(f"calnav|{session_id}|{prev_month.strftime('%Y-%m-01')}" if allow_prev else f"noop|{session_id}")),
        InlineKeyboardButton("Manual entry", callback_data=f"manual|{session_id}"),
        InlineKeyboardButton("Next »", callback_data=(f"calnav|{session_id}|{next_month.strftime('%Y-%m-01')}" if allow_next else f"noop|{session_id}"))
    ]
    cancel = [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{session_id}")]

    keyboard = [header, week_hdr] + rows + [nav, cancel]
    return InlineKeyboardMarkup(keyboard)

def validate_half_step(x: float) -> bool:
    return abs((x * 10) % 5) < 1e-9

def parse_date_yyyy_mm_dd(s: str) -> Optional[str]:
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d").date()
        return d.strftime("%Y-%m-%d")
    except Exception:
        return None

def validate_application_date(action: str, dstr: str) -> tuple[bool, str]:
    """
    Returns (ok, errmsg). dstr = 'YYYY-MM-DD'
    Clocking (clockoff/clockphoff/newuser_ph/mass): today-365 .. today
    Claiming (claimoff/claimphoff): today-365 .. today+365
    """
    try:
        d = datetime.strptime(dstr, "%Y-%m-%d").date()
    except Exception:
        return False, "Invalid date format. Please use YYYY-MM-DD."

    today = date.today()
    past_365 = today - timedelta(days=365)
    future_365 = today + timedelta(days=365)

    is_clock = action in ("clockoff", "clockphoff", "newuser_ph", "mass")
    if is_clock:
        lo, hi = past_365, today
    else:
        lo, hi = past_365, future_365

    if d < lo or d > hi:
        return False, f"Date must be between {lo} and {hi}."
    return True, ""

# -----------------------------------------------------------------------------
# Messaging snippets
# -----------------------------------------------------------------------------
HELP_TEXT = (
    "🛠️ *Oil Tracking Bot Help*\n\n"
    "/clockoff – Request to clock normal OIL\n"
    "/claimoff – Request to claim normal OIL\n"
    "/clockphoff – Clock Public Holiday OIL (PH)\n"
    "/claimphoff – Claim Public Holiday OIL (PH)\n"
    "/massclockoff – Admin: Mass clock normal OIL for all\n"
    "/massclockphoff – Admin: Mass clock PH OIL for all (with preview)\n"
    "/newuser – Import your old records (onboarding)\n"
    "/startadmin – Start admin session (PM only)\n"
    "/summary – Your Total OIL Balance + breakdown\n"
    "/history – Your past 5 logs\n"
    "/help – Show this help message\n\n"
    "Tip: You can always tap ❌ Cancel or type -quit to abort."
)

PIN_TEXT = (
    "📌 *OIL Bot Quick Guide*  \n"
    "• Use /clockoff and /claimoff to add/claim normal OIL (0.5–3 days).  \n"
    "• Use /clockphoff and /claimphoff for *Public Holiday (PH)* OIL with 365-day expiry.  \n"
    "• /summary shows your Total OIL balance (Normal + PH) and PH entries.  \n"
    "• /overview shows a snapshot (totals, last action, soonest PH expiries).  \n"
    "• /history shows your last 5 logs.  \n"
    "• Admins: /massclockoff, /massclockphoff.  \n"
    "• New teammates: /newuser to import old records.  \n"
    "• Need to stop a flow? Tap ❌ Cancel or type `-quit`.\n"
)

# -----------------------------------------------------------------------------
# Bot commands
# -----------------------------------------------------------------------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_quiet(update, HELP_TEXT, parse_mode="Markdown")

async def cmd_startadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await reply_quiet(update, "Please PM me and use /startadmin to begin the admin session.")
        return
    user_state[update.effective_user.id] = {"flow": "admin_session", "stage": "ready", "owner_id": update.effective_user.id}
    await update.message.reply_text("✅ Admin session started here. You’ll receive approval prompts in this PM.")

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)

    bal = last_off_for_user(uid)  
    ph_total_left, active = compute_ph_entries_active(uid)
    normal_bal = bal - ph_total_left

    lines = []
    lines.append(f"📊 Current Off Balance: {bal:.1f} day(s).")
    lines.append(f"🗂 Normal OIL Balance: {normal_bal:.1f} day(s)")
    lines.append(f"🏖 PH Off Total: {ph_total_left:.1f} day(s)")
    if active:
        lines.append("🔎 PH Off Entries (active):")
        for c in active:
            lines.append(f"• {c['date']}: +{c['qty']:.1f} (exp {c['expiry']}) - {c['reason']}")
    else:
        lines.append("🔎 PH Off Entries (active): none")

    await reply_quiet(update, "\n".join(lines))

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    rows = get_all_rows()
    urows = [r for r in rows if len(r) > 1 and r[1] == uid]
    if not urows:
        await reply_quiet(update, "📜 No logs found.")
        return
    last5 = urows[-5:]
    out = []
    for r in last5:
        ts = r[0] if len(r) > 0 else ""
        action = r[3] if len(r) > 3 else ""
        delta = r[5] if len(r) > 5 else ""
        final = r[6] if len(r) > 6 else ""
        remarks = r[9] if len(r) > 9 else ""
        out.append(f"{ts} | {action} | {delta} → {final} | {remarks}")
    await reply_quiet(update, "📜 Your last 5 OIL logs:\n\n" + "\n".join(out))

# ------------------- Generic 1:1 flows (normal + PH) -------------------------
async def start_flow_days(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: str, action: str, is_ph: bool):
    uid = update.effective_user.id
    sid = str(uuid4())[:10]
    user_state[uid] = {
        "sid": sid,
        "flow": flow,               # 'normal' or 'ph'
        "action": action,           # 'clockoff'|'claimoff'|'clockphoff'|'claimphoff'
        "stage": "awaiting_days",
        "group_id": update.effective_chat.id if update.effective_chat else None,
        "is_ph": is_ph,
        "owner_id": uid,            # guard against cross-user presses
    }
    icon = "🏖" if is_ph else ("🗂" if action.startswith("claim") else "🕒")
    await reply_quiet(
        update,
        f"{icon} How many {'PH ' if is_ph else ''}OIL days do you want to "
        f"{'clock' if 'clock' in action else 'claim'}? (0.5 to 3, in 0.5 steps)\n"
        f"– Date limits will be shown in the next step.",
        reply_markup=cancel_keyboard(sid)
    )

async def cmd_clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, "normal", "clockoff", False)

async def cmd_claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, "normal", "claimoff", False)

async def cmd_clockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, "ph", "clockphoff", True)

async def cmd_claimphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_flow_days(update, context, "ph", "claimphoff", True)

# ------------------- Admin overview ------------------------------------------

async def cmd_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Run /overview in the group.")
        return
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        if update.effective_user.id not in [a.user.id for a in admins if not a.user.is_bot]:
            await reply_quiet(update, "Only admins can use this.")
            return
    except Exception:
        pass

    rows = get_all_rows()
    seen = {}
    for r in rows[1:]:
        if len(r) < 3:
            continue
        tid = r[1].strip()
        name = r[2].strip() or tid
        if not tid.isdigit():
            continue
        seen[tid] = name

    if not seen:
        await reply_quiet(update, "No users found in sheet.")
        return

    lines = ["📋 *OIL Overview*", ""]
    for uid, name in sorted(seen.items(), key=lambda x: x[1].lower()):
        try:
            total = last_off_for_user(uid)                  # total balance (Final Off)
            ph_left, _ = compute_ph_entries_active(uid)     # PH balance
            normal = total - ph_left                        # derive Normal
            lines.append(
                f"• {name} ({uid}) — Total: {total:.1f}d | Normal: {normal:.1f}d | PH: {ph_left:.1f}d"
            )
        except Exception:
            lines.append(f"• {name} ({uid}) — Total: ? | Normal: ? | PH: ?")

    out = ""
    for line in lines:
        if len(out) + len(line) + 1 > 3500:
            try:
                await reply_quiet(update, out, parse_mode="Markdown")
            except Exception:
                await reply_quiet(update, out)
            out = ""
        out += (line + "\n")
    if out:
        try:
            await reply_quiet(update, out, parse_mode="Markdown")
        except Exception:
            await reply_quiet(update, out)
            
# ------------------- Onboarding /newuser -------------------------------------

async def cmd_newuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Please send /newuser *in the group* where records live.", parse_mode="Markdown")
        return

    uid = update.effective_user.id
    sid = str(uuid4())[:10]
    rows = get_all_rows()
    exists = any(len(r) > 1 and r[1] == str(uid) for r in rows)
    if exists:
        await reply_quiet(update, "You already have records here. Import is only for brand-new users.")
        return

    user_state[uid] = {
        "sid": sid,
        "flow": "newuser",
        "stage": "awaiting_normal_days",
        "group_id": chat.id,
        "newuser": {
            "normal_days": None,
            "ph_entries": [],
        },
        "owner_id": uid,
    }
    await reply_quiet(
        update,
        "🆕 *Onboarding: Import Old Records*\n\n"
        "1) How many *normal OIL* days to import? (Enter a number, e.g. 7.5 or 0 if none)",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(sid)
    )

# -----------------------------------------------------------------------------
# Message handler (free-text steps)
# -----------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    text = update.message.text.strip()

    if text.lower() == "-quit":
        user_state.pop(uid, None)
        await reply_quiet(update, "🧹 Cancelled.")
        return

    st = user_state.get(uid)
    if not st:
        return

    # ---- Days -> Date -> Remarks (single & mass) ----
    if st["stage"] == "awaiting_days":
        try:
            days = float(text)
            if days <= 0:
                raise ValueError()
            if st["flow"] in ("normal", "ph", "mass_normal", "mass_ph"):
                if not (0.5 <= days <= 3.0) or not validate_half_step(days):
                    raise ValueError()
        except ValueError:
            await reply_quiet(update, "❌ Invalid input. Enter 0.5 to 3.0 in 0.5 steps.", reply_markup=cancel_keyboard(st["sid"]))
            return

        st["days"] = days
        cur = date.today()

        # Set date limits
        past_365 = date.today() - timedelta(days=365)
        if st["flow"].startswith("mass_"):
            st["stage"] = "awaiting_mass_date"
            st["min_date"] = past_365
            st["max_date"] = date.today()
            await reply_quiet(
                update,
                f"{bold('📅 Select the Application Date for the mass action:')}\n"
                f"• Tap a date below, or tap {bold('Manual entry')} to type YYYY-MM-DD.\n"
                f"• Allowed date range (clocking): {st['min_date']} to {st['max_date']}",
                parse_mode="Markdown",
                reply_markup=build_calendar(st["sid"], cur, st["min_date"], st["max_date"])
            )
            return

        st["stage"] = "awaiting_app_date"
        is_claim = st.get("action") in ("claimoff", "claimphoff")
        st["min_date"] = past_365
        st["max_date"] = date.today() + (timedelta(days=365) if is_claim else timedelta(days=0))
        await reply_quiet(
            update,
            f"{bold('📅 Select Application Date:')}\n"
            f"• Tap a date below, or tap {bold('Manual entry')} to type YYYY-MM-DD.\n"
            f"• Allowed date range: {st['min_date']} to {st['max_date']}",
            parse_mode="Markdown",
            reply_markup=build_calendar(st["sid"], cur, st["min_date"], st["max_date"])
        )
        return

    # ---- remarks after date (single) ----
    if st["flow"] in ("normal", "ph") and st["stage"] == "awaiting_reason":
        txt = text.strip()
        action = st.get("action","")
        optional = action in ("claimoff", "claimphoff")
        if optional:
            st["reason"] = ("—" if txt.lower() == "nil" or txt == "" else txt[:80])
        else:
            if not txt or txt.lower() == "nil":
                await reply_quiet(update, "❌ Remarks required. Please provide a short reason (max 80 chars).", reply_markup=cancel_keyboard(st["sid"]))
                return
            st["reason"] = txt[:80]
        await finalize_single_request(update, context, st, st.get("app_date",""))
        return

    # ---- remarks after date (mass) ----
    if st["flow"].startswith("mass_") and st["stage"] == "awaiting_mass_remarks":
        st["reason"] = text[:80]
        await mass_preview_and_confirm(update, context, st)
        return

    # ---- /newuser flow ----
    if st["flow"] == "newuser":
        nu = st["newuser"]
        if st["stage"] == "awaiting_normal_days":
            try:
                nd = float(text)
                if nd < 0:
                    raise ValueError()
            except ValueError:
                await reply_quiet(update, "Please enter a non-negative number (e.g., 0, 6, 7.5).", reply_markup=cancel_keyboard(st["sid"]))
                return
            nu["normal_days"] = nd
            st["stage"] = "ph_ask_count"
            await reply_quiet(
                update,
                "Now we’ll import *PH OIL* entries.\n"
                "How many PH entries do you want to add? (0–10)\n"
                "You’ll add them one-by-one with date + PH name.",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard(st["sid"])
            )
            return

        if st["stage"] == "ph_ask_count":
            try:
                cnt = int(text)
                if cnt < 0 or cnt > 10:
                    raise ValueError()
            except ValueError:
                await reply_quiet(update, "Enter an integer between 0 and 10.", reply_markup=cancel_keyboard(st["sid"]))
                return
            nu["ph_count"] = cnt
            if cnt == 0:
                st["stage"] = "review_submit"
                await newuser_review(update, context, st)
            else:
                st["ph_idx"] = 0
                st["stage"] = "ph_date"
                st["min_date"] = date.today() - timedelta(days=365)
                st["max_date"] = date.today()
                cur = date.today()
                await reply_quiet(
                    update,
                    f"PH Entry 1/{nu['ph_count']} — {bold('Select Application Date')} (YYYY-MM-DD)\n"
                    f"• Allowed date range (clocking): {st['min_date']} to {st['max_date']}",
                    parse_mode="Markdown",
                    reply_markup=build_calendar(st["sid"], cur, st["min_date"], st["max_date"])
                )
            return

        elif st["stage"] == "ph_reason":
            idx = st["ph_idx"]
            txt = text.strip()
            if not txt or txt.lower() == "nil":
                await reply_quiet(update, "❌ PH name is required. Please enter the PH name (e.g., National Day 2025).", reply_markup=cancel_keyboard(st["sid"]))
                return
            nu["ph_entries"][idx]["reason"] = txt[:80]
            idx += 1
            if idx < nu["ph_count"]:
                st["ph_idx"] = idx
                st["stage"] = "ph_date"
                st["min_date"] = date.today() - timedelta(days=365)
                st["max_date"] = date.today()
                cur = date.today()
                await reply_quiet(
                    update,
                    f"PH Entry {idx+1}/{nu['ph_count']} — {bold('Select Application Date')} (YYYY-MM-DD)\n"
                    f"• Allowed date range (clocking): {st['min_date']} to {st['max_date']}",
                    parse_mode="Markdown",
                    reply_markup=build_calendar(st["sid"], cur, st["min_date"], st["max_date"])
                )
            else:
                st["stage"] = "review_submit"
                await newuser_review(update, context, st)
            return

    # ---- manual date entry (single) ----
    if st.get("stage") == "awaiting_app_date_manual":
        d = parse_date_yyyy_mm_dd(text)
        if not d:
            await reply_quiet(update, "Invalid date. Please type YYYY-MM-DD.", reply_markup=cancel_keyboard(st["sid"]))
            return
        ok, msg = validate_application_date(st.get("action",""), d)
        if not ok:
            await reply_quiet(update, msg, reply_markup=cancel_keyboard(st["sid"]))
            return
        st["app_date"] = d
        st["stage"] = "awaiting_reason"
        if st.get("action") == "clockoff":
            prompt = "📝 Enter clocking reason (e.g., OT number, event name)."
        elif st.get("action") == "clockphoff":
            prompt = "📝 Enter PH name (e.g., National Day 2025)."
        elif st.get("action") == "claimoff":
            prompt = "📝 Enter remarks (optional). Type 'nil' to skip."
        else:
            prompt = "📝 Enter remarks (optional). Type 'nil' to skip."
        await reply_quiet(update, prompt, reply_markup=cancel_keyboard(st["sid"]))
        return

    # ---- manual date entry (mass) ----
    if st.get("stage") == "awaiting_mass_date_manual":
        d = parse_date_yyyy_mm_dd(text)
        if not d:
            await reply_quiet(update, "Invalid date. Please type YYYY-MM-DD.", reply_markup=cancel_keyboard(st["sid"]))
            return
        ok, msg = validate_application_date("mass", d)
        if not ok:
            await reply_quiet(update, msg, reply_markup=cancel_keyboard(st["sid"]))
            return
        st["app_date"] = d
        st["stage"] = "awaiting_mass_remarks"
        await reply_quiet(update, "📝 Enter remarks for the mass action (reason or PH name).", reply_markup=cancel_keyboard(st["sid"]))
        return

    # ---- manual date entry for /newuser PH step ----
    if st.get("stage") == "ph_date_manual":
        d = parse_date_yyyy_mm_dd(text)
        if not d:
            await reply_quiet(update, "Invalid date. Please type YYYY-MM-DD.", reply_markup=cancel_keyboard(st["sid"]))
            return
        ok, msg = validate_application_date("newuser_ph", d)
        if not ok:
            await reply_quiet(update, msg, reply_markup=cancel_keyboard(st["sid"]))
            return
        nu = st["newuser"]
        idx = st.get("ph_idx", 0)
        nu["ph_entries"].append({"date": d, "reason": None})
        st["stage"] = "ph_reason"
        await reply_quiet(update, f"PH Entry {idx+1}/{nu['ph_count']} — Enter {bold('PH name')} (max 80 chars):", parse_mode="Markdown", reply_markup=cancel_keyboard(st["sid"]))
        return

# -----------------------------------------------------------------------------
# Callback handler
# -----------------------------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    parts = data.split("|")
    if not parts:
        return

    kind = parts[0]
    sid = parts[1] if len(parts) > 1 else ""

    uid = q.from_user.id
    st = user_state.get(uid)

    # Only the flow owner can operate inline controls
    def _not_owner_block():
        return (not st) or (st.get("sid") != sid) or (st.get("owner_id") != uid)

    if kind == "cancel":
        if _not_owner_block():
            await q.answer("This isn’t your session.", show_alert=True)
            return
        user_state.pop(uid, None)
        try:
            await q.edit_message_text("🧹 Cancelled.")
        except Exception:
            pass
        return

    if kind == "noop":
        return

    # Calendar navigation / selection requires active state & ownership
    if kind in ("calnav", "manual", "cal"):
        if _not_owner_block():
            await q.answer("This isn’t your session.", show_alert=True)
            return

    if kind == "calnav":
        try:
            target = datetime.strptime(parts[2], "%Y-%m-%d").date()
        except Exception:
            target = date.today()
        min_d = st.get("min_date")
        max_d = st.get("max_date")
        try:
            await q.edit_message_reply_markup(reply_markup=build_calendar(sid, target, min_d, max_d))
        except Exception:
            await q.edit_message_text(
                f"{bold('📅 Select Application Date:')}\n• Tap a date below, or\n• Tap {bold('Manual entry')}, then type YYYY-MM-DD.",
                parse_mode="Markdown",
                reply_markup=build_calendar(sid, target, min_d, max_d)
            )
        return

    if kind == "manual":
        if st["flow"] in ("normal", "ph") and st["stage"] == "awaiting_app_date":
            st["stage"] = "awaiting_app_date_manual"
            await q.edit_message_text("⌨️ Type the application date as YYYY-MM-DD.", reply_markup=cancel_keyboard(sid))
            return
        if st["flow"].startswith("mass_") and st["stage"] == "awaiting_mass_date":
            st["stage"] = "awaiting_mass_date_manual"
            await q.edit_message_text("⌨️ Type the mass application date as YYYY-MM-DD.", reply_markup=cancel_keyboard(sid))
            return
        if st["flow"] == "newuser" and st["stage"] == "ph_date":
            st["stage"] = "ph_date_manual"
            await q.edit_message_text("⌨️ Type the PH application date as YYYY-MM-DD.", reply_markup=cancel_keyboard(sid))
            return
        return

    if kind == "cal":
        chosen = parts[2]
        if st["flow"] in ("normal", "ph") and st["stage"] == "awaiting_app_date":
            ok, msg = validate_application_date(st.get("action",""), chosen)
            if not ok:
                await q.answer(msg, show_alert=True)
                return
            st["app_date"] = chosen
            try:
                await q.edit_message_text(f"📅 Application Date: {chosen}")
            except Exception:
                pass
            st["stage"] = "awaiting_reason"
            if st.get("action") == "clockoff":
                prompt = "📝 Enter clocking reason (e.g., OT number, event name)."
            elif st.get("action") == "clockphoff":
                prompt = "📝 Enter PH name (e.g., National Day 2025)."
            elif st.get("action") == "claimoff":
                prompt = "📝 Enter remarks (optional). Type 'nil' to skip."
            else:
                prompt = "📝 Enter remarks (optional). Type 'nil' to skip."
            if update.effective_chat and _is_group(update.effective_chat.type):
                await send_group_quiet(context, q.message.chat.id, prompt, reply_markup=cancel_keyboard(st["sid"]))
            else:
                await context.bot.send_message(chat_id=q.message.chat.id, text=prompt, reply_markup=cancel_keyboard(st["sid"]))
            return

        if st["flow"].startswith("mass_") and st["stage"] == "awaiting_mass_date":
            ok, msg = validate_application_date("mass", chosen)
            if not ok:
                await q.answer(msg, show_alert=True)
                return
            st["app_date"] = chosen
            try:
                await q.edit_message_text(f"📅 Mass Application Date: {chosen}")
            except Exception:
                pass
            st["stage"] = "awaiting_mass_remarks"
            await send_group_quiet(context, q.message.chat.id, "📝 Enter remarks for the mass action (reason or PH name).", reply_markup=cancel_keyboard(st["sid"]))
            return

        if st["flow"] == "newuser" and st["stage"] == "ph_date":
            ok, msg = validate_application_date("newuser_ph", chosen)
            if not ok:
                await q.answer(msg, show_alert=True)
                return
            nu = st["newuser"]
            idx = st["ph_idx"]
            nu["ph_entries"].append({"date": chosen, "reason": None})
            try:
                await q.edit_message_text(f"📅 PH Entry {idx+1}/{nu['ph_count']} — Date: {chosen}")
            except Exception:
                pass
            st["stage"] = "ph_reason"
            await send_group_quiet(context, q.message.chat.id, f"PH Entry {idx+1}/{nu['ph_count']} — Enter *PH name* (max 80 chars):", parse_mode="Markdown", reply_markup=cancel_keyboard(sid))
            return

    if kind == "massgo" and st and st.get("stage") == "mass_confirm":
        if _not_owner_block():
            await q.answer("This isn’t your session.", show_alert=True)
            return
        await mass_send_to_admins(update, context, st)
        try:
            await q.edit_message_text("Submitted to admins for approval.")
        except Exception:
            pass
        user_state.pop(uid, None)
        return

    # Approve/deny (admin PM)
    if kind in ("approve", "deny"):
        key = parts[1] if len(parts) > 1 else ""
        payload = pending_payloads.pop(key, None)
        approver = q.from_user.full_name
        approver_id = q.from_user.id
        if not payload:
            try:
                await q.edit_message_text("⚠️ This request has already been handled.")
            except Exception:
                pass
            return

        if payload.get("type") == "newuser":
            await handle_newuser_apply(update, context, payload, kind == "approve", approver, approver_id)
            summary = build_admin_summary_text(payload, approved=(kind=="approve"), approver_name=approver, final_off=None)
            try:
                await q.edit_message_text(summary)
            except Exception:
                pass
            return

        if payload.get("type") == "mass":
            await handle_mass_apply(context, payload, kind == "approve", approver, approver_id)
            summary = build_admin_summary_text(payload, approved=(kind=="approve"), approver_name=approver, final_off=None)
            try:
                await q.edit_message_text(summary)
            except Exception:
                pass
            return

        if payload.get("type") in ("single",):
            await handle_single_apply(update, context, payload, kind == "approve", approver, approver_id)
            final_off = None
            if kind == "approve":
                cur = last_off_for_user(payload["user_id"])
                calc = cur + (payload["days"] if "clock" in payload["action"] else -payload["days"])
                final_off = calc
            try:
                await q.edit_message_text(build_admin_summary_text(payload, approved=(kind=="approve"), approver_name=approver, final_off=final_off))
            except Exception:
                pass
            return

# -----------------------------------------------------------------------------
# Finalize single (normal or PH) -> send to admins
# -----------------------------------------------------------------------------
async def finalize_single_request(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str,Any], app_date: str):
    uid = update.effective_user.id
    user = update.effective_user
    group_id = st.get("group_id") or (update.effective_chat.id if update.effective_chat else None)

    days = float(st["days"])
    if days <= 0 or not validate_half_step(days):
        await reply_quiet(update, "❌ Days must be positive and in 0.5 steps.")
        user_state.pop(uid, None)
        return

    current_off = last_off_for_user(str(uid))
    add = +days if st["action"] in ("clockoff", "clockphoff") else -days
    final = current_off + add
    is_ph = st["is_ph"]
    app_date = app_date or st.get("app_date","")

    # date window double-check (defense-in-depth)
    tag = st["action"]
    ok, msg = validate_application_date(tag, app_date)
    if not ok:
        await reply_quiet(update, msg)
        return

    expiry = ""
    ph_total_after = ""
    if is_ph:
        if st["action"] == "clockphoff":
            try:
                d = datetime.strptime(app_date, "%Y-%m-%d").date()
                expiry = (d + timedelta(days=365)).strftime("%Y-%m-%d")
            except Exception:
                expiry = ""
        before, _ = compute_ph_entries_active(str(uid))
        ph_total_after = before + (days if st["action"] == "clockphoff" else -days)

    key = str(uuid4())[:12]
    payload = {
        "type": "single",
        "user_id": str(uid),
        "user_name": user.full_name,
        "group_id": group_id,
        "action": st["action"],
        "days": days,
        "reason": st.get("reason", ""),
        "app_date": app_date,
        "current_off": current_off,
        "final_off": final,
        "is_ph": is_ph,
        "expiry": expiry,
        "ph_total_after": ph_total_after if ph_total_after != "" else None,
        "admin_msgs": []
    }

    # send to admins and store PM refs
    try:
        admins = await context.bot.get_chat_administrators(group_id)
    except Exception:
        admins = []

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{key}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{key}")
    ]])

    label = (
        "Clock Off" if st["action"]=="clockoff" else
        "Claim Off" if st["action"]=="claimoff" else
        "Clock Off (PH)" if st["action"]=="clockphoff" else
        "Claim Off (PH)"
    )

    text = (
        f"🆕 *{label} Request*\n\n"
        f"👤 User: {user.full_name} ({uid})\n"
        f"📅 Days: {days}\n"
        f"🗓 Application Date: {app_date}\n"
        f"📝 Reason: {st.get('reason','') or '—'}\n\n"
        f"📊 Current Off: {current_off:.1f}\n"
        f"📈 New Balance: {final:.1f}"
    )
    if is_ph and expiry:
        text += f"\n🏖 PH Expiry: {expiry}"
        if payload.get("ph_total_after") is not None:
            text += f"\n🏖 PH Total After: {payload['ph_total_after']:.1f}"

    sent_any = False
    admin_msgs = []
    for a in admins:
        if a.user.is_bot:
            continue
        try:
            msg = await context.bot.send_message(chat_id=a.user.id, text=text, parse_mode="Markdown", reply_markup=kb)
            admin_msgs.append((a.user.id, msg.message_id))
            sent_any = True
        except Exception:
            pass

    payload["admin_msgs"] = admin_msgs
    pending_payloads[key] = payload

    if sent_any:
        await send_group_quiet(context, group_id, "📩 Request submitted to admins for approval.")
    else:
        await send_group_quiet(context, group_id, "⚠️ Could not reach any admin. Please ensure the bot can PM admins.")

    user_state.pop(uid, None)

# -----------------------------------------------------------------------------
# Apply single (admin approve/deny) + send receipts + edit all admin PMs
# -----------------------------------------------------------------------------
async def handle_single_apply(update: Update, context: ContextTypes.DEFAULT_TYPE, p: Dict[str,Any], approved: bool, approver_name: str, approver_id: int):
    gid = p.get("group_id")
    uid = p["user_id"]
    uname = p["user_name"]
    action = p["action"]
    days = p["days"]
    reason = p["reason"]
    app_date = p["app_date"]
    is_ph = p["is_ph"]
    expiry = p.get("expiry")

    if not approved:
        try:
            await send_group_quiet(context, gid, f"❌ Request by {uname} denied by {approver_name}.\n📝 Reason: {reason or '—'}")
        except Exception:
            pass
        summary = build_admin_summary_text(p, approved=False, approver_name=approver_name, final_off=None)
        await update_all_admin_pm(context, p, summary)
        return

    current_off = last_off_for_user(uid)
    add = +days if "clock" in action else -days
    final = current_off + add

    ph_total_left, _ = compute_ph_entries_active(uid)
    ph_total_after = ph_total_left + (days if action == "clockphoff" else (-days if action == "claimphoff" else 0))
    if not is_ph:
        ph_total_after = 0.0

    try:
        append_row(
            user_id=uid,
            user_name=uname,
            action=("Clock Off" if action.startswith("clock") else "Claim Off"),
            current_off=current_off,
            add_subtract=add,
            final_off=final,
            approved_by=approver_name,
            application_date=app_date,
            remarks=reason or "—",
            is_ph=is_ph,
            ph_total=ph_total_after if is_ph else 0.0,
            expiry=expiry if is_ph else ""
        )
    except Exception:
        log.exception("Failed to append row for single apply")

    msg = (
        f"✅ {uname}'s {('PH ' if is_ph else '')}{'Clock Off' if 'clock' in action else 'Claim Off'} approved by {approver_name}.\n"
        f"🗓 Application Date: {app_date}\n"
        f"📅 Days: {days}\n"
        f"📝 Reason: {reason or '—'}\n"
        f"📊 Final: {final:.1f} day(s)"
    )
    if is_ph and expiry:
        msg += f"\n🏖 PH Expiry: {expiry}"
    try:
        await send_group_quiet(context, gid, msg)
    except Exception:
        pass

    summary = build_admin_summary_text(p, approved=True, approver_name=approver_name, final_off=final)
    await update_all_admin_pm(context, p, summary)

# -----------------------------------------------------------------------------
# Mass preview & apply
# -----------------------------------------------------------------------------
async def mass_preview_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]):
    chat_id = st["group_id"]
    rows = get_all_rows()
    seen = {}
    for r in rows[1:]:
        if len(r) < 3:
            continue
        tid = r[1].strip()
        name = r[2].strip()
        if not tid.isdigit():
            continue
        seen[tid] = name or tid
    if not seen:
        await send_group_quiet(context, chat_id, "No users found in sheet to mass clock.")
        user_state.pop(update.effective_user.id, None)
        return

    listing = "\n".join([f"- {n} ({t})" for t, n in seen.items()])
    st["mass_targets"] = [{"user_id": t, "name": n} for t, n in seen.items()]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Proceed", callback_data=f"massgo|{st['sid']}"),
                                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{st['sid']}")]])
    await send_group_quiet(
        context,
        chat_id,
        f"🔍 *Dry-run preview* ({len(seen)} users)\nDays per user: {st['days']}\nDate: {st.get('app_date','')}\nRemarks: {st.get('reason','')}\n\n{listing}",
        parse_mode="Markdown",
        reply_markup=kb
    )
    st["stage"] = "mass_confirm"

async def cmd_massclockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Run this in the group you want to affect.")
        return
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        if update.effective_user.id not in [a.user.id for a in admins if not a.user.is_bot]:
            await reply_quiet(update, "Only admins can use this.")
            return
    except Exception:
        pass

    sid = str(uuid4())[:10]
    user_state[update.effective_user.id] = {
        "sid": sid,
        "flow": "mass_normal",
        "stage": "awaiting_days",
        "group_id": chat.id,
        "is_ph": False,
        "owner_id": update.effective_user.id,
    }
    await reply_quiet(
        update,
        "👥 Mass Clock *normal* OIL — How many days per user? (0.5 to 3, in 0.5 steps)\n"
        "You’ll next choose a date (allowed: today-365 to today) and set a remark.",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(sid)
    )

async def cmd_massclockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Run this in the group you want to affect.")
        return
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        if update.effective_user.id not in [a.user.id for a in admins if not a.user.is_bot]:
            await reply_quiet(update, "Only admins can use this.")
            return
    except Exception:
        pass

    sid = str(uuid4())[:10]
    user_state[update.effective_user.id] = {
        "sid": sid,
        "flow": "mass_ph",
        "stage": "awaiting_days",
        "group_id": chat.id,
        "is_ph": True,
        "owner_id": update.effective_user.id,
    }
    await reply_quiet(
        update,
        "👥 Mass Clock *PH* OIL — How many days per user? (0.5 to 3, in 0.5 steps)\n"
        "You’ll next choose a date (allowed: today-365 to today) and set a PH name.",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(sid)
    )

# -----------------------------------------------------------------------------
# Newuser review & apply
# -----------------------------------------------------------------------------
async def newuser_review(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str,Any], via_edit: Optional[Any]=None):
    nu = st["newuser"]
    uid = update.effective_user.id
    uname = update.effective_user.full_name
    gid = st["group_id"]

    lines = [f"👤 {uname} ({uid})"]
    lines.append(f"Normal OIL days to import: {nu['normal_days']}")
    lines.append(f"PH entries: {len(nu['ph_entries'])}")
    for e in nu["ph_entries"]:
        lines.append(f"  • {e['date']} — {e['reason']}")

    key = str(uuid4())[:12]
    payload = {
        "type": "newuser",
        "group_id": gid,
        "user_id": str(uid),
        "user_name": uname,
        "normal_days": float(nu["normal_days"] or 0.0),
        "ph_entries": nu["ph_entries"],
        "admin_msgs": []
    }

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve|{key}"),
                                InlineKeyboardButton("❌ Deny", callback_data=f"deny|{key}")]])

    txt = "🔎 *Import Review*\n" + "\n".join(lines)

    try:
        admins = await context.bot.get_chat_administrators(gid)
    except Exception:
        admins = []
    sent = False
    admin_msgs = []
    for a in admins:
        if a.user.is_bot:
            continue
        try:
            msg = await context.bot.send_message(chat_id=a.user.id, text=txt, parse_mode="Markdown", reply_markup=kb)
            admin_msgs.append((a.user.id, msg.message_id))
            sent = True
        except Exception:
            pass

    payload["admin_msgs"] = admin_msgs
    pending_payloads[key] = payload

    if sent:
        if via_edit:
            await via_edit.edit_message_text("Submitted to admins for approval.")
        else:
            await send_group_quiet(context, gid, "Submitted to admins for approval.")
    else:
        if via_edit:
            await via_edit.edit_message_text("⚠️ Couldn’t reach any admin.")
        else:
            await send_group_quiet(context, gid, "⚠️ Couldn’t reach any admin.")
    user_state.pop(uid, None)

async def handle_newuser_apply(update: Update, context: ContextTypes.DEFAULT_TYPE, p: Dict[str,Any], approved: bool, approver_name: str, approver_id: int):
    gid = p["group_id"]
    uid = p["user_id"]
    uname = p["user_name"]
    normal_days = float(p.get("normal_days", 0.0))
    ph_entries = p.get("ph_entries", [])

    if not approved:
        try:
            await send_group_quiet(context, gid, f"❌ Onboarding import for {uname} denied by {approver_name}.")
        except Exception:
            pass
        summary = build_admin_summary_text(p, approved=False, approver_name=approver_name, final_off=None)
        await update_all_admin_pm(context, p, summary)
        return

    if normal_days > 0:
        current = last_off_for_user(uid)
        add = +normal_days
        final = current + add
        try:
            append_row(
                user_id=uid,
                user_name=uname,
                action="Clock Off",
                current_off=current,
                add_subtract=add,
                final_off=final,
                approved_by=approver_name,
                application_date=date.today().strftime("%Y-%m-%d"),
                remarks="Transfer from old record",
                is_ph=False,
                ph_total=0.0,
                expiry=""
            )
        except Exception:
            log.exception("Failed to append normal import for newuser")

    for e in ph_entries:
        dstr = e.get("date")
        reason = e.get("reason", "")
        if not dstr:
            continue
        current = last_off_for_user(uid)
        add = +1.0
        final = current + add
        d = parse_date_yyyy_mm_dd(dstr)
        exp = ""
        try:
            dt = datetime.strptime(dstr, "%Y-%m-%d").date()
            exp = (dt + timedelta(days=365)).strftime("%Y-%m-%d")
        except Exception:
            pass
        before, _ = compute_ph_entries_active(uid)
        ph_after = before + 1.0
        try:
            append_row(
                user_id=uid,
                user_name=uname,
                action="Clock Off",
                current_off=current,
                add_subtract=add,
                final_off=final,
                approved_by=approver_name,
                application_date=d or date.today().strftime("%Y-%m-%d"),
                remarks=reason,
                is_ph=True,
                ph_total=ph_after,
                expiry=exp
            )
        except Exception:
            log.exception("Failed to append PH import for newuser")

    try:
        await send_group_quiet(context, gid, f"✅ Onboarding import for {uname} approved by {approver_name}.")
    except Exception:
        pass

    summary = build_admin_summary_text(p, approved=True, approver_name=approver_name, final_off=None)
    await update_all_admin_pm(context, p, summary)

# -----------------------------------------------------------------------------
# Mass apply
# -----------------------------------------------------------------------------
async def mass_send_to_admins(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str,Any]):
    gid = st["group_id"]
    days = st["days"]
    is_ph = st["is_ph"]
    targets = st["mass_targets"]

    key = str(uuid4())[:12]
    payload = {
        "type": "mass",
        "group_id": gid,
        "days": days,
        "is_ph": is_ph,
        "targets": targets,
        "admin_msgs": [],
        "reason": st.get("reason",""),
        "app_date": st.get("app_date",""),
    }

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve|{key}"),
                                InlineKeyboardButton("❌ Deny", callback_data=f"deny|{key}")]])

    label = "Mass Clock PH" if is_ph else "Mass Clock"
    listing = "\n".join([f"- {t['name']} ({t['user_id']})" for t in targets])
    txt = (
        f"🆕 *{label}* — Days per user: {days}\n"
        f"🗓 Date: {payload['app_date']}\n"
        f"📝 Remarks: {payload['reason']}\n\n"
        f"{listing}\n\nProceed?"
    )

    try:
        admins = await context.bot.get_chat_administrators(gid)
    except Exception:
        admins = []
    sent = False
    admin_msgs = []
    for a in admins:
        if a.user.is_bot:
            continue
        try:
            msg = await context.bot.send_message(chat_id=a.user.id, text=txt, parse_mode="Markdown", reply_markup=kb)
            admin_msgs.append((a.user.id, msg.message_id))
            sent = True
        except Exception:
            pass

    payload["admin_msgs"] = admin_msgs
    pending_payloads[key] = payload

    if sent:
        await send_group_quiet(context, gid, "📩 Mass request sent to admins.")
    else:
        await send_group_quiet(context, gid, "⚠️ Couldn’t DM any admins.")

async def handle_mass_apply(context: ContextTypes.DEFAULT_TYPE, p: Dict[str,Any], approved: bool, approver_name: str, approver_id: int):
    gid = p["group_id"]
    days = p["days"]
    is_ph = p["is_ph"]
    targets = p["targets"]
    label = "Mass Clock PH" if is_ph else "Mass Clock"

    if not approved:
        try:
            await send_group_quiet(context, gid, f"❌ {label} denied by {approver_name}.")
        except Exception:
            pass
        summary = build_admin_summary_text(p, approved=False, approver_name=approver_name, final_off=None)
        await update_all_admin_pm(context, p, summary)
        return

    count_ok = 0
    for t in targets:
        uid = t["user_id"]
        uname = t["name"]
        current_off = last_off_for_user(uid)
        add = +days
        final = current_off + add

        expiry = ""
        ph_total_after = 0.0
        if is_ph:
            today = date.today()
            expiry = (today + timedelta(days=365)).strftime("%Y-%m-%d")
            before, _ = compute_ph_entries_active(uid)
            ph_total_after = before + days

        try:
            append_row(
                user_id=uid,
                user_name=uname,
                action="Clock Off",
                current_off=current_off,
                add_subtract=add,
                final_off=final,
                approved_by=approver_name,
                application_date=p.get("app_date", date.today().strftime("%Y-%m-%d")),
                remarks=p.get("reason","Mass clock"),
                is_ph=is_ph,
                ph_total=ph_total_after if is_ph else 0.0,
                expiry=expiry if is_ph else ""
            )
            count_ok += 1
        except Exception:
            log.exception("Mass append failed for %s", uid)

    try:
        await send_group_quiet(context, gid, f"✅ {label} approved by {approver_name}. Processed {count_ok}/{len(targets)} users.")
    except Exception:
        pass

    summary = build_admin_summary_text(p, approved=True, approver_name=approver_name, final_off=None)
    await update_all_admin_pm(context, p, summary)

# -----------------------------------------------------------------------------
# Webhook endpoints
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return "✅ Oil Tracking Bot is up."

@app.route("/health")
def health():
    return "✅ Health check passed."

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app
    if telegram_app is None:
        return "Bot not ready", 503

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        log.info(f"📨 Incoming update: {request.get_json(force=True)}")
        future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
        future.add_done_callback(lambda f: f.exception())
        return "OK"
    except Exception:
        log.exception("Error processing update")
        return "Internal Server Error", 500

# -----------------------------------------------------------------------------
# Init & run
# -----------------------------------------------------------------------------
async def init_app():
    global telegram_app, worksheet
    gsheet_init()

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).get_updates_http_version("1.1").build()

    telegram_app.add_handler(CommandHandler("help", cmd_help))
    telegram_app.add_handler(CommandHandler("startadmin", cmd_startadmin))
    telegram_app.add_handler(CommandHandler("summary", cmd_summary))
    telegram_app.add_handler(CommandHandler("history", cmd_history))
    telegram_app.add_handler(CommandHandler("overview", cmd_overview))

    telegram_app.add_handler(CommandHandler("clockoff", cmd_clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", cmd_claimoff))
    telegram_app.add_handler(CommandHandler("clockphoff", cmd_clockphoff))
    telegram_app.add_handler(CommandHandler("claimphoff", cmd_claimphoff))

    telegram_app.add_handler(CommandHandler("massclockoff", cmd_massclockoff))
    telegram_app.add_handler(CommandHandler("massclockphoff", cmd_massclockphoff))

    telegram_app.add_handler(CommandHandler("newuser", cmd_newuser))

    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    log.info("🚀 Webhook set.")

if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    log.info("🟢 Starting Flask…")
    app.run(host="0.0.0.0", port=10000)
