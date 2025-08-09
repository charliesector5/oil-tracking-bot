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
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Chat
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

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------- Env ----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

# ---------------- Flask ----------------
app = Flask(__name__)

@app.route('/')
def index():
    return "✅ Oil Tracking Bot is up."

@app.route('/health')
def health():
    return "✅ Health check passed."

# ---------------- Globals ----------------
telegram_app = None
worksheet = None
loop = asyncio.new_event_loop()
executor = ThreadPoolExecutor()

# conversational state per user while filling forms
user_state = {}

# short-token stores to keep callback_data tiny
pending_requests = {}     # token -> dict for single approvals & onboarding & mass
calendar_sessions = {}    # token -> {user_id, chat_id, flow, ...}

# track admin PMs to fanout/clean up
admin_message_refs = {}   # token -> [(admin_id, message_id), ...]

MAX_REMARKS_LEN = 80

# ---------------- Small helpers ----------------
def cancel_row(show: bool):
    return [InlineKeyboardButton("❌ Cancel", callback_data="cancel")] if show else []

def is_admin_id(admins, uid: int) -> bool:
    for a in admins:
        if a.user and a.user.id == uid:
            return True
    return False

def build_name(u):
    if getattr(u, "full_name", None):
        return u.full_name
    if getattr(u, "username", None):
        return f"@{u.username}"
    return str(u.id)

def safe_half_step_days_ok(s: str, allow_up_to: float = 3.0) -> bool:
    try:
        val = float(s)
    except:
        return False
    # prevent negative/misuse; allow 0.5 steps up to 'allow_up_to'
    return (val > 0) and (val <= allow_up_to) and ((val * 10) % 5 == 0)

def clamp_remarks(s: str) -> str:
    return s[:MAX_REMARKS_LEN] if len(s) > MAX_REMARKS_LEN else s

def fmt_act(action_key: str) -> str:
    # human label
    if action_key in ("clockoff", "clockphoff"):
        return "Clock Off" if action_key == "clockoff" else "Clock PH Off"
    if action_key in ("claimoff", "claimphoff"):
        return "Claim Off" if action_key == "claimoff" else "Claim PH Off"
    if action_key == "massclockoff":
        return "Mass Clock Off"
    if action_key == "massclockphoff":
        return "Mass Clock PH Off"
    if action_key == "newuser_import":
        return "Onboarding Import"
    return action_key

def is_private_chat(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == Chat.PRIVATE

def exp_from_appdate(app_date: str) -> str:
    try:
        d = datetime.strptime(app_date, "%Y-%m-%d").date()
        e = d + timedelta(days=365)
        return e.strftime("%Y-%m-%d")
    except:
        return "N/A"

# Google Sheet column indices per latest spec:
COL_TIMESTAMP = 0
COL_TG_ID = 1
COL_NAME = 2
COL_ACTION = 3
COL_CURR = 4
COL_ADDSUB = 5
COL_FINAL = 6
COL_APPROVED_BY = 7
COL_APPDATE = 8
COL_REMARKS = 9
COL_HOLIDAY = 10
COL_PH_TOTAL = 11
COL_EXPIRY = 12

# ---------------- Month Calendar UI ----------------
async def send_month_calendar(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    token: str,
    year: int,
    month: int,
    allow_manual: bool = True,
    show_cancel: bool = True,
    prompt: str = "📅 Select Application Date:",
):
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)

    header = f"{calendar.month_name[month]} {year}"
    rows = []

    # header row with navigation
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    rows.append([
        InlineKeyboardButton("«", callback_data=f"cal_nav|{token}|{prev_year}|{prev_month}"),
        InlineKeyboardButton(header, callback_data="noop"),
        InlineKeyboardButton("»", callback_data=f"cal_nav|{token}|{next_year}|{next_month}"),
    ])

    # weekday labels
    rows.append([InlineKeyboardButton(x, callback_data="noop") for x in ["Mo","Tu","We","Th","Fr","Sa","Su"]])

    # dates
    for week in month_days:
        wk = []
        for day in week:
            if day == 0:
                wk.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                wk.append(InlineKeyboardButton(
                    str(day),
                    callback_data=f"cal_pick|{token}|{year}|{month}|{day}"
                ))
        rows.append(wk)

    last = []
    if allow_manual:
        last.append(InlineKeyboardButton("⌨️ Enter manually", callback_data=f"cal_manual|{token}"))
    if show_cancel:
        last.extend(cancel_row(True))
    if last:
        rows.append(last)

    await context.bot.send_message(
        chat_id=chat_id,
        text=prompt,
        reply_markup=InlineKeyboardMarkup(rows)
    )

