"""Telegram send-or-edit and local message-id persistence (no live API)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from trade_relay.telegram_out import TelegramCardSink


def _handler_factory(store: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        store.setdefault("calls", []).append((path, request.read().decode("utf-8")))
        if path.endswith("/sendMessage"):
            store["next_id"] = store.get("next_id", 100) + 0
            message_id = store.get("next_id", 100)
            store["next_id"] = message_id + 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
        if path.endswith("/editMessageText"):
            if store.get("fail_edit"):
                return httpx.Response(
                    200,
                    json={"ok": False, "description": "Bad Request: message to edit not found"},
                )
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 100}})
        return httpx.Response(404, json={"ok": False, "description": "unknown"})

    return handler


def test_first_publish_sends_then_edits_and_persists(tmp_path) -> None:
    asyncio.run(_first_publish(tmp_path))


async def _first_publish(tmp_path) -> None:
    store: dict[str, Any] = {"next_id": 42}
    transport = httpx.MockTransport(_handler_factory(store))
    client = httpx.AsyncClient(transport=transport)
    state_file = tmp_path / "state.json"
    sink = TelegramCardSink("demo-token", "-100", state_file, client=client)
    first = await sink.publish("BTCUSDT:BOTH", "<b>one</b>")
    assert first == 42
    assert sink.message_id_for("BTCUSDT:BOTH") == 42
    assert state_file.exists()

    # New sink instance should reuse the persisted id and edit, not send.
    client2 = httpx.AsyncClient(transport=transport)
    sink2 = TelegramCardSink("demo-token", "-100", state_file, client=client2)
    second = await sink2.publish("BTCUSDT:BOTH", "<b>two</b>")
    assert second == 42
    methods = [path.rsplit("/", 1)[-1] for path, _ in store["calls"]]
    assert methods == ["sendMessage", "editMessageText"]
    await client.aclose()
    await client2.aclose()


def test_missing_message_falls_back_to_send(tmp_path) -> None:
    asyncio.run(_fallback(tmp_path))


async def _fallback(tmp_path) -> None:
    store: dict[str, Any] = {"next_id": 7, "fail_edit": True}
    transport = httpx.MockTransport(_handler_factory(store))
    client = httpx.AsyncClient(transport=transport)
    state_file = tmp_path / "state.json"
    state_file.write_text('{"message_ids": {"BTCUSDT:BOTH": 1}}\n', encoding="utf-8")
    sink = TelegramCardSink("demo-token", "-100", state_file, client=client)
    message_id = await sink.publish("BTCUSDT:BOTH", "<b>new</b>")
    assert message_id == 7
    await client.aclose()
