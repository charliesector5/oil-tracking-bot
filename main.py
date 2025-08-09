# main.py
import os
import re
import logging
import asyncio
import nest_asyncio
import gspread
import uuid
from datetime import datetime, timedelta, date
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

# Per-user conversation states
user_state = {}  # user_id -> dict
# Short tokens -> pending approval payloads (keeps callback_data tiny)
pending_tokens = {}  # token -> dict
# Admin PM message references for cleanup after one admin handles
admin_message_refs = {}  # token -> list[(admin_id, msg_id)]

MAX_REMARKS_LEN = 80

# ---------- Helpers ----------
def short_token() -> str:
    return uuid.uuid4().hex[:10]

def fmt_days(x: float) -> str:
    return f"{x:.1f}".rstrip('0').rstrip('.') if x % 1 else f"{int(x)}"

def is_valid_half_step(value: float) -> bool:
    # 0.5 steps: value*10 mod 5 == 0
    return (value * 10) % 5 == 0

def parse_date_yyyy_mm_dd(s: str) -> date | None:
    try:
        y, m, d = map(int, s.split("-"))
        return date(y, m, d)
    except Exception:
        return None

def display_name_for_member(member) -> str:
    if not member or not getattr(member, "user", None):
        return ""
    u = member.user
    if u.full_name:
        return u.full_name
    if u.username:
        return f"@{u.username}"
    return str(u.id)

def safe_user_display(context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int, fallback_name: str):
    # Try resolving display name in group; fallback to provided
    return fallback_name

def build_cancel_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{token}")]])

def build_calendar_markup(token: str, ref: date) -> InlineKeyboardMarkup:
    # Simple full-month grid; each cell callback is cal|<token>|YYYY-MM-DD
    # Week rows Sun..Sat
    first = ref.replace(day=1)
    start_wd = first.weekday()  # Mon=0..Sun=6
    # We want Sun first; shift to Sun=0..Sat=6
    sun_first_shift = (start_wd + 1) % 7
    # Days in month
    if first.month == 12:
        next_month = first.replace(year=first.year + 1, month=1, day=1)
    else:
        next_month = first.replace(month=first.month + 1, day=1)
    days_in_month = (next_month - first).days

    rows = []
    # header
    rows.append([InlineKeyboardButton(first.strftime("📅 %B %Y"), callback_data=f"noop|{token}")])
    # weekday header
    rows.append([InlineKeyboardButton(x, callback_data=f"noop|{token}") for x in ["Su","Mo","Tu","We","Th","Fr","Sa"]])

    day = 1
    # leading blanks
    week = []
    for i in range(sun_first_shift):
        week.append(InlineKeyboardButton(" ", callback_data=f"noop|{token}"))

    while day <= days_in_month:
        while len(week) < 7 and day <= days_in_month:
            d = date(first.year, first.month, day)
            week.append(InlineKeyboardButton(str(day), callback_data=f"cal|{token}|{d.isoformat()}"))
            day += 1
        if len(week) < 7:
            while len(week) < 7:
                week.append(InlineKeyboardButton(" ", callback_data=f"noop|{token}"))
        rows.append(week)
        week = []

    # nav
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_m = next_month
    rows.append([
        InlineKeyboardButton("« Prev", callback_data=f"calnav|{token}|{prev_month.isoformat()}"),
        InlineKeyboardButton("Manual entry", callback_data=f"manual|{token}"),
        InlineKeyboardButton("Next »", callback_data=f"calnav|{token}|{next_m.isoformat()}"),
    ])
    # cancel
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{token}")])
    return InlineKeyboardMarkup(rows)

async def dm_admins(context: ContextTypes.DEFAULT_TYPE, group_id: int, text: str, reply_markup: InlineKeyboardMarkup | None):
    admins = await context.bot.get_chat_administrators(group_id)
    sent = []
    for admin in admins:
        if admin.user.is_bot:
            continue
        try:
            msg = await context.bot.send_message(chat_id=admin.user.id, text=text, parse_mode="Markdown", reply_markup=reply_markup)
            sent.append((admin.user.id, msg.message_id))
        except Exception as e:
            logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")
    return sent

