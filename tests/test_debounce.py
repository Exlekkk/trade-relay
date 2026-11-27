"""Debouncer coalesces rapid updates and can be flushed or canceled."""

from __future__ import annotations

import asyncio

from trade_relay.debounce import KeyDebouncer


def test_rapid_updates_fire_once_with_latest_payload() -> None:
    asyncio.run(_rapid_updates())


async def _rapid_updates() -> None:
    seen: list[tuple[str, int]] = []

    async def callback(key: str, payload: int) -> None:
        seen.append((key, payload))

    debouncer: KeyDebouncer[int] = KeyDebouncer(0.05)
    debouncer.schedule("BTCUSDT:BOTH", 1, callback)
    debouncer.schedule("BTCUSDT:BOTH", 2, callback)
    debouncer.schedule("BTCUSDT:BOTH", 3, callback)
    await asyncio.sleep(0.12)
    assert seen == [("BTCUSDT:BOTH", 3)]


def test_keys_are_independent() -> None:
    asyncio.run(_keys_independent())


async def _keys_independent() -> None:
    seen: list[tuple[str, str]] = []

    async def callback(key: str, payload: str) -> None:
        seen.append((key, payload))

    debouncer: KeyDebouncer[str] = KeyDebouncer(0.04)
    debouncer.schedule("A", "one", callback)
    debouncer.schedule("B", "two", callback)
    await asyncio.sleep(0.1)
    assert set(seen) == {("A", "one"), ("B", "two")}


def test_cancel_drops_pending_update() -> None:
    asyncio.run(_cancel())


async def _cancel() -> None:
    seen: list[int] = []

    async def callback(key: str, payload: int) -> None:
        seen.append(payload)

    debouncer: KeyDebouncer[int] = KeyDebouncer(0.05)
    debouncer.schedule("k", 1, callback)
    assert debouncer.has_pending("k")
    debouncer.cancel("k")
    await asyncio.sleep(0.08)
    assert seen == []
    assert not debouncer.has_pending("k")


def test_flush_delivers_immediately() -> None:
    asyncio.run(_flush())


async def _flush() -> None:
    seen: list[int] = []

    async def callback(key: str, payload: int) -> None:
        seen.append(payload)

    debouncer: KeyDebouncer[int] = KeyDebouncer(10)
    debouncer.schedule("k", 42, callback)
    await debouncer.flush("k")
    assert seen == [42]
    assert not debouncer.has_pending("k")