# ---------------- Webhook ----------------
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

# ---------------- Commands ----------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠️ Oil Tracking Bot Help\n\n"
        "/clockoff – Request to clock normal OIL\n"
        "/claimoff – Request to claim normal OIL\n"
        "/clockphoff – Clock Public Holiday OIL (PH)\n"
        "/claimphoff – Claim Public Holiday OIL (PH)\n"
        "/massclockoff – Admin: Mass clock normal OIL for all\n"
        "/massclockphoff – Admin: Mass clock PH OIL for all (preview)\n"
        "/summary – Your current balance & PH details\n"
        "/history – Your past 5 logs\n"
        "/newuser – Import old records (onboarding)\n"
        "/startadmin – Start admin session (PM only)\n"
        "/help – Show this help message\n\n"
        "Tip: You can always tap ❌ Cancel or type -quit to abort."
    )

async def startadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private_chat(update):
        await update.message.reply_text("ℹ️ Please PM me and run /startadmin there.")
        return
    await update.message.reply_text("✅ Admin session started. I'll DM you approvals and previews here.")

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > COL_TG_ID and row[COL_TG_ID] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[COL_FINAL]  # Final Off
            # PH Off Total if present
            ph_total = last_row[COL_PH_TOTAL] if len(last_row) > COL_PH_TOTAL and last_row[COL_PH_TOTAL] else "0.0"
            # Active PH entries: Action == "Clock PH Off" and not expired
            now = datetime.now().date()
            active = []
            for r in user_rows:
                if len(r) >= (COL_EXPIRY + 1):
                    action = r[COL_ACTION]
                    holiday_flag = (r[COL_HOLIDAY] or "").strip().lower()
                    if action == "Clock PH Off" and holiday_flag == "yes":
                        expiry = (r[COL_EXPIRY] or "").strip()
                        try:
                            if datetime.strptime(expiry, "%Y-%m-%d").date() >= now:
                                app_date = r[COL_APPDATE]
                                remarks = r[COL_REMARKS] if len(r) > COL_REMARKS else ""
                                active.append(f"• {app_date} → exp {expiry} — {remarks}")
                        except:
                            # if expiry is malformed, consider it active
                            app_date = r[COL_APPDATE]
                            remarks = r[COL_REMARKS] if len(r) > COL_REMARKS else ""
                            active.append(f"• {app_date} — {remarks}")
            msg = (
                f"📊 Current Off Balance: {balance} day(s).\n"
                f"🏖 PH Off Total: {ph_total} day(s)\n"
            )
            if active:
                msg += "🔎 Active PH Off Entries:\n" + "\n".join(active)
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("📊 No records found.")
    except Exception:
        logger.exception("❌ Failed to fetch summary")
        await update.message.reply_text("❌ Could not retrieve your summary.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > COL_TG_ID and row[COL_TG_ID] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            lines = []
            for row in last_5:
                ts = row[COL_TIMESTAMP] if len(row) > COL_TIMESTAMP else ""
                action = row[COL_ACTION] if len(row) > COL_ACTION else ""
                ad = row[COL_ADDSUB] if len(row) > COL_ADDSUB else ""
                fin = row[COL_FINAL] if len(row) > COL_FINAL else ""
                app_date = row[COL_APPDATE] if len(row) > COL_APPDATE else ""
                remarks = row[COL_REMARKS] if len(row) > COL_REMARKS else ""
                lines.append(f"{ts} | {action} | {ad} → {fin} | {app_date} | {remarks}")
            await update.message.reply_text("📜 Your last 5 OIL logs:\n\n" + "\n".join(lines))
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

# --------------- Normal OIL (clock/claim) ---------------
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"action": "clockoff", "is_ph": False, "stage": "awaiting_days", "group_id": update.message.chat_id}
    show_cancel = (update.effective_chat.type != "private")
    kb = InlineKeyboardMarkup([cancel_row(show_cancel)]) if show_cancel else None
    await update.message.reply_text("🕒 How many days do you want to clock off? (0.5 to 3, in 0.5 steps)", reply_markup=kb)

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"action": "claimoff", "is_ph": False, "stage": "awaiting_days", "group_id": update.message.chat_id}
    show_cancel = (update.effective_chat.type != "private")
    kb = InlineKeyboardMarkup([cancel_row(show_cancel)]) if show_cancel else None
    await update.message.reply_text("📥 How many days do you want to claim off? (0.5 to 3, in 0.5 steps)", reply_markup=kb)

