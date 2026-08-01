from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
import os

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! I am Allmightsee Prime.\n\nI'm alive and ready to trade!"
    )

    response = requests.get(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    )

    data = response.json()
    current_price = data["bitcoin"]["usd"]

    await update.message.reply_text(
        f"📈 Current Bitcoin Price: ${current_price:,.2f}"
    )

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))

print("🤖 Allmightsee Prime is running...")

app.run_polling()