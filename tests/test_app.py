"""Trade relay publish routing (no live Binance or Telegram calls)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from trade_relay.app import TradeRelayApp
from trade_relay.config import Config
from trade_relay.models import CardChange, CardStatus, ChangeReason, TradeCardState


class FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, CardStatus | None]] = []

    async def publish(self, key: str, html: str, status: CardStatus | None = None) -> int:
        self.calls.append((key, html, status))
        return len(self.calls)

    async def aclose(self) -> None:
        return None


def _config(tmp_path: Path) -> Config:
    return Config(
        binance_api_key="demo-key",
        binance_api_secret="demo-secret",
        telegram_bot_token="demo-token",
        telegram_chat_id="-100",
        debounce_seconds=20,
        state_file=tmp_path / "state.json",
        show_size=False,
        show_notional=False,
    )


def _card(*, status: CardStatus, reason_price: str = "61696.1") -> TradeCardState:
    return TradeCardState(
        key="BTCUSDT:SHORT",
        symbol="BTCUSDT",
        position_side="SHORT",
        side="SHORT",
        status=status,
        planned_entry=Decimal(reason_price),
    )


def test_entry_amend_publishes_immediately(tmp_path: Path) -> None:
    asyncio.run(_amend_immediate(tmp_path, ChangeReason.ENTRY_AMENDED, CardStatus.AMENDED))


def test_close_amend_publishes_immediately(tmp_path: Path) -> None:
    asyncio.run(_amend_immediate(tmp_path, ChangeReason.CLOSE_AMENDED, CardStatus.CLOSE_AMENDED))


async def _amend_immediate(tmp_path: Path, reason: ChangeReason, status: CardStatus) -> None:
    app = TradeRelayApp(_config(tmp_path))
    sink = FakeSink()
    app.telegram = sink
    card = _card(status=status)
    change = CardChange(key=card.key, reason=reason, card=card)
    await app._route(change)
    assert len(sink.calls) == 1
    assert sink.calls[0][0] == card.key
    assert sink.calls[0][2] == status
    await app.client.aclose()
