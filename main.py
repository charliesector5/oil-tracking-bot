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
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Chat,
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
from datetime import datetime, timedelta, date as date_cls

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

# conversation + flow states
user_state = {}

# approval tokens -> payload (short callback_data)
pending_requests = {}        # for normal /clockoff, /claimoff if you ever reuse
pending_onboard = {}         # token -> onboarding payload

# track admin PM message ids to clean up/mark as handled
admin_message_refs = {}      # token -> [(admin_id, message_id), ...]

# inline calendar sessions (short tokens)
calendar_sessions = {}       # token -> {"user_id", "target": ("state_key", ...), "year", "month"}

# --- Settings ---
MAX_REMARKS_LEN = 80  # general cap to keep messages readable and safe

# --- Utilities ---
def validate_date_str(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

async def resolve_display_name(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, fallback: str) -> str:
    name = fallback or str(user_id)
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member and member.user:
            if member.user.full_name:
                name = member.user.full_name
            elif member.user.username:
                name = f"@{member.user.username}"
    except Exception as e:
        logger.warning(f"⚠️ Could not resolve name for {user_id} in chat {chat_id}: {e}")
    return name

def add_days_365(yyyy_mm_dd: str) -> str:
    # Expiry = application date + 365 days
    try:
        d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").date()
        exp = d + timedelta(days=365)
        return exp.strftime("%Y-%m-%d")
    except Exception:
        return "N/A"

def last_balance_for(user_id: str) -> float:
    try:
        all_data = worksheet.get_all_values()
        rows = [row for row in all_data if len(row) > 6 and row[1] == user_id]
        if rows:
            return float(rows[-1][6])
    except Exception as e:
        logger.warning(f"balance lookup failed for {user_id}: {e}")
    return 0.0

def is_group_chat(chat: Chat) -> bool:
    return chat.type in ("group", "supergroup")

# --- Inline Calendar ---
def calendar_keyboard(year: int, month: int, token: str, include_manual=True):
    cal = calendar.Calendar(firstweekday=0)  # Monday=0? Telegram weeks: we'll show Mon..Sun labels
    month_days = cal.monthdayscalendar(year, month)

    # Header row: <<  Month YYYY  >>
    month_name = calendar.month_name[month]
    header = [
        InlineKeyboardButton("«", callback_data=f"cal|prev|{token}"),
        InlineKeyboardButton(f"{month_name} {year}", callback_data=f"cal|noop|{token}"),
        InlineKeyboardButton("»", callback_data=f"cal|next|{token}")
    ]

    # Weekday labels
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    label_row = [InlineKeyboardButton(x, callback_data=f"cal|noop|{token}") for x in labels]

    # Day grid
    rows = []
    # Python's Calendar defaults Monday-first; monthdayscalendar returns weeks with zeros for padding days.
    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data=f"cal|noop|{token}"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal|pick|{token}|{day}"))
        rows.append(row)

    kb = [header, label_row] + rows
    if include_manual:
        kb.append([InlineKeyboardButton("⌨️ Type date manually", callback_data=f"cal|manual|{token}")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{token}")])
    return InlineKeyboardMarkup(kb)

async def send_calendar_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_user_id: int, target_key: str, title: str):
    now = datetime.now()
    token = uuid.uuid4().hex[:10]
    calendar_sessions[token] = {
        "user_id": owner_user_id,
        "target": target_key,   # which state key to fill when picked (e.g., "import_application_date" or "ph_pending_date")
        "year": now.year,
        "month": now.month,
        "chat_id": update.effective_chat.id,
        "message_id": None,
    }
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📅 {title}\nPick a date or type manually.",
        reply_markup=calendar_keyboard(now.year, now.month, token)
    )
    calendar_sessions[token]["message_id"] = msg.message_id

async def handle_calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    # parts like: ["cal", action, token, (optional) day]
    query = update.callback_query
    action = parts[1]
    token = parts[2]
    session = calendar_sessions.get(token)
    if not session:
        await query.answer("Session expired.")
        return

    # Only the original user can interact with this calendar
    if update.effective_user.id != session["user_id"]:
        await query.answer("Not your calendar.", show_alert=True)
        return

    y, m = session["year"], session["month"]

    if action == "noop":
        await query.answer()
        return

    if action == "manual":
        # Switch to manual input stage for this date target
        st = user_state.get(session["user_id"], {})
        st["stage"] = f"awaiting_{session['target']}_manual"
        user_state[session["user_id"]] = st
        await query.edit_message_text("⌨️ Please type the date as YYYY-MM-DD.")
        return

    if action == "prev":
        # go to previous month
        m -= 1
        if m < 1:
            m = 12
            y -= 1
        session["year"], session["month"] = y, m
        await query.edit_message_reply_markup(reply_markup=calendar_keyboard(y, m, token))
        return

    if action == "next":
        m += 1
        if m > 12:
            m = 1
            y += 1
        session["year"], session["month"] = y, m
        await query.edit_message_reply_markup(reply_markup=calendar_keyboard(y, m, token))
        return

    if action == "pick":
        day = int(parts[3])
        try:
            picked = date_cls(year=y, month=m, day=day).strftime("%Y-%m-%d")
        except Exception:
            await query.answer("Invalid date.")
            return

        # Write back into user_state under the target key
        st = user_state.get(session["user_id"], {})
        st[session["target"]] = picked
        user_state[session["user_id"]] = st

        await query.edit_message_text(f"📅 Selected date: {picked}")
        # Continue flow based on which key was filled:
        # The flow code will check for presence of that key and move on.
        return

# --- Cancel handling ---
async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str | None):
    query = update.callback_query
    uid = update.effective_user.id
    user_state.pop(uid, None)
    await query.edit_message_text("❌ Cancelled.")

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
        "/newuser – New user onboarding/import\n"
        "/startadmin – PM only: start admin session\n"
        "/summary – Your current balance & PH details\n"
        "/history – Your past 5 logs\n"
        "/help – Show this help message\n\n"
        "Tip: You can always tap ❌ Cancel or type -quit to abort.",
        parse_mode="Markdown"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # unchanged from your working logic (kept minimal here)
    user = update.effective_user
    try:
        all_data = worksheet.get_all_values()
        user_rows = [row for row in all_data if len(row) > 6 and row[1] == str(user.id)]
        if user_rows:
            last_row = user_rows[-1]
            balance = last_row[6]
            # simple total PH Off Total sum from column 12 (index 12) if present
            ph_total = 0.0
            for r in user_rows:
                if len(r) >= 13 and r[12]:
                    try:
                        ph_total += float(r[12])
                    except:
                        pass
            # List *active* PH entries (Holiday Off == Yes) that are not expired and not negative adds
            today = datetime.now().date()
            entries = []
            for r in user_rows:
                if len(r) >= 12 and (len(r) > 10):
                    holiday_flag = (r[10].strip().lower() == "yes")
                    if holiday_flag:
                        app_date = r[8] if len(r) > 8 else ""
                        exp = r[11] if len(r) > 11 else "N/A"
                        remark = r[9] if len(r) > 9 else ""
                        addsub = r[5] if len(r) > 5 else ""
                        if addsub.startswith("+"):
                            # check expiry
                            if exp and exp != "N/A":
                                try:
                                    if datetime.strptime(exp, "%Y-%m-%d").date() >= today:
                                        entries.append(f"• {app_date}: +1.0 (exp {exp}) - {remark}")
                                except:
                                    pass
            body = "🔎 PH Off Entries:\n" + ("\n".join(entries) if entries else "• None")
            await update.message.reply_text(
                f"📊 Current Off Balance: {balance} day(s).\n"
                f"🏖 PH Off Total: {ph_total:.1f} day(s)\n"
                f"{body}"
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
        user_rows = [row for row in all_data if len(row) > 6 and row[1] == str(user.id)]
        if user_rows:
            last_5 = user_rows[-5:]
            response = "\n".join([f"{row[0]} | {row[3]} | {row[5]} → {row[6]} | {row[8]}" for row in last_5])
            await update.message.reply_text(f"📜 Your last 5 OIL logs:\n\n{response}")
        else:
            await update.message.reply_text("📜 No logs found.")
    except Exception:
        logger.exception("❌ Failed to fetch history")
        await update.message.reply_text("❌ Could not retrieve your logs.")

# --- Existing clock/claim (kept) ---
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"flow": "normal", "action": "clockoff", "stage": "awaiting_days"}
    await update.message.reply_text(
        "🕒 How many days do you want to clock off? (0.5 to 3, in 0.5 increments)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")]])
    )

