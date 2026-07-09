import json

import api.index as webhook


class DummyRequest:
    def __init__(self, method="GET", json_body=None, headers=None, body=None):
        self.method = method
        self.json = json_body
        self.headers = headers or {}
        self.body = body


def run():
    webhook.TELEGRAM_TOKEN = "TEST_TOKEN"

    health = webhook.handler(DummyRequest("GET"))
    assert health["statusCode"] == 200
    assert json.loads(health["body"])["ok"] is True

    missing = webhook.handler(DummyRequest("POST"))
    assert missing["statusCode"] == 400

    print("Webhook handler smoke test passed.")


if __name__ == "__main__":
    run()