# --------------- PH OIL (clock/claim) ---------------
async def clockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"action": "clockphoff", "is_ph": True, "stage": "awaiting_days", "group_id": update.message.chat_id}
    show_cancel = (update.effective_chat.type != "private")
    kb = InlineKeyboardMarkup([cancel_row(show_cancel)]) if show_cancel else None
    await update.message.reply_text("🏖 How many PH OIL days to clock? (0.5 to 3, in 0.5 steps)", reply_markup=kb)

async def claimphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"action": "claimphoff", "is_ph": True, "stage": "awaiting_days", "group_id": update.message.chat_id}
    show_cancel = (update.effective_chat.type != "private")
    kb = InlineKeyboardMarkup([cancel_row(show_cancel)]) if show_cancel else None
    await update.message.reply_text("📥 How many PH OIL days to claim? (0.5 to 3, in 0.5 steps)", reply_markup=kb)

# --------------- Mass clock (normal + PH) ---------------
async def massclockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    if not is_admin_id(admins, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    st = {"action": "massclockoff", "is_ph": False, "stage": "awaiting_days", "group_id": update.message.chat_id}
    user_state[update.effective_user.id] = st
    await update.message.reply_text("🧑‍🤝‍🧑 Mass clock normal off — how many days? (0.5 to 3)")

async def massclockphoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    if not is_admin_id(admins, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    st = {"action": "massclockphoff", "is_ph": True, "stage": "awaiting_days", "group_id": update.message.chat_id}
    user_state[update.effective_user.id] = st
    await update.message.reply_text("🧑‍🤝‍🧑 Mass clock PH off — how many days? (0.5 to 3)")

# --------------- New User Onboarding ---------------
async def newuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {
        "action": "newuser",
        "stage": "newuser_normal_days",
        "group_id": update.message.chat_id,
        "newuser_ph_items": []
    }
    show_cancel = (update.effective_chat.type != "private")
    kb = InlineKeyboardMarkup([cancel_row(show_cancel)]) if show_cancel else None
    await update.message.reply_text(
        "🆕 Onboarding: Import Old Records\n\n"
        "1) How many normal OIL days to import? (Enter a number, e.g. 7.5 or 0 if none)",
        reply_markup=kb
    )

# ---------------- Message handler (all stages) ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Global quit by text
    if text == "-quit":
        user_state.pop(uid, None)
        await update.message.reply_text("🧹 Cancelled.")
        return

    if uid not in user_state:
        return

    st = user_state[uid]
    stage = st.get("stage")

    # ---------- Normal / PH single-request & mass flows ----------
    if stage == "awaiting_days":
        # For mass and single we want 0.5..3 range
        if not safe_half_step_days_ok(text, 3.0):
            await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 and 3 in 0.5 steps.")
            return
        st["days"] = float(text)
        # move to calendar for application date
        st["stage"] = "awaiting_app_date"
        tok = uuid.uuid4().hex[:8]
        st["calendar_token"] = tok
        flow = "mass" if st["action"].startswith("mass") else "single"
        calendar_sessions[tok] = {
            "user_id": uid,
            "chat_id": st["group_id"],
            "flow": flow,  # single request normal/PH or mass
        }
        now = datetime.now()
        prompt = "📅 Select Application Date (YYYY-MM-DD):" if flow == "single" else "📅 Select Application Date for mass action:"
        await send_month_calendar(
            context, st["group_id"], tok, now.year, now.month,
            allow_manual=True,
            show_cancel=(update.effective_chat.type != "private"),
            prompt=prompt
        )
        return

    if stage == "awaiting_reason":
        reason = clamp_remarks(text)
        st["reason"] = reason
        await update.message.reply_text("📩 Your request has been submitted for approval.")
        await send_approval_request(update, context, st)
        user_state.pop(uid, None)
        return

    # ---------- /newuser flow ----------
    if stage == "newuser_normal_days":
        try:
            days = float(text)
            if days < 0:
                raise ValueError()
        except:
            await update.message.reply_text("❌ Please enter a valid number (>= 0).")
            return
        st["newuser_normal_days"] = days
        if days > 0:
            st["stage"] = "newuser_normal_app_date"
            tok = uuid.uuid4().hex[:8]
            st["calendar_token"] = tok
            calendar_sessions[tok] = {"user_id": uid, "chat_id": st["group_id"], "flow": "newuser_normal"}
            now = datetime.now()
            await send_month_calendar(context, st["group_id"], tok, now.year, now.month, True,
                                      show_cancel=(update.effective_chat.type != "private"),
                                      prompt="📅 Select Application Date for normal OIL import:")
        else:
            st["stage"] = "newuser_ph_intro"
            await update.message.reply_text(
                "Now, import Public Holiday (PH) OIL.\n"
                "You can add multiple PH entries (1 day each). Tap ➕ Add PH entry or tap ✅ Done when finished.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add PH entry", callback_data="newuser_ph_add"),
                    InlineKeyboardButton("✅ Done PH import", callback_data="newuser_ph_done")
                ]])
            )
        return

    if stage == "newuser_after_normal_date":
        st["stage"] = "newuser_ph_intro"
        await update.message.reply_text(
            "Now, import Public Holiday (PH) OIL.\n"
            "You can add multiple PH entries (1 day each). Tap ➕ Add PH entry or tap ✅ Done when finished.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add PH entry", callback_data="newuser_ph_add"),
                InlineKeyboardButton("✅ Done PH import", callback_data="newuser_ph_done")
            ]])
        )
        return

    if stage == "newuser_ph_reason":
        reason = clamp_remarks(text)
        cur = st.get("newuser_ph_current", {}) or {}
        cur["reason"] = reason
        st["newuser_ph_current"] = None
        items = st.setdefault("newuser_ph_items", [])
        items.append(cur)
        await update.message.reply_text(
            "PH entry saved.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add another PH", callback_data="newuser_ph_add"),
                InlineKeyboardButton("✅ Done PH import", callback_data="newuser_ph_done")
            ]])
        )
        return

    # ---------- Manual date entry (from calendar) ----------
    if stage == "manual_date_entry":
        # handled by handle_manual_date_entry
        return

    # if we get here, ignore
    return