async def claimoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"flow": "normal", "action": "claimoff", "stage": "awaiting_days"}
    await update.message.reply_text(
        "🧾 How many days do you want to claim off? (0.5 to 3, in 0.5 increments)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")]])
    )

# --- NEW: Onboarding /newuser ---
async def newuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # check if exists in sheet
    try:
        all_data = worksheet.get_all_values()
        exists = any((len(r) > 1 and r[1] == str(uid)) for r in all_data)
        if exists:
            await update.message.reply_text("✅ You’re already registered in this sheet.")
            return
    except Exception:
        logger.exception("lookup failed")

    # start wizard
    user_state[uid] = {
        "flow": "onboard",
        "stage": "awaiting_import_days",
        "import_days": None,
        "import_application_date": None,
        "ph_list": [],  # list of {"date": "YYYY-MM-DD", "remark": "..."}
        "group_id": update.effective_chat.id if is_group_chat(update.effective_chat) else None,
    }
    await update.message.reply_text(
        "🆕 *New User Onboarding*\n\n"
        "First, how many *normal OIL* days to import from your old record?\n"
        "• Enter a positive number (no 0.5 restriction here)\n"
        "• Or tap Skip if none",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Skip", callback_data="onb|skip_import"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")
        ]])
    )

async def startadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update.effective_chat):
        await update.message.reply_text("ℹ️ Please PM me and run /startadmin there.")
        return
    await update.message.reply_text(
        "👋 Admin session is ready.\nYou’ll receive approval prompts here whenever someone submits a request."
    )

