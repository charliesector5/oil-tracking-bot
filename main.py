from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from flask import Flask
from threading import Thread
import sqlite3
import os

# === Your Telegram Bot Token ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
print("DEBUG: BOT_TOKEN =", BOT_TOKEN)

# === Setup SQLite Database ===
conn = sqlite3.connect("oil.db", check_same_thread=False)
c = conn.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS personnel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    name TEXT,
    is_admin INTEGER DEFAULT 0,
    balance REAL DEFAULT 0
)""")

c.execute("""CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    days REAL,
    remarks TEXT,
    status TEXT DEFAULT 'pending'
)""")

conn.commit()

# === Keep Alive Flask Server for UptimeRobot ===
app_web = Flask('')

@app_web.route('/')
def home():
    return "✅ OIL Bot is alive."

def run():
    app_web.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# === Group Admin Check ===
async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(admin.user.id == user_id for admin in admins)
    except Exception as e:
        print("Admin check failed:", e)
        return False

# === Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Bot. Use /claim_off <days> <remarks> to submit a claim.")

async def claim_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = float(context.args[0])
        remarks = " ".join(context.args[1:])
        user_id = update.effective_user.id

        c.execute("INSERT INTO claims (user_id, days, remarks) VALUES (?, ?, ?)", (user_id, days, remarks))
        conn.commit()

        claim_id = c.lastrowid
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{claim_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{claim_id}")
            ]
        ]

        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("OIL claim submitted.", reply_markup=None)

        # Send to all admins
        c.execute("SELECT telegram_id FROM personnel WHERE is_admin=1")
        for row in c.fetchall():
            await context.bot.send_message(chat_id=row[0],
                                           text=f"New OIL Claim\nUser: {update.effective_user.full_name}\nDays: {days}\nReason: {remarks}",
                                           reply_markup=markup)
    except Exception as e:
        print("Error in claim_off:", e)
        await update.message.reply_text("Usage: /claim_off <days> <remarks>")

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, claim_id = query.data.split("_")
    c.execute("SELECT user_id, days FROM claims WHERE id = ?", (claim_id,))
    result = c.fetchone()

    if result:
        user_id, days = result
        if action == "approve":
            c.execute("UPDATE claims SET status = 'approved' WHERE id = ?", (claim_id,))
            c.execute("UPDATE personnel SET balance = balance - ? WHERE telegram_id = ?", (days, user_id))
            await context.bot.send_message(chat_id=user_id, text=f"✅ Your OIL claim of {days} day(s) has been approved.")
        elif action == "reject":
            c.execute("UPDATE claims SET status = 'rejected' WHERE id = ?", (claim_id,))
            await context.bot.send_message(chat_id=user_id, text=f"❌ Your OIL claim has been rejected.")
        conn.commit()
        await query.edit_message_text("Action completed.")
    else:
        await query.edit_message_text("Claim not found.")

# === Main ===
def main():
    keep_alive()  # Keeps bot alive via HTTP ping
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("claim_off", claim_off))
    app.add_handler(CallbackQueryHandler(handle_approval))
    app.run_polling()

if __name__ == "__main__":
    main()