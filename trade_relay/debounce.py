"""Coalesce rapid entry-price amendments into a single delayed update."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")

Callback = Callable[[str, T], Awaitable[None] | None]


class KeyDebouncer(Generic[T]):
    """Debounce updates per key.

    Each ``schedule`` call resets the timer for that key. When the delay
    elapses with no further calls, the *latest* payload is delivered once.
    ``cancel`` drops a pending update; ``flush`` delivers it immediately.
    """

    def __init__(self, delay_seconds: float) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")
        self.delay_seconds = delay_seconds
        self._pending: dict[str, tuple[T, Callback[T]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def has_pending(self, key: str) -> bool:
        return key in self._pending

    def pending_keys(self) -> frozenset[str]:
        return frozenset(self._pending)

    def schedule(self, key: str, payload: T, callback: Callback[T]) -> None:
        self._pending[key] = (payload, callback)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()
        self._tasks[key] = asyncio.create_task(
            self._wait_and_fire(key),
            name=f"debounce:{key}",
        )

    def cancel(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        self._pending.pop(key, None)

    async def flush(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        await self._deliver(key)

    async def flush_all(self) -> None:
        keys = list(self._pending)
        for key in keys:
            await self.flush(key)

    async def _wait_and_fire(self, key: str) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            return
        self._tasks.pop(key, None)
        await self._deliver(key)

    async def _deliver(self, key: str) -> None:
        item = self._pending.pop(key, None)
        if item is None:
            return
        payload, callback = item
        result = callback(key, payload)
        if asyncio.iscoroutine(result):
            await result