# --- Message handler (all text replies) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # universal quit fallback
    if text.lower() == "-quit":
        user_state.pop(user_id, None)
        await update.message.reply_text("❌ Cancelled.")
        return

    if user_id not in user_state:
        return

    state = user_state[user_id]
    flow = state.get("flow")

    # ---- Normal flow (existing) ----
    if flow == "normal":
        if state["stage"] == "awaiting_days":
            try:
                days = float(text)
                # block negatives and zero
                if days <= 0:
                    raise ValueError()
                # 0.5 step
                if (days * 10) % 5 != 0 or days > 3:
                    raise ValueError()
                state["days"] = days
                state["stage"] = "awaiting_reason"
                await update.message.reply_text(
                    f"📝 What's the reason? (Max {MAX_REMARKS_LEN} characters)",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")]])
                )
            except ValueError:
                await update.message.reply_text("❌ Invalid input. Enter a number between 0.5 to 3 (0.5 steps).")
        elif state["stage"] == "awaiting_reason":
            reason = text
            if len(reason) > MAX_REMARKS_LEN:
                reason = reason[:MAX_REMARKS_LEN]
                await update.message.reply_text(f"✂️ Remarks trimmed to {MAX_REMARKS_LEN} characters.")
            state["reason"] = reason
            state["group_id"] = update.message.chat_id
            await update.message.reply_text("📩 Your request has been submitted for approval.")
            await send_approval_request_normal(update, context, state)
            user_state.pop(user_id, None)
        return

    # ---- Onboarding flow ----
    if flow == "onboard":
        stage = state.get("stage")

        if stage == "awaiting_import_days":
            # accept positive floats, or tell them to press Skip button
            try:
                days = float(text)
                if days <= 0:
                    raise ValueError()
                state["import_days"] = days
                state["stage"] = "awaiting_import_date_prompt"
                # ask for application date (calendar)
                await send_calendar_prompt(update, context, user_id, "import_application_date",
                                           "Pick *Application Date* for the normal OIL import")
            except ValueError:
                await update.message.reply_text("❌ Enter a positive number, or tap Skip if none.",
                                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="onb|skip_import"),
                                                                                    InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")]]))
            return

        if stage == "awaiting_import_date_prompt":
            # this stage is moved by calendar; if they typed manually:
            if state.get("import_application_date") is None:
                # waiting for manual typed date
                if validate_date_str(text):
                    state["import_application_date"] = text
                else:
                    await update.message.reply_text("❌ Date format must be YYYY-MM-DD. Try again.")
                    return
            # proceed to PH loop menu
            state["stage"] = "ph_menu"
            await update.message.reply_text(
                "🏖 Now let's add *PH OIL* entries.\nEach PH entry is **1 day** with its own date and remark.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add PH entry", callback_data="onb|add_ph"),
                    InlineKeyboardButton("Done / Skip", callback_data="onb|done_ph"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")
                ]])
            )
            return

        if stage == "awaiting_import_application_date_manual":
            if validate_date_str(text):
                state["import_application_date"] = text
                # proceed to PH menu
                state["stage"] = "ph_menu"
                await update.message.reply_text(
                    "🏖 Now let's add *PH OIL* entries.\nEach PH entry is **1 day** with its own date and remark.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("➕ Add PH entry", callback_data="onb|add_ph"),
                        InlineKeyboardButton("Done / Skip", callback_data="onb|done_ph"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")
                    ]])
                )
            else:
                await update.message.reply_text("❌ Date format must be YYYY-MM-DD. Try again.")
            return

        if stage == "awaiting_ph_date_manual":
            if validate_date_str(text):
                state["ph_pending_date"] = text
                state["stage"] = "awaiting_ph_remark"
                await update.message.reply_text(
                    "📝 PH remark for this entry?",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")]])
                )
            else:
                await update.message.reply_text("❌ Date format must be YYYY-MM-DD. Try again.")
            return

        if stage == "awaiting_ph_remark":
            remark = text[:MAX_REMARKS_LEN] if text else ""
            ph_date = state.get("ph_pending_date")
            if not ph_date:
                await update.message.reply_text("❌ Internal error: missing PH date. Tap Add PH again.")
                state["stage"] = "ph_menu"
                return
            state["ph_list"].append({"date": ph_date, "remark": remark})
            # clear pending
            state.pop("ph_pending_date", None)
            state["stage"] = "ph_menu"
            await update.message.reply_text(
                "✅ PH entry added.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add another PH", callback_data="onb|add_ph"),
                    InlineKeyboardButton("Done / Skip", callback_data="onb|done_ph"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")
                ]])
            )
            return

        # awaiting_* calendar manual fallbacks
        if stage == "awaiting_import_application_date_manual":
            # already handled above
            return

    # unknown state fallback
    await update.message.reply_text("❓ I'm not sure what to do. Try /help or type -quit to cancel.")