def sheet_get_user_rows(uid: int | str):
    all_data = worksheet.get_all_values()
    return [row for row in all_data if len(row) > 1 and row[1] == str(uid)], all_data

def get_current_off(uid: int | str) -> float:
    rows, _ = sheet_get_user_rows(uid)
    if rows:
        try:
            return float(rows[-1][6])  # Final Off
        except Exception:
            return 0.0
    return 0.0

def append_sheet_row(
    timestamp: str,
    uid: str,
    name: str,
    action_text: str,
    current_off: float,
    add_subtract: str,
    final_off: float,
    approved_by: str,
    app_date: str,
    remarks: str,
    holiday_off: str,
    ph_total: str,
    expiry: str
):
    worksheet.append_row([
        timestamp, uid, name, action_text,
        f"{current_off:.1f}", add_subtract, f"{final_off:.1f}",
        approved_by, app_date, remarks, holiday_off, ph_total, expiry
    ])

def human_action(action: str) -> str:
    if action == "clockoff": return "Clock Off"
    if action == "claimoff": return "Claim Off"
    if action == "clockphoff": return "Clock PH Off"
    if action == "claimphoff": return "Claim PH Off"
    return action

# ---------- Commands ----------
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
        "/allsummary – Everyone’s balances at a glance (admin)\n"
        "/newuser – Import your past OIL and PH entries (onboarding)\n"
        "/startadmin – Start admin PM session\n"
        "/history – Your past 5 logs\n"
        "/help – Show this help message\n\n"
        "Tip: tap *Cancel* to abort any step.",
        parse_mode="Markdown"
    )

# start admin in PM only
async def startadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("ℹ️ Please PM me and run /startadmin there.")
        return
    await update.message.reply_text("✅ Admin session started. I’ll DM you requests to approve or deny.")

# normal clock/claim
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = short_token()
    user_state[update.effective_user.id] = {
        "flow": "normal", "action": "clockoff", "stage": "awaiting_days",
        "token": token, "group_id": update.effective_chat.id
    }
    await update.message.reply_text(
        "🕓 How many days do you want to *clock off*? (0.5 to 3, in 0.5 steps)",
        parse_mode="Markdown",
        reply_markup=build_cancel_markup(token)
    )

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = short_token()
    user_state[update.effective_user.id] = {
        "flow": "normal", "action": "claimoff", "stage": "awaiting_days",
        "token": token, "group_id": update.effective_chat.id
    }
    await update.message.reply_text(
        "📥 How many days do you want to *claim off*? (0.5 to 3, in 0.5 steps)",
        parse_mode="Markdown",
        reply_markup=build_cancel_markup(user_state[update.effective_user.id]["token"])
    )

# PH clock/claim
async def clockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = short_token()
    user_state[update.effective_user.id] = {
        "flow": "ph", "action": "clockphoff", "stage": "awaiting_days",
        "token": token, "group_id": update.effective_chat.id
    }
    await update.message.reply_text(
        "🏖️ How many *PH OIL* days to *clock*? (0.5 to 3, in 0.5 steps)",
        parse_mode="Markdown",
        reply_markup=build_cancel_markup(token)
    )

async def claimphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = short_token()
    user_state[update.effective_user.id] = {
        "flow": "ph", "action": "claimphoff", "stage": "awaiting_days",
        "token": token, "group_id": update.effective_chat.id
    }
    await update.message.reply_text(
        "🏖️📥 How many *PH OIL* days to *claim*? (0.5 to 3, in 0.5 steps)",
        parse_mode="Markdown",
        reply_markup=build_cancel_markup(token)
    )