# ---------------- Admin approval (single user) ----------------
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    user = update.effective_user
    group_id = st["group_id"]
    action = st["action"]  # clockoff/claimoff/clockphoff/claimphoff
    is_ph = st.get("is_ph", False)
    app_date = st.get("app_date", datetime.now().strftime("%Y-%m-%d"))
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > COL_TG_ID and row[COL_TG_ID] == str(user.id)]
        current_off = float(user_rows[-1][COL_FINAL]) if user_rows else 0.0
        delta = float(st["days"])
        new_off = current_off + delta if action in ("clockoff", "clockphoff") else current_off - delta

        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": action,
            "days": st["days"],
            "reason": st["reason"],
            "app_date": app_date,
            "is_ph": is_ph,
            "group_id": group_id,
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
                        f"🆕 *{fmt_act(action)} Request*\n\n"
                        f"👤 User: {user.full_name}\n"
                        f"📅 Days: {st['days']}\n"
                        f"📆 Application Date: {app_date}\n"
                        f"📝 Reason: {st['reason']}\n\n"
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

# ---------------- Roster helpers for MASS ----------------
def sheet_unique_users(all_data):
    """
    Build a unique roster from the sheet: {user_id: name}
    Skips header rows and any rows without numeric Telegram IDs.
    """
    roster = {}
    for row in all_data:
        if len(row) <= COL_TG_ID:
            continue
        tg_id = row[COL_TG_ID].strip()
        name = row[COL_NAME].strip() if len(row) > COL_NAME else ""
        if not tg_id:
            continue
        # only numeric positive ids
        if not tg_id.isdigit():
            continue
        roster[tg_id] = name or tg_id
    return roster

# ---------------- MASS preview + apply ----------------
async def mass_prepare_preview_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    """
    After admin chooses date, build a preview list and DM the admin with Confirm/Cancel.
    """
    admin_id = update.effective_user.id
    group_id = st["group_id"]
    action = st["action"]  # massclockoff or massclockphoff
    is_ph = st.get("is_ph", False)
    days = float(st["days"])
    app_date = st.get("app_date")

    try:
        all_data = worksheet.get_all_values()
        roster = sheet_unique_users(all_data)
        if not roster:
            await context.bot.send_message(chat_id=group_id, text="⚠️ No users found in the sheet yet.")
            user_state.pop(admin_id, None)
            return

        # Dry run lines
        # exlude nothing else; roster already skips header
        lines = [f"• {roster[uid]} ({uid})" for uid in roster.keys()]
        preview = "🧪 *Dry-run Preview*\n\n" \
                  f"Action: {fmt_act(action)}\n" \
                  f"Days: {days}\n" \
                  f"Application Date: {app_date}\n\n" \
                  f"Users to apply ({len(lines)}):\n" + "\n".join(lines)

        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "action": action,
            "is_ph": is_ph,
            "days": days,
            "app_date": app_date,
            "group_id": group_id,
            "roster": roster,  # dict: uid->name
            "request_by": admin_id
        }

        # send preview to admin PM
        try:
            m = await context.bot.send_message(
                chat_id=admin_id,
                text=preview,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm mass apply", callback_data=f"mass_confirm|{token}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"mass_cancel|{token}")
                ]])
            )
            admin_message_refs[token] = [(admin_id, m.message_id)]
            await context.bot.send_message(chat_id=group_id, text="📨 Preview sent to admin via PM for confirmation.")
        except Exception as e:
            logger.warning(f"⚠️ Cannot PM admin {admin_id}: {e}")
            await context.bot.send_message(
                chat_id=group_id,
                text="⚠️ I couldn't DM you the preview. Please /startadmin in PM and retry."
            )
        user_state.pop(admin_id, None)
    except Exception:
        logger.exception("❌ Failed to prepare mass preview")
        await context.bot.send_message(chat_id=group_id, text="❌ Failed to prepare mass preview.")
        user_state.pop(admin_id, None)