# --- Admin approval: normal flow (existing pattern) ---
async def send_approval_request_normal(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    group_id = state["group_id"]
    try:
        current_off = last_balance_for(str(user.id))
        delta = float(state["days"])
        if state["action"] == "clockoff":
            new_off = current_off + delta
        else:
            # claimoff: prevent negative addition (safeguard already in input)
            new_off = current_off - delta

        token = uuid.uuid4().hex[:10]
        pending_requests[token] = {
            "user_id": user.id,
            "user_full_name": user.full_name,
            "action": state["action"],
            "days": state["days"],
            "reason": state["reason"],
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
                        f"🆕 *{state['action'].title()} Request*\n\n"
                        f"👤 User: {user.full_name}\n"
                        f"📅 Days: {state['days']}\n"
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

# --- Admin approval: onboarding ---
async def send_approval_request_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: dict):
    """payload contains:
       user_id, user_full_name, group_id,
       import_days (float or None), import_application_date (str or None),
       ph_list: list of {"date", "remark"}
    """
    user_id = payload["user_id"]
    full_name = payload.get("user_full_name") or str(user_id)
    group_id = payload["group_id"]

    # Preview text
    lines = [f"🆕 *New User Import*",
             f"👤 User: {full_name}",
             ""]
    if payload.get("import_days"):
        lines.append(f"🧾 Normal OIL to import: {payload['import_days']} day(s)")
        lines.append(f"📅 Application Date: {payload.get('import_application_date')}")
        lines.append(f"📝 Reason: Transfer from old record")
        lines.append("")
    ph_list = payload.get("ph_list", [])
    lines.append(f"🏖 PH entries: {len(ph_list)}")
    for i, ph in enumerate(ph_list, 1):
        lines.append(f"  {i}. {ph['date']} – {ph['remark']}")
    text = "\n".join(lines)

    token = uuid.uuid4().hex[:10]
    pending_onboard[token] = payload
    admin_message_refs[token] = []

    admins = await context.bot.get_chat_administrators(group_id)
    for admin in admins:
        if admin.user.is_bot:
            continue
        try:
            msg = await context.bot.send_message(
                chat_id=admin.user.id,
                text=text + "\n\nApprove import?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"onbapprove|{token}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"onbdeny|{token}")
                ]])
            )
            admin_message_refs[token].append((admin.user.id, msg.message_id))
        except Exception as e:
            logger.warning(f"⚠️ Cannot PM admin {admin.user.id}: {e}")

