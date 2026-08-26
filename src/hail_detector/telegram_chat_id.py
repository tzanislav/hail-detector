from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


TELEGRAM_API_URL = "https://api.telegram.org"


def latest_message_chat(updates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for update in reversed(updates):
        message = update.get("message")
        if isinstance(message, dict) and isinstance(message.get("chat"), dict):
            return message["chat"]
    return None


def _get_json(client: httpx.Client, endpoint: str) -> dict[str, Any]:
    try:
        response = client.get(endpoint)
    except httpx.HTTPError as error:
        raise SystemExit("Telegram network request failed.") from error

    try:
        payload = response.json()
    except ValueError as error:
        raise SystemExit("Telegram returned an invalid response.") from error
    if response.status_code < 200 or response.status_code >= 300 or payload.get("ok") is not True:
        description = payload.get("description")
        detail = f": {description}" if isinstance(description, str) else ""
        raise SystemExit(f"Telegram rejected the request{detail}")
    return payload


def main() -> None:
    environment_file = Path(os.getenv("HAIL_ENV_FILE", ".env"))
    load_dotenv(environment_file, override=False)
    token = os.getenv("HAIL_TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("HAIL_TELEGRAM_BOT_TOKEN is missing from .env.")

    base_url = f"{TELEGRAM_API_URL}/bot{token}"
    with httpx.Client(timeout=10.0) as client:
        identity = _get_json(client, f"{base_url}/getMe")["result"]
        updates = _get_json(client, f"{base_url}/getUpdates?offset=-1")["result"]

    chat = latest_message_chat(updates)
    username = identity.get("username", "unknown")
    if chat is None:
        raise SystemExit(
            f"No message is waiting for @{username}. Open that bot, send it a "
            "new message, and run this command again."
        )

    chat_id = str(chat["id"])
    if chat_id == str(identity["id"]):
        raise SystemExit("The latest update points to the bot itself, not your chat.")
    print(f"Bot: @{username}")
    print(f"Chat type: {chat.get('type', 'unknown')}")
    print(f"HAIL_TELEGRAM_CHAT_ID={chat_id}")


if __name__ == "__main__":
    main()