async def mass_apply(query, context, req):
    """
    Apply mass action to all users in roster.
    """
    action = req["action"]
    is_ph = req.get("is_ph", False)
    days = float(req.get("days", 0))
    app_date = req.get("app_date")
    group_id = req.get("group_id")
    roster = req.get("roster", {})

    try:
        all_data = worksheet.get_all_values()
        # build last final per user
        last_final = {}
        for row in all_data:
            if len(row) > COL_TG_ID and row[COL_TG_ID].isdigit():
                uid = row[COL_TG_ID]
                final = row[COL_FINAL] if len(row) > COL_FINAL and row[COL_FINAL] else "0"
                try:
                    last_final[uid] = float(final)
                except:
                    last_final[uid] = 0.0

        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows_to_append = []
        for uid, name in roster.items():
            curr = last_final.get(uid, 0.0)
            final = curr + days  # mass actions are always "clock" types
            add_subtract = f"+{days}"
            expiry = exp_from_appdate(app_date) if is_ph and action == "massclockphoff" else "N/A"
            act_label = "Clock Off" if action == "massclockoff" else "Clock PH Off"
            rows_to_append.append([
                now_ts,
                uid,
                name,
                act_label,
                f"{curr:.1f}",
                add_subtract,
                f"{final:.1f}",
                query.from_user.full_name,
                app_date,
                "Mass clock by Admin",
                "Yes" if is_ph else "No",
                "",  # PH Off Total (left blank; can be calculated elsewhere)
                expiry
            ])

        # Append rows in batch (faster)
        for row in rows_to_append:
            worksheet.append_row(row)

        await query.edit_message_text(
            f"✅ Mass apply completed for {len(rows_to_append)} users.\n"
            f"Action: {fmt_act(action)} | Days: {days} | App Date: {app_date}"
        )
        await context.bot.send_message(
            chat_id=group_id,
            text=f"✅ {fmt_act(action)} completed for {len(rows_to_append)} users by {query.from_user.full_name}."
        )
    except Exception:
        logger.exception("❌ Mass apply failed")
        await query.edit_message_text("❌ Mass apply failed. Please try again later.")