# --- Callback handler ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        # Calendar handling
        if data.startswith("cal|"):
            parts = data.split("|")
            await handle_calendar_callback(update, context, parts)
            return

        # Cancel button (generic)
        if data.startswith("cancel|"):
            token = data.split("|", 1)[1] if "|" in data else None
            await handle_cancel_callback(update, context, token)
            return

        # Onboarding control buttons
        if data.startswith("onb|"):
            # onb|skip_import, onb|add_ph, onb|done_ph
            _, action = data.split("|", 1)
            uid = update.effective_user.id
            st = user_state.get(uid, {})
            if st.get("flow") != "onboard":
                await query.edit_message_text("⚠️ This onboarding session has ended.")
                return

            if action == "skip_import":
                st["import_days"] = None
                st["import_application_date"] = None
                st["stage"] = "ph_menu"
                await query.edit_message_text(
                    "✅ Skipped normal OIL import.\n\n"
                    "🏖 Now let's add *PH OIL* entries.\nEach PH entry is **1 day** with its own date and remark.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("➕ Add PH entry", callback_data="onb|add_ph"),
                        InlineKeyboardButton("Done / Skip", callback_data="onb|done_ph"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel|_")
                    ]]),
                    parse_mode="Markdown"
                )
                return

            if action == "add_ph":
                # ask PH date via calendar
                st["stage"] = "awaiting_ph_date"
                user_state[uid] = st
                await send_calendar_prompt(update, context, uid, "ph_pending_date", "Pick *PH Date*")
                return

            if action == "done_ph":
                # Show preview and send to admins
                # group context: if this was started in PM, we need a group_id (take last group used? require it's started in group ideally)
                group_id = st.get("group_id") or update.effective_chat.id
                payload = {
                    "user_id": uid,
                    "user_full_name": update.effective_user.full_name,
                    "group_id": group_id,
                    "import_days": st.get("import_days"),
                    "import_application_date": st.get("import_application_date"),
                    "ph_list": st.get("ph_list", []),
                }
                await query.edit_message_text("📤 Sending import request to admins for approval…")
                await send_approval_request_onboard(update, context, payload)
                # clear state
                user_state.pop(uid, None)
                return

        # Approve/Deny for existing normal requests (Issue #1 pattern)
        if data.startswith("approve|") or data.startswith("deny|"):
            action_type, token = data.split("|", 1)
            req = pending_requests.get(token)
            if not req:
                await query.edit_message_text("⚠️ This request has expired or was already handled.")
                return

            user_id = int(req["user_id"])
            user_full_name = req.get("user_full_name") or str(user_id)
            action = req["action"]
            days = float(req["days"])
            reason = req["reason"]
            group_id = int(req["group_id"])

            if action_type == "approve":
                timestamp = fmt(datetime.now())
                app_date = datetime.now().strftime("%Y-%m-%d")
                current = last_balance_for(str(user_id))
                final = current + days if action == "clockoff" else current - days
                add_subtract = f"+{days}" if action == "clockoff" else f"-{days}"

                # Append with extended columns (Holiday Off / Expiry / PH Off Total)
                worksheet.append_row([
                    timestamp, str(user_id), user_full_name,
                    "Clock Off" if action == "clockoff" else "Claim Off",
                    f"{current:.1f}", add_subtract, f"{final:.1f}",
                    query.from_user.full_name, app_date, reason,
                    "No", "N/A", "0"
                ])

                display_name = await resolve_display_name(context, group_id, user_id, user_full_name)
                await query.edit_message_text(
                    f"✅ Approved.\nUser: {display_name}\nAction: {action.replace('off',' Off')}\nDays: {days}\nReason: {reason}\nFinal: {final:.1f}"
                )
                await context.bot.send_message(
                    chat_id=group_id,
                    text=(
                        f"✅ {display_name}'s {action.replace('off', ' Off')} approved by {query.from_user.full_name}.\n"
                        f"📅 Days: {days}\n"
                        f"📝 Reason: {reason}\n"
                        f"📊 Final: {final:.1f} day(s)"
                    )
                )
            else:
                display_name = await resolve_display_name(context, req["group_id"], user_id, user_full_name)
                await query.edit_message_text(
                    f"❌ Denied.\nUser: {display_name}\nAction: {req['action'].replace('off',' Off')}\nDays: {req['days']}\nReason: {reason}"
                )
                await context.bot.send_message(
                    chat_id=int(req["group_id"]),
                    text=f"❌ {display_name}'s request was denied by {query.from_user.full_name}.\n📝 Reason: {reason}"
                )

            # mark other admin PMs
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

        # Approve/Deny for onboarding
        if data.startswith("onbapprove|") or data.startswith("onbdeny|"):
            action_type, token = data.split("|", 1)
            req = pending_onboard.get(token)
            if not req:
                await query.edit_message_text("⚠️ This import request has expired or was handled.")
                return

            user_id = int(req["user_id"])
            full_name = req.get("user_full_name") or str(user_id)
            group_id = int(req["group_id"])

            if action_type == "onbapprove":
                # Append rows
                current = last_balance_for(str(user_id))
                nowts = fmt(datetime.now())

                # 1) Normal import (if any)
                if req.get("import_days"):
                    days = float(req["import_days"])
                    if days <= 0:
                        # ignore invalid
                        pass
                    else:
                        final = current + days
                        worksheet.append_row([
                            nowts, str(user_id), full_name,
                            "Clock Off",
                            f"{current:.1f}", f"+{days}", f"{final:.1f}",
                            query.from_user.full_name,
                            req.get("import_application_date") or datetime.now().strftime("%Y-%m-%d"),
                            "Transfer from old record",
                            "No", "N/A", "0"
                        ])
                        current = final  # update running balance

                # 2) PH entries (each +1)
                for ph in req.get("ph_list", []):
                    ph_date = ph["date"]
                    remark = ph["remark"][:MAX_REMARKS_LEN] if ph.get("remark") else ""
                    days = 1.0
                    final = current + days
                    expiry = add_days_365(ph_date)
                    worksheet.append_row([
                        nowts, str(user_id), full_name,
                        "Clock Off",
                        f"{current:.1f}", f"+{days}", f"{final:.1f}",
                        query.from_user.full_name,
                        ph_date,
                        remark,
                        "Yes", expiry, "1.0"
                    ])
                    current = final

                display_name = await resolve_display_name(context, group_id, user_id, full_name)
                await query.edit_message_text(
                    f"✅ New user import approved.\nUser: {display_name}\n"
                    f"Normal import: {req.get('import_days') or 0} day(s)\n"
                    f"PH entries: {len(req.get('ph_list', []))}"
                )
                await context.bot.send_message(
                    chat_id=group_id,
                    text=(f"🆕✅ Onboarding import approved for {display_name} by {query.from_user.full_name}.\n"
                          f"• Normal import: {req.get('import_days') or 0} day(s)\n"
                          f"• PH entries: {len(req.get('ph_list', []))}")
                )
            else:
                display_name = await resolve_display_name(context, group_id, user_id, full_name)
                await query.edit_message_text(
                    f"❌ New user import denied for {display_name}."
                )
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"🆕❌ Onboarding import denied for {display_name} by {query.from_user.full_name}."
                )

            # mark other admin PMs
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
            pending_onboard.pop(token, None)
            return

        # Onboarding: calendar manual fallbacks
        if data.startswith("cal|") is False and data.startswith("onb|") is False and data.startswith("cancel|") is False:
            await query.edit_message_text("⚠️ Unknown action.")

    except Exception:
        logger.exception("❌ Failed to process callback")
        try:
            await query.edit_message_text("❌ Something went wrong.")
        except Exception:
            pass

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

    # Onboarding
    telegram_app.add_handler(CommandHandler("newuser", newuser))
    telegram_app.add_handler(CommandHandler("startadmin", startadmin))

    # Text replies for wizards
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Callbacks: calendar / approvals / onboarding
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
