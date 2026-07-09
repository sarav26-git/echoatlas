import os
import sys

import requests


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

    if not token:
        print("Missing TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    if not webhook_url:
        print("Missing WEBHOOK_URL")
        sys.exit(1)

    api_url = f"https://api.telegram.org/bot{token}/setWebhook"
    payload = {"url": webhook_url}
    if secret:
        payload["secret_token"] = secret

    resp = requests.post(api_url, data=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("ok"):
        print(data)
        sys.exit(1)

    print("Webhook registered successfully.")
    print(f"URL: {webhook_url}")


if __name__ == "__main__":
    main()