# ---------------- Callback handler ----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    uid = query.from_user.id

    # absorb benign callbacks
    if data == "noop":
        await query.answer()
        return

    # cancel
    if data == "cancel":
        user_state.pop(uid, None)
        await query.edit_message_text("🧹 Cancelled.")
        return

    # calendar navigation
    if data.startswith("cal_nav|"):
        _, tok, y, m = data.split("|")
        sess = calendar_sessions.get(tok)
        if not sess:
            await query.answer("Expired.")
            return
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await send_month_calendar(context, sess["chat_id"], tok, int(y), int(m), True, True)
        return

    # manual entry prompt
    if data.startswith("cal_manual|"):
        _, tok = data.split("|")
        sess = calendar_sessions.get(tok)
        if not sess:
            await query.answer("Expired.")
            return
        await query.edit_message_text("⌨️ Enter date manually in YYYY-MM-DD:")
        st = user_state.get(sess["user_id"], {})
        st["stage"] = "manual_date_entry"
        st["calendar_token"] = tok
        user_state[sess["user_id"]] = st
        return

    # date picked
    if data.startswith("cal_pick|"):
        _, tok, y, m, d = data.split("|")
        sess = calendar_sessions.get(tok)
        if not sess:
            await query.answer("Expired.")
            return
        app_date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        st = user_state.get(sess["user_id"], {})
        flow = sess.get("flow")

        if flow == "single":
            st["app_date"] = app_date
            st["stage"] = "awaiting_reason"
            await query.edit_message_text(f"📅 Selected: {app_date}")
            await context.bot.send_message(chat_id=sess["chat_id"], text=f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")
            user_state[sess["user_id"]] = st
            return

        if flow == "newuser_normal":
            st["newuser_normal_app_date"] = app_date
            st["stage"] = "newuser_after_normal_date"
            await query.edit_message_text(f"📅 Selected: {app_date}")
            await context.bot.send_message(chat_id=sess["chat_id"],
                text="✅ Noted. Now we’ll import PH entries.\nTap ➕ Add PH entry to start or ✅ Done PH import to finish.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add PH entry", callback_data="newuser_ph_add"),
                    InlineKeyboardButton("✅ Done PH import", callback_data="newuser_ph_done")
                ]])
            )
            user_state[sess["user_id"]] = st
            return

        if flow == "newuser_ph":
            cur = st.get("newuser_ph_current") or {}
            cur["date"] = app_date
            st["newuser_ph_current"] = cur
            st["stage"] = "newuser_ph_reason"
            await query.edit_message_text(f"📅 Selected: {app_date}")
            await context.bot.send_message(chat_id=sess["chat_id"], text=f"PH Entry — Enter Reason (max {MAX_REMARKS_LEN} chars)")
            user_state[sess["user_id"]] = st
            return

        if flow == "mass":
            st["app_date"] = app_date
            await query.edit_message_text(f"📅 Selected: {app_date}\nPreparing preview…")
            await mass_prepare_preview_and_send(update, context, st)
            return

        # default fallback
        st["app_date"] = app_date
        st["stage"] = "awaiting_reason"
        await query.edit_message_text(f"📅 Selected: {app_date}")
        await context.bot.send_message(chat_id=sess["chat_id"], text=f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")
        user_state[sess["user_id"]] = st
        return

    # /newuser PH add/done
    if data == "newuser_ph_add":
        st = user_state.get(uid)
        if not st:
            await query.answer()
            return
        st["newuser_ph_current"] = {}
        st["stage"] = "newuser_ph_app_date"
        tok = uuid.uuid4().hex[:8]
        st["calendar_token"] = tok
        calendar_sessions[tok] = {"user_id": uid, "chat_id": st["group_id"], "flow": "newuser_ph"}
        now = datetime.now()
        await query.edit_message_text("➕ Add PH entry — pick the PH date:")
        await send_month_calendar(context, st["group_id"], tok, now.year, now.month, True,
                                  show_cancel=True, prompt="📅 Select PH Application Date:")
        user_state[uid] = st
        return

    if data == "newuser_ph_done":
        st = user_state.get(uid)
        if not st:
            await query.answer()
            return
        normal_days = st.get("newuser_normal_days", 0)
        normal_date = st.get("newuser_normal_app_date", "N/A")
        items = st.get("newuser_ph_items", [])
        lines = "\n".join([f"• {it.get('date')} — {it.get('reason','')}" for it in items]) or "• (none)"
        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": uid,
            "user_full_name": query.from_user.full_name,
            "action": "newuser_import",
            "group_id": st["group_id"],
            "normal_days": normal_days,
            "normal_app_date": normal_date,
            "ph_list": items,
        }
        admin_message_refs[token] = []
        admins = await context.bot.get_chat_administrators(st["group_id"])
        for a in admins:
            if a.user.is_bot:
                continue
            txt = (
                f"🆕 *Onboarding — Import Old Records*\n\n"
                f"👤 User: {query.from_user.full_name}\n\n"
                f"Normal OIL to import: {normal_days} day(s)\n"
                f"• Application Date: {normal_date}\n"
                f"• Reason: Transfer from old record\n\n"
                f"PH entries ({len(items)}):\n{lines}\n\n"
                "Proceed to import?"
            )
            try:
                m = await context.bot.send_message(
                    chat_id=a.user.id,
                    text=txt,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve import", callback_data=f"approve|{token}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny|{token}")
                    ]])
                )
                admin_message_refs[token].append((a.user.id, m.message_id))
            except Exception as e:
                logger.warning(f"⚠️ Cannot PM admin {a.user.id}: {e}")
        await query.edit_message_text("✅ Submitted for admin review. You’ll be notified after approval.")
        user_state.pop(uid, None)
        return

    # approval / deny (single or onboarding)
    if data.startswith("approve|") or data.startswith("deny|"):
        kind, token = data.split("|", 1)
        req = pending_requests.get(token)
        if not req:
            await query.edit_message_text("⚠️ This request has expired or was already handled.")
            return

        if req["action"] == "newuser_import":
            await process_approve_onboarding(query, context, req, approve=(kind=="approve"))
        else:
            await process_approve_single(query, context, req, approve=(kind=="approve"))

        # cleanup fanout PMs
        if token in admin_message_refs:
            for admin_id, msg_id in admin_message_refs[token]:
                if admin_id != query.from_user.id:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=admin_id,
                            message_id=msg_id,
                            text=f"ℹ️ Decision recorded by {query.from_user.full_name}.",
                        )
                    except Exception:
                        pass
            del admin_message_refs[token]
        pending_requests.pop(token, None)
        return

    # MASS confirm/cancel
    if data.startswith("mass_confirm|") or data.startswith("mass_cancel|"):
        kind, token = data.split("|", 1)
        req = pending_requests.get(token)
        if not req:
            await query.edit_message_text("⚠️ This mass request has expired or was already handled.")
            return
        if kind == "mass_cancel":
            await query.edit_message_text("🧹 Mass request cancelled.")
        else:
            await mass_apply(query, context, req)
        pending_requests.pop(token, None)
        return

