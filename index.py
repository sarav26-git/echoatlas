import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

sys.path.append(str(Path(__file__).resolve().parent.parent))

from song_metadata_bot import (
    start,
    help_command,
    handle_song_search,
    handle_song_selection,
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

app = FastAPI()

telegram_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .build()
)

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song_search)
)
telegram_app.add_handler(CallbackQueryHandler(handle_song_selection))


@app.get("/")
async def home():
    return {"status": "EchoAtlas is online"}


@app.post("/webhook")
async def webhook(request: Request):
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if received_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if not telegram_app._initialized:
        await telegram_app.initialize()

    update_data = await request.json()
    update = Update.de_json(update_data, telegram_app.bot)

    await telegram_app.process_update(update)

    return {"ok": True}
