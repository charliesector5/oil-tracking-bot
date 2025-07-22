import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)

# Load environment variables from .env file
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_ACTUAL_BOT_TOKEN_HERE")

# Example command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I’m alive and working ✅")

# Example command: /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands:\n/start - Start the bot\n/help - Show this help message")

def main():
    # Create Application instance
    application = Application.builder().token(BOT_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # Start the bot
    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