# ---------------- Approval processors ----------------
async def process_approve_single(query, context, req, approve: bool):
    user_id = req["user_id"]
    group_id = req["group_id"]
    action = req["action"]
    is_ph = req.get("is_ph", False)
    app_date = req.get("app_date", datetime.now().strftime("%Y-%m-%d"))
    days = float(req.get("days", 0))
    reason = req.get("reason", "")

    # resolve display name
    display_name = req.get("user_full_name") or str(user_id)
    try:
        member = await context.bot.get_chat_member(group_id, int(user_id))
        if member and member.user:
            display_name = build_name(member.user)
    except Exception:
        pass

    if not approve:
        await query.edit_message_text(
            f"❌ Denied.\n\nUser: {display_name}\nAction: {fmt_act(action)}\nDays: {days}\nApp Date: {app_date}\nReason: {reason}"
        )
        await context.bot.send_message(
            chat_id=group_id,
            text=f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
        )
        return

    # approve -> append row
    try:
        all_data = worksheet.get_all_values()
        rows = [row for row in all_data if len(row) > COL_TG_ID and row[COL_TG_ID] == str(user_id)]
        current_off = float(rows[-1][COL_FINAL]) if rows else 0.0
        final = current_off + days if action in ("clockoff", "clockphoff") else current_off - days
        add_subtract = f"+{days}" if action in ("clockoff", "clockphoff") else f"-{days}"
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        expiry = exp_from_appdate(app_date) if (is_ph and action == "clockphoff") else "N/A"
        worksheet.append_row([
            now_ts,
            str(user_id),
            display_name,
            fmt_act(action),
            f"{current_off:.1f}",
            add_subtract,
            f"{final:.1f}",
            query.from_user.full_name,
            app_date,
            reason,
            "Yes" if is_ph else "No",
            "",  # PH Off Total (leave blank; you can calculate separately)
            expiry
        ])

        await query.edit_message_text(
            "✅ Approved and recorded.\n\n"
            f"User: {display_name}\nAction: {fmt_act(action)}\nDays: {days}\n"
            f"App Date: {app_date}\nReason: {reason}\nFinal: {final:.1f}"
        )
        await context.bot.send_message(
            chat_id=group_id,
            text=(
                f"✅ {display_name}'s {fmt_act(action)} approved by {query.from_user.full_name}.\n"
                f"📅 Days: {days}\n"
                f"📆 Application Date: {app_date}\n"
                f"📝 Reason: {reason}\n"
                f"📊 Final: {final:.1f} day(s)"
            )
        )
    except Exception:
        logger.exception("❌ Failed to approve and record single")
        await query.edit_message_text("❌ Failed to approve request.")

