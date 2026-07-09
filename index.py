import asyncio
import json
import os

from telegram import Update

from song_metadata_bot import TELEGRAM_TOKEN, build_application, logger

_telegram_app = None
_telegram_app_ready = False


def _get_telegram_app():
    global _telegram_app
    if _telegram_app is None:
        _telegram_app = build_application()
    return _telegram_app


async def _dispatch_update(payload):
    telegram_app = _get_telegram_app()
    global _telegram_app_ready
    if not _telegram_app_ready:
        await telegram_app.initialize()
        _telegram_app_ready = True
    try:
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception:
        raise


def _json_response(body, status=200):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(request):
    if TELEGRAM_TOKEN in {"", "YOUR_TELEGRAM_BOT_TOKEN", "ENTER_TEL_TOKEN"}:
        return _json_response({"ok": False, "error": "Telegram token is not configured"}, 500)

    method = getattr(request, "method", "GET").upper()
    if method == "GET":
        return _json_response({"ok": True, "service": "echoatlas-bot"})
    if method != "POST":
        return _json_response({"ok": False, "error": "Method not allowed"}, 405)

    secret_token = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if secret_token:
        incoming_secret = ""
        headers = getattr(request, "headers", {}) or {}
        if hasattr(headers, "get"):
            incoming_secret = headers.get("x-telegram-bot-api-secret-token", "")
        elif isinstance(headers, dict):
            incoming_secret = headers.get("x-telegram-bot-api-secret-token", "")
        if incoming_secret != secret_token:
            return _json_response({"ok": False, "error": "Unauthorized"}, 403)

    payload = None
    if hasattr(request, "json"):
        payload = request.json
    if payload is None and hasattr(request, "body"):
        raw_body = request.body
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        if raw_body:
            payload = json.loads(raw_body)
    if not payload:
        return _json_response({"ok": False, "error": "Missing JSON payload"}, 400)

    try:
        asyncio.run(_dispatch_update(payload))
    except Exception as exc:
        logger.exception("Webhook dispatch failed")
        return _json_response({"ok": False, "error": str(exc)}, 500)

    return _json_response({"ok": True})


app = handler