# history/summary
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        rows = [r for r in all_data if len(r) > 1 and r[1] == str(user.id)]
        if rows:
            last_5 = rows[-5:]
            response = "\n".join([f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | {row[8]}" for row in last_5])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        all_rows = worksheet.get_all_values()
        my = [r for r in all_rows if len(r) > 1 and r[1] == str(uid)]
        if not my:
            await update.message.reply_text("📊 No records yet.")
            return
        bal = float(my[-1][6]) if my[-1][6] else 0.0

        # PH outstanding list (sum PH Off Total per application date, only positive)
        ph_rows = [r for r in my if len(r) >= 12 and (len(r) > 10 and (r[10] or "").strip().lower() == "yes")]
        from collections import defaultdict
        per_date = defaultdict(float)
        per_reason = defaultdict(str)
        expiry_map = {}
        for r in ph_rows:
            app_date = r[8]
            amt = 0.0
            try:
                amt = float(r[11]) if r[11] else 0.0
            except Exception:
                amt = 0.0
            per_date[app_date] += amt
            per_reason[app_date] = r[9] or per_reason.get(app_date, "")
            expiry_map[app_date] = r[12] if len(r) > 12 and r[12] else "N/A"

        active_lines = []
        for adt, amt in sorted(per_date.items()):
            if amt > 0.0001:
                exp = expiry_map.get(adt, "N/A")
                reason = per_reason.get(adt, "")
                active_lines.append(f"• {adt}: {fmt_days(amt)} (exp {exp}) – {reason}")

        total_ph = sum(v for v in per_date.values() if v > 0)
        if not active_lines:
            ph_block = "🔎 PH Off Entries: None"
        else:
            ph_block = "🔎 PH Off Entries:\n" + "\n".join(active_lines)

        await update.message.reply_text(
            f"📊 Current Off Balance: {fmt_days(bal)} day(s).\n"
            f"🏖 PH Off Total: {fmt_days(total_ph)} day(s)\n"
            f"{ph_block}"
        )
    except Exception:
        logger.exception("❌ Failed to compute summary")
        await update.message.reply_text("❌ Could not compute summary.")

# admin-only mass clock (normal + PH) — preview + confirm
async def massclockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only admins
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        admin_ids = {a.user.id for a in admins if not a.user.is_bot}
        if update.effective_user.id not in admin_ids:
            await update.message.reply_text("🚫 Admins only.")
            return
    except Exception:
        await update.message.reply_text("🚫 Admins only.")
        return

    token = short_token()
    user_state[update.effective_user.id] = {
        "flow": "mass_normal", "action": "clockoff", "stage": "awaiting_days",
        "token": token, "group_id": update.effective_chat.id
    }
    await update.message.reply_text(
        "👥 Mass Clock (Normal)\nHow many days per user? (0.5 to 3, in 0.5 steps)",
        reply_markup=build_cancel_markup(token)
    )

async def massclockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only admins
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        admin_ids = {a.user.id for a in admins if not a.user.is_bot}
        if update.effective_user.id not in admin_ids:
            await update.message.reply_text("🚫 Admins only.")
            return
    except Exception:
        await update.message.reply_text("🚫 Admins only.")
        return

    token = short_token()
    user_state[update.effective_user.id] = {
        "flow": "mass_ph", "action": "clockphoff", "stage": "awaiting_days",
        "token": token, "group_id": update.effective_chat.id
    }
    await update.message.reply_text(
        "👥 Mass Clock (PH)\nHow many PH days per user? (0.5 to 3, in 0.5 steps)",
        reply_markup=build_cancel_markup(token)
    )

# Onboarding
async def newuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = short_token()
    user_state[update.effective_user.id] = {
        "flow": "newuser", "stage": "awaiting_import_days",
        "token": token, "group_id": update.effective_chat.id,
        "import_days": 0.0, "ph_entries": []
    }
    await update.message.reply_text(
        "🆕 *Onboarding: Import Old Records*\n\n"
        "1) How many *normal OIL* days to import?\n"
        "   (Enter a number, e.g. 7.5 or 0 if none)",
        parse_mode="Markdown",
        reply_markup=build_cancel_markup(token)
    )

# ---------- Message handler (stages) ----------
@logger.catch if hasattr(logger, "catch") else (lambda f: f)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    st = user_state.get(uid)
    if not st:
        return

    logger.info(f"handle_message: uid={uid} stage={st.get('stage')} flow={st.get('flow')} action={st.get('action')} text={text}")

    # universal quit words (still keep inline Cancel as primary)
    if text.lower() in {"-quit", "cancel"}:
        user_state.pop(uid, None)
        await update.message.reply_text("🧹 Cancelled.")
        return

    def ask_date():
        # calendar + manual option
        token = st["token"]
        ref = date.today()
        return update.message.reply_text(
            ("📅 *Select Application Date*:\n"
             "• Tap a date below, or\n"
             "• Tap *Manual entry*, then type YYYY-MM-DD."),
            parse_mode="Markdown",
            reply_markup=build_calendar_markup(token, ref)
        )

    # ---- Normal / PH / Mass flows: get days ----
    if st["stage"] == "awaiting_days":
        try:
            days = float(text)
            if days <= 0:
                raise ValueError()
            if not is_valid_half_step(days) or days < 0.5 or days > 3:
                raise ValueError()
        except Exception:
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 and 3 in 0.5 steps.")
            return
        st["days"] = days
        # next: date
        st["stage"] = "awaiting_date"
        await ask_date()
        return

    # ---- Date typed manually (fallback when waiting for date) ----
    if st["stage"] == "awaiting_date":
        dt = parse_date_yyyy_mm_dd(text) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None
        if not dt:
            await update.message.reply_text("❌ Please enter date as YYYY-MM-DD, or pick from the calendar.")
            return
        st["app_date"] = dt.isoformat()
        st["stage"] = "awaiting_reason"
        await update.message.reply_text(
            f"📝 Enter reason (max {MAX_REMARKS_LEN} chars):",
            reply_markup=build_cancel_markup(st["token"])
        )
        return

    # ---- Reason ----
    if st["stage"] == "awaiting_reason":
        reason = text
        if len(reason) > MAX_REMARKS_LEN:
            reason = reason[:MAX_REMARKS_LEN]
            await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
        st["reason"] = reason

        # If this is mass flow, we prepare a preview instead of direct admin DM
        if st["flow"] in {"mass_normal", "mass_ph"}:
            # Build preview list from SHEET (unique users with at least one row), ignoring header-ish rows
            _, all_rows = sheet_get_user_rows(uid)  # just to fetch all rows
            # include unique user ids where row[1] is numeric and not header
            from collections import OrderedDict
            uniq = OrderedDict()
            for r in all_rows[1:]:
                if len(r) < 3: 
                    continue
                rid = r[1]
                nm = r[2]
                if not rid.isdigit():
                    continue
                if rid not in uniq:
                    uniq[rid] = nm or rid
            names = [f"- {nm} ({rid})" for rid, nm in uniq.items()]
            if not names:
                await update.message.reply_text("⚠️ No known users to mass clock (sheet is empty).")
                user_state.pop(uid, None)
                return

            st["mass_targets"] = list(uniq.items())
            pretty = "\n".join(names[:100])  # avoid oversize message
            await update.message.reply_text(
                f"👥 *Mass Preview* ({len(names)} users)\n\n{pretty}\n\n"
                "Proceed?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm", callback_data=f"massgo|{st['token']}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{st['token']}")
                ]])
            )
            return

        # Otherwise: single-user approval
        await submit_for_approval(update, context, st)
        user_state.pop(uid, None)
        return

    # ---- Newuser flow ----
    if st["flow"] == "newuser":
        if st["stage"] == "awaiting_import_days":
            try:
                days = float(text)
                if days < 0:
                    raise ValueError()
            except Exception:
                await update.message.reply_text("❌ Please enter a number (e.g., 7.5) or 0.")
                return
            st["import_days"] = days
            st["stage"] = "newuser_ph_count"
            await update.message.reply_text(
                "PH import: how many *separate PH entries* do you have? (Enter 0 if none)\n"
                "We’ll capture *Application Date* and *Reason* for each.",
                parse_mode="Markdown",
                reply_markup=build_cancel_markup(st["token"])
            )
            return
        if st["stage"] == "newuser_ph_count":
            try:
                cnt = int(text)
                if cnt < 0 or cnt > 20:
                    raise ValueError()
            except Exception:
                await update.message.reply_text("❌ Enter a whole number from 0 to 20.")
                return
            st["ph_needed"] = cnt
            st["ph_index"] = 0
            st["ph_entries"] = []
            if cnt == 0:
                # Submit to admins directly
                await submit_newuser_for_approval(update, context, st)
                user_state.pop(uid, None)
                return
            # For first PH: ask date via calendar
            st["stage"] = "newuser_ph_date"
            await update.message.reply_text(
                f"PH Entry {st['ph_index']+1}/{st['ph_needed']} — *Select Application Date*",
                parse_mode="Markdown",
                reply_markup=build_calendar_markup(st["token"], date.today())
            )
            return
        if st["stage"] == "newuser_ph_date":
            # Manual typed?
            dt = parse_date_yyyy_mm_dd(text) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None
            if not dt:
                await update.message.reply_text("❌ Type date as YYYY-MM-DD, or pick from the calendar.")
                return
            st["ph_current_date"] = dt.isoformat()
            st["stage"] = "newuser_ph_reason"
            await update.message.reply_text(
                f"PH Entry {st['ph_index']+1}/{st['ph_needed']} — Enter *Reason* (max {MAX_REMARKS_LEN} chars):",
                parse_mode="Markdown",
                reply_markup=build_cancel_markup(st["token"])
            )
            return
        if st["stage"] == "newuser_ph_reason":
            reason = text[:MAX_REMARKS_LEN]
            st["ph_entries"].append({"app_date": st["ph_current_date"], "reason": reason})
            st["ph_index"] += 1
            if st["ph_index"] >= st["ph_needed"]:
                # Submit to admins
                await submit_newuser_for_approval(update, context, st)
                user_state.pop(uid, None)
                return
            # next date
            st["stage"] = "newuser_ph_date"
            await update.message.reply_text(
                f"PH Entry {st['ph_index']+1}/{st['ph_needed']} — *Select Application Date*",
                parse_mode="Markdown",
                reply_markup=build_calendar_markup(st["token"], date.today())
            )
            return