async def process_approve_onboarding(query, context, req, approve: bool):
    user_id = req["user_id"]
    group_id = req["group_id"]
    display_name = req.get("user_full_name") or str(user_id)
    try:
        member = await context.bot.get_chat_member(group_id, int(user_id))
        if member and member.user:
            display_name = build_name(member.user)
    except Exception:
        pass

    if not approve:
        await query.edit_message_text(
            f"❌ Onboarding denied.\n\nUser: {display_name}"
        )
        await context.bot.send_message(chat_id=group_id, text=f"❌ Onboarding import for {display_name} was denied.")
        return

    try:
        all_data = worksheet.get_all_values()
        rows = [row for row in all_data if len(row) > COL_TG_ID and row[COL_TG_ID] == str(user_id)]
        current_off = float(rows[-1][COL_FINAL]) if rows else 0.0
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1) Normal import (if any) — reason auto "Transfer from old record"
        ndays = float(req.get("normal_days", 0))
        ndate = req.get("normal_app_date") or datetime.now().strftime("%Y-%m-%d")
        if ndays > 0:
            final = current_off + ndays
            worksheet.append_row([
                now_ts, str(user_id), display_name,
                "Clock Off",
                f"{current_off:.1f}", f"+{ndays}", f"{final:.1f}",
                query.from_user.full_name, ndate, "Transfer from old record",
                "No", "", "N/A"
            ])
            current_off = final

        # 2) PH entries (each +1.0 day)
        ph_list = req.get("ph_list", [])
        for item in ph_list:
            pdate = item.get("date")
            reason = item.get("reason", "")
            final = current_off + 1.0
            worksheet.append_row([
                now_ts, str(user_id), display_name,
                "Clock PH Off",
                f"{current_off:.1f}", f"+1.0", f"{final:.1f}",
                query.from_user.full_name, pdate, reason,
                "Yes", "", exp_from_appdate(pdate)
            ])
            current_off = final

        await query.edit_message_text(
            "✅ Onboarding imported and recorded.\n\n"
            f"User: {display_name}\n"
            f"Normal days: {ndays}\nPH entries: {len(ph_list)}"
        )
        await context.bot.send_message(
            chat_id=group_id,
            text=f"✅ Imported old records for {display_name} (Normal: {ndays}, PH: {len(ph_list)})."
        )
    except Exception:
        logger.exception("❌ Failed to process onboarding approval")
        await query.edit_message_text("❌ Failed to process onboarding approval.")

# ---------------- Manual date entry handler ----------------
async def handle_manual_date_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = user_state.get(uid)
    if not st or st.get("stage") != "manual_date_entry":
        return
    text = (update.message.text or "").strip()
    tok = st.get("calendar_token")
    sess = calendar_sessions.get(tok)
    if not sess:
        await update.message.reply_text("⚠️ Date session expired. Please try again.")
        st["stage"] = "awaiting_app_date"
        user_state[uid] = st
        return
    # validate
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except:
        await update.message.reply_text("❌ Invalid date. Please enter in YYYY-MM-DD.")
        return

    flow = sess.get("flow")
    if flow == "single":
        st["app_date"] = text
        st["stage"] = "awaiting_reason"
        await update.message.reply_text(f"📅 Selected: {text}\n\n📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)")
        return
    if flow == "newuser_normal":
        st["newuser_normal_app_date"] = text
        st["stage"] = "newuser_after_normal_date"
        await update.message.reply_text(
            f"📅 Selected: {text}\n\n"
            "✅ Noted. Now we’ll import PH entries.\nTap ➕ Add PH entry to start or ✅ Done PH import to finish.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add PH entry", callback_data="newuser_ph_add"),
                InlineKeyboardButton("✅ Done PH import", callback_data="newuser_ph_done")
            ]])
        )
        return
    if flow == "newuser_ph":
        cur = st.get("newuser_ph_current") or {}
        cur["date"] = text
        st["newuser_ph_current"] = cur
        st["stage"] = "newuser_ph_reason"
        await update.message.reply_text(f"📅 Selected: {text}\n\nPH Entry — Enter Reason (max {MAX_REMARKS_LEN} chars)")
        return
    if flow == "mass":
        st["app_date"] = text
        await update.message.reply_text(f"📅 Selected: {text}\nPreparing preview…")
        await mass_prepare_preview_and_send(update, context, st)
        return

# ---------------- Init ----------------
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
    telegram_app.add_handler(CommandHandler("clockphoff", clockphoff))
    telegram_app.add_handler(CommandHandler("claimphoff", claimphoff))

    telegram_app.add_handler(CommandHandler("massclockoff", massclockoff))
    telegram_app.add_handler(CommandHandler("massclockphoff", massclockphoff))

    telegram_app.add_handler(CommandHandler("newuser", newuser))

    # message handlers: manual date first, then general stage handler
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_date_entry))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # callbacks
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook set.")

# ---------------- Run ----------------
if __name__ == "__main__":
    nest_asyncio.apply()
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(init_app()))
    logger.info("🟢 Starting Flask...")
    app.run(host="0.0.0.0", port=10000)