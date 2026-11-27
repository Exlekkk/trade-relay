"""Telegram send-or-edit and local message-id persistence (no live API)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from trade_relay.models import CardStatus
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


def _methods(store: dict[str, Any]) -> list[str]:
    return [path.rsplit("/", 1)[-1] for path, _ in store["calls"]]


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
    assert _methods(store) == ["sendMessage", "editMessageText"]
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


def test_mid_trade_edits_keep_same_message_id(tmp_path) -> None:
    asyncio.run(_mid_trade(tmp_path))


async def _mid_trade(tmp_path) -> None:
    store: dict[str, Any] = {"next_id": 100}
    transport = httpx.MockTransport(_handler_factory(store))
    client = httpx.AsyncClient(transport=transport)
    sink = TelegramCardSink("demo-token", "-100", tmp_path / "state.json", client=client)
    key = "BTCUSDT:LONG"
    first = await sink.publish(key, "<b>open</b>", status=CardStatus.PLANNED)
    amended = await sink.publish(key, "<b>amended</b>", status=CardStatus.AMENDED)
    filled = await sink.publish(key, "<b>filled</b>", status=CardStatus.FILLED)
    working = await sink.publish(key, "<b>close</b>", status=CardStatus.CLOSE_WORKING)
    closed = await sink.publish(key, "<b>flat</b>", status=CardStatus.CLOSED)
    assert first == amended == filled == working == closed == 100
    assert _methods(store) == [
        "sendMessage",
        "editMessageText",
        "editMessageText",
        "editMessageText",
        "editMessageText",
    ]
    await client.aclose()


def test_new_message_after_canceled(tmp_path) -> None:
    asyncio.run(_after_terminal(tmp_path, CardStatus.CANCELED))


def test_new_message_after_closed(tmp_path) -> None:
    asyncio.run(_after_terminal(tmp_path, CardStatus.CLOSED))


async def _after_terminal(tmp_path, terminal: CardStatus) -> None:
    store: dict[str, Any] = {"next_id": 200}
    transport = httpx.MockTransport(_handler_factory(store))
    client = httpx.AsyncClient(transport=transport)
    state_file = tmp_path / "state.json"
    sink = TelegramCardSink("demo-token", "-100", state_file, client=client)
    key = "BTCUSDT:SHORT"
    first = await sink.publish(key, "<b>open</b>", status=CardStatus.PLANNED)
    terminal_id = await sink.publish(key, "<b>done</b>", status=terminal)
    assert first == terminal_id == 200
    second = await sink.publish(key, "<b>open again</b>", status=CardStatus.PLANNED)
    assert second == 201
    assert sink.message_id_for(key) == 201
    assert sink.last_status_for(key) == CardStatus.PLANNED.value
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw["message_ids"][key] == 201
    assert raw["last_statuses"][key] == CardStatus.PLANNED.value
    assert _methods(store) == ["sendMessage", "editMessageText", "sendMessage"]
    await client.aclose()


def test_restart_after_canceled_sends_new_message(tmp_path) -> None:
    asyncio.run(_restart_after_terminal(tmp_path, CardStatus.CANCELED))


def test_restart_after_closed_sends_new_message(tmp_path) -> None:
    asyncio.run(_restart_after_terminal(tmp_path, CardStatus.CLOSED))


async def _restart_after_terminal(tmp_path, terminal: CardStatus) -> None:
    store: dict[str, Any] = {"next_id": 6607}
    transport = httpx.MockTransport(_handler_factory(store))
    client = httpx.AsyncClient(transport=transport)
    state_file = tmp_path / "state.json"
    sink = TelegramCardSink("demo-token", "-100", state_file, client=client)
    key = "BTCUSDT:SHORT"
    first = await sink.publish(key, "<b>open</b>", status=CardStatus.PLANNED)
    await sink.publish(key, "<b>done</b>", status=terminal)
    assert first == 6607
    await client.aclose()

    client2 = httpx.AsyncClient(transport=transport)
    sink2 = TelegramCardSink("demo-token", "-100", state_file, client=client2)
    assert sink2.message_id_for(key) == 6607
    assert sink2.last_status_for(key) == terminal.value
    second = await sink2.publish(key, "<b>new trade</b>", status=CardStatus.PLANNED)
    assert second == 6608
    assert sink2.message_id_for(key) == 6608
    assert _methods(store)[-1] == "sendMessage"
    await client2.aclose()
