import os
import sys
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in Vercel environment variables")

if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing in Vercel environment variables")

app = FastAPI()

telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

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
    try:
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

    except HTTPException:
        raise

    except Exception as error:
        print(traceback.format_exc())

        return JSONResponse(
            status_code=500,
            content={
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )