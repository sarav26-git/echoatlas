# echoatlas-bot
EchoAtlas is a Telegram bot that delivers Instant Song Metadata, Lyrics and Descriptive insights. It enables users to quickly access metadata of a song such as Artist details, Album, Release year and Genre.

## Vercel deployment

This repo now supports Telegram webhooks on Vercel.

1. Set these environment variables in Vercel:
   - `TELEGRAM_BOT_TOKEN`
   - `GENIUS_ACCESS_TOKEN`
   - `TELEGRAM_WEBHOOK_SECRET` or leave it unset
2. Deploy the project to Vercel.
3. Register the webhook with Telegram once the deployment is live:

```bash
curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-vercel-domain>/api/webhook" \
  -d "secret_token=<your-telegram-webhook-secret>"
```

Or run the helper script after setting the same environment variables locally:

```bash
python set_webhook.py
```

For local runs, `python song_metadata_bot.py` still uses polling.
