"""Send or edit the live Telegram trade card; persist message ids locally."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from trade_relay.models import LocalRelayState

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Telegram API rejected a request."""


class TelegramCardSink:
    """Create one message per trade key, then edit that same message."""

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

    async def publish(self, key: str, html: str) -> int:
        """Edit the existing card for ``key``, or send a new message."""
        existing = self._state.message_ids.get(key)
        if existing is not None:
            try:
                await self._edit(existing, html)
                return existing
            except TelegramError as exc:
                if _is_not_modified(exc):
                    return existing
                logger.warning("editMessageText failed for %s (id=%s): %s; sending a new message", key, existing, exc)
        message_id = await self._send(html)
        self._state.message_ids[key] = message_id
        self._save_state()
        return message_id

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
        if isinstance(ids, dict):
            for key, value in ids.items():
                if isinstance(value, dict) and "message_id" in value:
                    value = value["message_id"]
                if isinstance(value, int):
                    parsed[str(key)] = value
        return LocalRelayState(message_ids=parsed)

    def _save_state(self) -> None:
        path = self._state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"message_ids": self._state.message_ids}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)


def _is_not_modified(exc: TelegramError) -> bool:
    text = str(exc).lower()
    return "message is not modified" in text
