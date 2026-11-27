"""Dataclasses for orders, positions, and the live trade card."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from typing import Optional


ZERO = Decimal("0")


class CardStatus(str, Enum):
    PLANNED = "planned"
    FILLED = "filled"
    AMENDED = "amended"
    REDUCED = "reduced"
    CLOSED = "closed"


class ChangeReason(str, Enum):
    SNAPSHOT = "snapshot"
    ENTRY_NEW = "entry_new"
    ENTRY_AMENDED = "entry_amended"
    ENTRY_CANCELED = "entry_canceled"
    FILL = "fill"
    POSITION = "position"
    TP_SL = "tp_sl"
    CLOSED = "closed"


# Working futures order types that are entries (not conditional TP/SL).
ENTRY_ORDER_TYPES = frozenset({"LIMIT", "MARKET", "LIMIT_MAKER"})

# After the 2025-12 algo migration these live on the algo service; they may
# still appear briefly as ordinary orders if a trigger has already fired.
PROTECTIVE_ORDER_TYPES = frozenset(
    {
        "STOP",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }
)

TP_ORDER_TYPES = frozenset({"TAKE_PROFIT", "TAKE_PROFIT_MARKET"})
SL_ORDER_TYPES = frozenset({"STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"})

# ORDER_TRADE_UPDATE ``o.X`` values that mean the order is no longer working.
TERMINAL_ORDER_STATUSES = frozenset(
    {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
)

# ALGO_UPDATE ``o.X`` values that mean the conditional order is no longer armed.
TERMINAL_ALGO_STATUSES = frozenset(
    {"CANCELED", "CANCELLED", "TRIGGERED", "FINISHED", "REJECTED", "EXPIRED"}
)


def d(value: object | None) -> Decimal:
    """Parse a Binance numeric field (string or number) into Decimal."""
    if value is None or value == "":
        return ZERO
    return Decimal(str(value))


def format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "—"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def card_key(symbol: str, position_side: str) -> str:
    return f"{symbol}:{position_side}"


def derive_trade_side(
    position_amt: Decimal | None,
    position_side: str,
    entry_side: str | None,
) -> str:
    """Map Binance positionSide / order side to a LONG or SHORT card side."""
    if position_side == "LONG":
        return "LONG"
    if position_side == "SHORT":
        return "SHORT"
    if position_amt is not None and position_amt != 0:
        return "LONG" if position_amt > 0 else "SHORT"
    if entry_side == "BUY":
        return "LONG"
    if entry_side == "SELL":
        return "SHORT"
    return "LONG"


@dataclass
class WorkingOrder:
    order_id: int
    symbol: str
    side: str
    position_side: str
    order_type: str
    price: Decimal
    quantity: Decimal
    filled_qty: Decimal = ZERO
    average_price: Decimal = ZERO
    stop_price: Decimal = ZERO
    trigger_price: Decimal = ZERO
    reduce_only: bool = False
    close_position: bool = False
    status: str = "NEW"
    client_order_id: str = ""
    is_algo: bool = False
    execution_type: str = ""

    @property
    def display_price(self) -> Decimal:
        if self.average_price > 0:
            return self.average_price
        if self.price > 0:
            return self.price
        if self.trigger_price > 0:
            return self.trigger_price
        return self.stop_price

    @property
    def protective_price(self) -> Decimal:
        """Price shown as SL/TP: trigger first, then limit price."""
        if self.trigger_price > 0:
            return self.trigger_price
        if self.stop_price > 0:
            return self.stop_price
        return self.price

    def is_protective(self) -> bool:
        return (
            self.is_algo
            or self.order_type in PROTECTIVE_ORDER_TYPES
            or self.close_position
        )

    def is_entry(self) -> bool:
        return not self.reduce_only and not self.is_protective()

    def is_take_profit(self) -> bool:
        return self.order_type in TP_ORDER_TYPES

    def is_stop_loss(self) -> bool:
        return self.order_type in SL_ORDER_TYPES


@dataclass
class Position:
    symbol: str
    position_side: str
    amount: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal = ZERO
    notional: Decimal = ZERO

    def is_open(self) -> bool:
        return self.amount != 0


@dataclass
class TradeCardState:
    key: str
    symbol: str
    position_side: str
    side: str
    status: CardStatus
    planned_entry: Optional[Decimal] = None
    filled_entry: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    updated_event_time: int = 0

    def with_status(self, status: CardStatus) -> "TradeCardState":
        return replace(self, status=status)


@dataclass
class CardChange:
    key: str
    reason: ChangeReason
    card: TradeCardState
    previous: Optional[TradeCardState] = None


@dataclass
class LocalRelayState:
    """Persisted Telegram message ids, keyed by trade card key."""

    message_ids: dict[str, int] = field(default_factory=dict)
