"""Send or edit the live Telegram trade card; persist message ids locally."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Union

import httpx

from trade_relay.models import CardStatus, LocalRelayState

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

TERMINAL_CARD_STATUSES = frozenset({CardStatus.CANCELED.value, CardStatus.CLOSED.value})


class TelegramError(RuntimeError):
    """Telegram API rejected a request."""


class TelegramCardSink:
    """Create one message per trade key, then edit that same message.

    After a card is published as canceled or closed, the next new trade on
    the same key starts a fresh Telegram message instead of editing the
    finished card. Last status is persisted so this survives a restart.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        state_file: Path,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._state_file = state_file
        self._client = client
        self._owns_client = client is None
        self._state = self._load_state()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def message_id_for(self, key: str) -> Optional[int]:
        return self._state.message_ids.get(key)

    def last_status_for(self, key: str) -> Optional[str]:
        return self._state.last_statuses.get(key)

    async def publish(
        self,
        key: str,
        html: str,
        status: Union[CardStatus, str, None] = None,
    ) -> int:
        """Edit the existing card for ``key``, or send a new message."""
        status_value = _status_value(status)
        if _should_open_new_message(self._state.last_statuses.get(key), status_value):
            self._state.message_ids.pop(key, None)
        existing = self._state.message_ids.get(key)
        if existing is not None:
            try:
                await self._edit(existing, html)
                self._remember(key, existing, status_value)
                return existing
            except TelegramError as exc:
                if _is_not_modified(exc):
                    self._remember(key, existing, status_value)
                    return existing
                logger.warning("editMessageText failed for %s (id=%s): %s; sending a new message", key, existing, exc)
        message_id = await self._send(html)
        self._remember(key, message_id, status_value)
        return message_id

    def _remember(self, key: str, message_id: int, status: Optional[str]) -> None:
        self._state.message_ids[key] = message_id
        if status:
            self._state.last_statuses[key] = status
        self._save_state()

    async def _client_obj(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def _send(self, html: str) -> int:
        data = await self._call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        message_id = data.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramError(f"sendMessage returned no message_id: {data}")
        return message_id

    async def _edit(self, message_id: int, html: str) -> None:
        await self._call(
            "editMessageText",
            {
                "chat_id": self._chat_id,
                "message_id": message_id,
                "text": html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._client_obj()
        url = f"{TELEGRAM_API}/bot{self._token}/{method}"
        response = await client.post(url, json=payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON ({response.status_code})") from exc
        if not body.get("ok"):
            description = body.get("description") or response.text
            raise TelegramError(f"{method} failed: {description}")
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    def _load_state(self) -> LocalRelayState:
        path = self._state_file
        if not path.exists():
            return LocalRelayState()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read state file %s: %s", path, exc)
            return LocalRelayState()
        ids = raw.get("message_ids") or raw.get("cards") or {}
        parsed: dict[str, int] = {}
        statuses: dict[str, str] = {}
        if isinstance(ids, dict):
            for key, value in ids.items():
                if isinstance(value, dict):
                    mid = value.get("message_id")
                    st = value.get("status")
                    if isinstance(mid, int):
                        parsed[str(key)] = mid
                    if isinstance(st, str) and st:
                        statuses[str(key)] = st
                elif isinstance(value, int):
                    parsed[str(key)] = value
        raw_statuses = raw.get("last_statuses") or {}
        if isinstance(raw_statuses, dict):
            for key, value in raw_statuses.items():
                if isinstance(value, str) and value:
                    statuses[str(key)] = value
        return LocalRelayState(message_ids=parsed, last_statuses=statuses)

    def _save_state(self) -> None:
        path = self._state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "message_ids": self._state.message_ids,
            "last_statuses": self._state.last_statuses,
        }
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)


def _status_value(status: Union[CardStatus, str, None]) -> Optional[str]:
    if status is None:
        return None
    if isinstance(status, CardStatus):
        return status.value
    text = str(status).strip()
    return text or None


def _should_open_new_message(last_status: Optional[str], new_status: Optional[str]) -> bool:
    if new_status is None or new_status in TERMINAL_CARD_STATUSES:
        return False
    return last_status in TERMINAL_CARD_STATUSES


def _is_not_modified(exc: TelegramError) -> bool:
    text = str(exc).lower()
    return "message is not modified" in text
