from hail_detector.telegram_chat_id import latest_message_chat


def test_latest_message_chat_ignores_non_message_updates() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"}}},
        {"update_id": 2, "callback_query": {}},
    ]

    assert latest_message_chat(updates) == {"id": 123, "type": "private"}


def test_latest_message_chat_returns_none_without_messages() -> None:
    assert latest_message_chat([{"update_id": 1}]) is None
