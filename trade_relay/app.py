"""Wire config, user-data watch, debounce, and Telegram card updates."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from trade_relay.binance_watch import BinanceReadClient, UserDataWatch
from trade_relay.book import AccountBook, CardChange
from trade_relay.card import render_card
from trade_relay.config import Config, ConfigError, load_config
from trade_relay.debounce import KeyDebouncer
from trade_relay.models import ChangeReason, TradeCardState
from trade_relay.telegram_out import TelegramCardSink

logger = logging.getLogger(__name__)

# Entry-price amendments are the only reason that waits; everything else
# flushes a pending debounce and publishes immediately.
IMMEDIATE_REASONS = frozenset(
    {
        ChangeReason.SNAPSHOT,
        ChangeReason.ENTRY_NEW,
        ChangeReason.ENTRY_CANCELED,
        ChangeReason.FILL,
        ChangeReason.POSITION,
        ChangeReason.TP_SL,
        ChangeReason.CLOSED,
    }
)


class TradeRelayApp:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.book = AccountBook()
        self.debouncer: KeyDebouncer[TradeCardState] = KeyDebouncer(config.debounce_seconds)
        self.client = BinanceReadClient(
            config.binance_api_key,
            config.binance_api_secret,
            fapi_base=config.fapi_base,
        )
        self.telegram = TelegramCardSink(
            config.telegram_bot_token,
            config.telegram_chat_id,
            config.state_file,
        )
        self.watch = UserDataWatch(
            self.client,
            self.book,
            ws_base=config.ws_base,
            on_changes=self.handle_changes,
            on_snapshot=self.handle_snapshot,
        )

    async def run(self) -> None:
        if not self.config.debounce_in_recommended_range:
            logger.warning(
                "DEBOUNCE_SECONDS=%s is outside the recommended 15-30s range",
                self.config.debounce_seconds,
            )
        try:
            await self.watch.run()
        finally:
            await self.debouncer.flush_all()
            await self.telegram.aclose()
            await self.client.aclose()

    async def handle_snapshot(self, changes: list[CardChange]) -> None:
        for change in changes:
            await self._publish(change.card)

    async def handle_changes(self, changes: list[CardChange]) -> None:
        for change in changes:
            await self._route(change)

    async def _route(self, change: CardChange) -> None:
        if change.reason == ChangeReason.ENTRY_AMENDED:
            self.debouncer.schedule(change.key, change.card, self._publish_debounced)
            logger.info("Debouncing entry amend for %s", change.key)
            return
        if change.reason in IMMEDIATE_REASONS or self.debouncer.has_pending(change.key):
            if self.debouncer.has_pending(change.key):
                self.debouncer.cancel(change.key)
            await self._publish(change.card)
            return
        await self._publish(change.card)

    async def _publish_debounced(self, key: str, card: TradeCardState) -> None:
        # Prefer the latest book state in case more events landed mid-flight.
        latest = self.book.get_card(key) or card
        await self._publish(latest)

    async def _publish(self, card: TradeCardState) -> None:
        html = render_card(
            card,
            show_size=self.config.show_size,
            show_notional=self.config.show_notional,
            display_tz=self.config.display_tz,
        )
        try:
            message_id = await self.telegram.publish(card.key, html)
            logger.info(
                "Updated card %s status=%s message_id=%s",
                card.key,
                card.status.value,
                message_id,
            )
        except Exception:
            logger.exception("Failed to publish card %s", card.key)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Broadcast Binance USD-M futures updates to a Telegram trade card.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional path to a .env file (default: search the current directory).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.env_file)
    except ConfigError as exc:
        print(f"trade-relay: {exc}", file=sys.stderr)
        print(
            "Copy .env.example to .env and fill in a read-only Binance API key "
            "plus TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    try:
        asyncio.run(TradeRelayApp(config).run())
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