# ---------- Approval submission ----------
async def submit_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    user = update.effective_user
    uid = user.id
    group_id = st["group_id"]
    days = float(st["days"])
    app_date = st["app_date"]
    reason = st["reason"]
    action = st["action"]

    # No negative or zero days (extra guard)
    if days <= 0:
        await update.message.reply_text("🚫 Days must be greater than 0.")
        return

    # Compute balances for preview
    current_off = get_current_off(uid) if action in {"clockoff", "claimoff"} else get_current_off(uid)
    if action == "clockoff":
        new_off = current_off + days
    elif action == "claimoff":
        new_off = current_off - days
    else:
        # PH flows don't change normal balance by themselves
        new_off = current_off

    token = short_token()
    pending_tokens[token] = {
        "kind": "single",
        "user_id": uid,
        "user_full_name": user.full_name,
        "group_id": group_id,
        "action": action,
        "days": days,
        "app_date": app_date,
        "reason": reason
    }

    # Build message
    lines = [
        f"🆕 *{human_action(action)} Request*",
        f"👤 User: {user.full_name} ({uid})",
        f"📅 Days: {fmt_days(days)}",
        f"🗓 Application Date: {app_date}",
        f"📝 Reason: {reason}",
    ]
    if action in {"clockoff", "claimoff"}:
        lines += [
            f"📊 Current Off: {fmt_days(current_off)} day(s)",
            f"📈 New Balance: {fmt_days(new_off)} day(s)"
        ]
    text = "\n".join(lines)

    rm = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{token}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{token}")
    ]])

    sent = await dm_admins(context, group_id, text, rm)
    admin_message_refs[token] = sent
    await update.message.reply_text("📩 Submitted to admins for approval.")

async def submit_newuser_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    user = update.effective_user
    uid = user.id
    group_id = st["group_id"]

    token = short_token()
    pending_tokens[token] = {
        "kind": "newuser",
        "user_id": uid,
        "user_full_name": user.full_name,
        "group_id": group_id,
        "import_days": float(st.get("import_days", 0.0)),
        "ph_entries": list(st.get("ph_entries", []))
    }

    ph_lines = []
    for i, entry in enumerate(pending_tokens[token]["ph_entries"], 1):
        ph_lines.append(f"• {entry['app_date']} – {entry['reason']}")

    text = (
        "🆕 *Onboarding Request*\n"
        f"👤 User: {user.full_name} ({uid})\n"
        f"🧮 Normal OIL to import: {fmt_days(pending_tokens[token]['import_days'])}\n"
        f"🏖 PH Entries ({len(ph_lines)}):\n" + ("\n".join(ph_lines) if ph_lines else "• None")
    )

    rm = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{token}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{token}")
    ]])

    sent = await dm_admins(context, group_id, text, rm)
    admin_message_refs[token] = sent
    await update.message.reply_text("📩 Submitted to admins for approval. You’ll be notified in the group.")

# ---------- Callback handler ----------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    try:
        # Cancel
        if data.startswith("cancel|"):
            _, token = data.split("|", 1)
            # Clear any states for this user that match token
            for uid, st in list(user_state.items()):
                if st.get("token") == token:
                    user_state.pop(uid, None)
                    break
            await query.edit_message_text("🧹 Cancelled.")
            return

        # Calendar day chosen
        if data.startswith("cal|"):
            _, token, sel = data.split("|", 2)
            # Find whose state has this token
            for uid, st in user_state.items():
                if st.get("token") == token and st.get("stage") in {"awaiting_date", "newuser_ph_date"}:
                    st["app_date"] = sel
                    if st["flow"] == "newuser" and st["stage"] == "newuser_ph_date":
                        st["ph_current_date"] = sel
                        st["stage"] = "newuser_ph_reason"
                        await query.edit_message_text(f"PH Entry {st['ph_index']+1}/{st['ph_needed']} — Enter *Reason* (max {MAX_REMARKS_LEN} chars):", parse_mode="Markdown")
                    else:
                        st["stage"] = "awaiting_reason"
                        await query.edit_message_text("📝 Enter reason (max 80 chars):")
                    return
            await query.edit_message_text("⚠️ This selection is no longer active.")
            return

        # Calendar navigation
        if data.startswith("calnav|"):
            _, token, month_iso = data.split("|", 2)
            d = parse_date_yyyy_mm_dd(month_iso + "-01") if len(month_iso) == 10 else None
            if not d:
                d = date.today().replace(day=1)
            await query.edit_message_reply_markup(reply_markup=build_calendar_markup(token, d))
            return

        # Manual entry choice
        if data.startswith("manual|"):
            _, token = data.split("|", 1)
            await query.edit_message_text("✍️ Type the date as YYYY-MM-DD.")
            return

        # Mass confirm
        if data.startswith("massgo|"):
            _, token = data.split("|", 1)
            # Find the requesting admin’s state
            for uid, st in list(user_state.items()):
                if st.get("token") == token and st.get("flow") in {"mass_normal", "mass_ph"} and st.get("stage") == "awaiting_reason":
                    # Create a single approval token for this mass op
                    mtoken = short_token()
                    pending_tokens[mtoken] = {
                        "kind": "mass",
                        "who": st["mass_targets"],   # list of (uid_str, name)
                        "group_id": st["group_id"],
                        "action": st["action"],
                        "days": float(st["days"]),
                        "app_date": st["app_date"],
                        "reason": st["reason"],
                        "requester_name": user.full_name
                    }
                    rm = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve|{mtoken}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{mtoken}")
                    ]])
                    preview = f"👥 Mass {human_action(st['action'])}\nDays: {fmt_days(st['days'])}\nDate: {st['app_date']}\nReason: {st['reason']}\nTargets: {len(st['mass_targets'])}"
                    sent = await dm_admins(context, st["group_id"], preview, rm)
                    admin_message_refs[mtoken] = sent
                    user_state.pop(uid, None)
                    await query.edit_message_text("📩 Mass request submitted to admins.")
                    return
            await query.edit_message_text("⚠️ Mass request no longer active.")
            return

        # Approve / Deny
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", 1)
            payload = pending_tokens.get(token)
            if not payload:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            # Clean up admin messages (others)
            if token in admin_message_refs:
                for admin_id, msg_id in admin_message_refs[token]:
                    if admin_id != user.id:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=admin_id, message_id=msg_id,
                                text=f"⚠️ Request handled by {user.full_name}."
                            )
                        except Exception:
                            pass

            if action_type == "deny":
                # Notify group / requester
                kind = payload["kind"]
                if kind == "single":
                    gid = payload["group_id"]
                    uname = payload.get("user_full_name") or str(payload["user_id"])
                    await query.edit_message_text("❌ Request denied.")
                    await context.bot.send_message(gid, text=f"❌ {uname}'s request was denied by {user.full_name}.")
                elif kind == "mass":
                    gid = payload["group_id"]
                    await query.edit_message_text("❌ Mass request denied.")
                    await context.bot.send_message(gid, text=f"❌ Mass request denied by {user.full_name}.")
                else:  # newuser
                    gid = payload["group_id"]
                    uname = payload.get("user_full_name") or str(payload["user_id"])
                    await query.edit_message_text("❌ Onboarding denied.")
                    await context.bot.send_message(gid, text=f"❌ Onboarding for {uname} was denied by {user.full_name}.")
                pending_tokens.pop(token, None)
                admin_message_refs.pop(token, None)
                return

            # APPROVAL path
            kind = payload["kind"]
            now = datetime.now()
            ts = now.strftime("%Y-%m-%d %H:%M:%S")

            if kind == "single":
                uid = str(payload["user_id"])
                name = payload.get("user_full_name") or uid
                gid = payload["group_id"]
                action = payload["action"]
                days = float(payload["days"])
                app_date = payload["app_date"]
                reason = payload["reason"]

                current = get_current_off(uid)
                final = current
                add_subtract = ""
                holiday_off = "No"
                ph_total = ""
                expiry = "N/A"

                if action == "clockoff":
                    final = current + days
                    add_subtract = f"+{fmt_days(days)}"
                elif action == "claimoff":
                    final = current - days
                    add_subtract = f"-{fmt_days(days)}"
                elif action == "clockphoff":
                    # PH doesn’t change normal balance; track in PH Off Total, mark as Holiday Off
                    holiday_off = "Yes"
                    ph_total = f"+{fmt_days(days)}"
                    # expiry = app_date + 365
                    d = parse_date_yyyy_mm_dd(app_date)
                    expiry = (d + timedelta(days=365)).isoformat() if d else "N/A"
                    add_subtract = "±0"
                elif action == "claimphoff":
                    holiday_off = "Yes"
                    ph_total = f"-{fmt_days(days)}"
                    add_subtract = "±0"
                else:
                    add_subtract = "±0"

                append_sheet_row(
                    ts, uid, name, human_action(action),
                    current, add_subtract, final,
                    user.full_name, app_date, reason,
                    holiday_off, ph_total, expiry
                )

                # keep info on the PM after handling
                await query.edit_message_text(
                    f"✅ Approved\n"
                    f"{human_action(action)} — {name}\n"
                    f"Days: {fmt_days(days)} | Date: {app_date}\n"
                    f"Reason: {reason}\n"
                    f"Final Off: {fmt_days(final)}"
                )

                # group notify with name, not ID
                await context.bot.send_message(
                    gid,
                    text=(f"✅ {name}'s {human_action(action)} approved by {user.full_name}.\n"
                          f"📅 Days: {fmt_days(days)}\n"
                          f"🗓 Application Date: {app_date}\n"
                          f"📝 Reason: {reason}\n"
                          f"📊 Final: {fmt_days(final)} day(s)")
                )

            elif kind == "mass":
                gid = payload["group_id"]
                days = float(payload["days"])
                app_date = payload["app_date"]
                reason = payload["reason"]
                action = payload["action"]
                targets = payload["who"]

                # Apply per target
                for uid, name in targets:
                    current = get_current_off(uid)
                    final = current
                    add_subtract = ""
                    holiday_off = "No"
                    ph_total = ""
                    expiry = "N/A"

                    if action == "clockoff":
                        final = current + days
                        add_subtract = f"+{fmt_days(days)}"
                    elif action == "clockphoff":
                        holiday_off = "Yes"
                        ph_total = f"+{fmt_days(days)}"
                        d = parse_date_yyyy_mm_dd(app_date)
                        expiry = (d + timedelta(days=365)).isoformat() if d else "N/A"
                        add_subtract = "±0"

                    append_sheet_row(
                        ts, str(uid), name or str(uid), human_action(action),
                        current, add_subtract, final, user.full_name,
                        app_date, reason, holiday_off, ph_total, expiry
                    )

                await query.edit_message_text(
                    f"✅ Mass {human_action(action)} approved.\n"
                    f"Users: {len(targets)} | Days: {fmt_days(days)} | Date: {app_date}\nReason: {reason}"
                )
                await context.bot.send_message(
                    gid,
                    text=(f"✅ Mass {human_action(action)} approved by {user.full_name}.\n"
                          f"👥 Users: {len(targets)}\n"
                          f"📅 Days each: {fmt_days(days)}\n"
                          f"🗓 Date: {app_date}\n"
                          f"📝 Reason: {reason}")
                )

            else:  # newuser
                uid = str(payload["user_id"])
                name = payload.get("user_full_name") or uid
                gid = payload["group_id"]
                import_days = float(payload.get("import_days", 0.0))
                ph_entries = payload.get("ph_entries", [])

                # normal import row (if > 0)
                if import_days > 0:
                    current = get_current_off(uid)
                    final = current + import_days
                    append_sheet_row(
                        ts, uid, name, "Clock Off",
                        current, f"+{fmt_days(import_days)}", final,
                        user.full_name, date.today().isoformat(), "Transfer from old record",
                        "No", "", "N/A"
                    )

                # PH entries: each +1.0 (or whatever logic you prefer), expiry +365 from app date
                for entry in ph_entries:
                    app_date = entry["app_date"]
                    reason = entry["reason"]
                    d = parse_date_yyyy_mm_dd(app_date)
                    expiry = (d + timedelta(days=365)).isoformat() if d else "N/A"
                    # PH Off Total +1, doesn't affect normal balance
                    current = get_current_off(uid)  # after import
                    final = current
                    append_sheet_row(
                        ts, uid, name, "Clock PH Off",
                        current, "±0", final,
                        user.full_name, app_date, reason,
                        "Yes", "+1", expiry
                    )

                await query.edit_message_text("✅ Onboarding approved and recorded.")
                await context.bot.send_message(gid, text=f"✅ Onboarding for {name} approved by {user.full_name}.")

            # cleanup
            pending_tokens.pop(token, None)
            admin_message_refs.pop(token, None)
            return

        # No-op buttons
        if data.startswith("noop|"):
            return

    except Exception:
        logger.exception("❌ Failed to process callback")
        try:
            await query.edit_message_text("❌ Something went wrong.")
        except Exception:
            pass

# ---------- Webhook ----------
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

# ---------- Init ----------
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
    telegram_app.add_handler(CommandHandler("allsummary", summary))  # simple reuse; can expand
    telegram_app.add_handler(CommandHandler("history", history))

    telegram_app.add_handler(CommandHandler("clockoff", clockoff))
    telegram_app.add_handler(CommandHandler("claimoff", claimoff))
    telegram_app.add_handler(CommandHandler("clockphoff", clockphoff))
    telegram_app.add_handler(CommandHandler("claimphoff", claimphoff))

    telegram_app.add_handler(CommandHandler("massclockoff", massclockoff))
    telegram_app.add_handler(CommandHandler("massclockphoff", massclockphoff))

    telegram_app.add_handler(CommandHandler("newuser", newuser))

    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook set.")

# ---------- Run ----------
if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    logger.info("🟢 Starting Flask...")
    app.run(host="0.0.0.0", port=10000)