"""Account book state transitions from official-shaped events."""

from __future__ import annotations

from decimal import Decimal

from trade_relay.binance_watch import apply_user_event
from trade_relay.book import AccountBook
from trade_relay.models import (
    CardStatus,
    ChangeReason,
    Position,
    WorkingOrder,
    card_key,
    derive_trade_side,
)


def _entry(price: str, *, order_id: int = 1, status: str = "NEW") -> WorkingOrder:
    return WorkingOrder(
        order_id=order_id,
        symbol="BTCUSDT",
        side="BUY",
        position_side="BOTH",
        order_type="LIMIT",
        price=Decimal(price),
        quantity=Decimal("0.01"),
        status=status,
    )


def _position(amount: str, entry: str = "67250") -> Position:
    amt = Decimal(amount)
    return Position(
        symbol="BTCUSDT",
        position_side="BOTH",
        amount=amt,
        entry_price=Decimal(entry),
        notional=abs(amt) * Decimal(entry),
    )


def _sl(price: str, *, order_id: int = 10) -> WorkingOrder:
    return WorkingOrder(
        order_id=order_id,
        symbol="BTCUSDT",
        side="SELL",
        position_side="BOTH",
        order_type="STOP_MARKET",
        price=Decimal("0"),
        quantity=Decimal("0.01"),
        trigger_price=Decimal(price),
        stop_price=Decimal(price),
        reduce_only=True,
        is_algo=True,
        status="NEW",
    )


def _tp(price: str, *, order_id: int = 11) -> WorkingOrder:
    return WorkingOrder(
        order_id=order_id,
        symbol="BTCUSDT",
        side="SELL",
        position_side="BOTH",
        order_type="TAKE_PROFIT",
        price=Decimal(price),
        quantity=Decimal("0.01"),
        trigger_price=Decimal(price),
        reduce_only=True,
        is_algo=True,
        status="NEW",
    )


def test_derive_trade_side_one_way_and_hedge() -> None:
    assert derive_trade_side(Decimal("0.01"), "BOTH", "BUY") == "LONG"
    assert derive_trade_side(Decimal("-0.01"), "BOTH", "SELL") == "SHORT"
    assert derive_trade_side(None, "BOTH", "SELL") == "SHORT"
    assert derive_trade_side(None, "LONG", "SELL") == "LONG"
    assert derive_trade_side(None, "SHORT", "BUY") == "SHORT"


def test_new_limit_entry_is_planned() -> None:
    book = AccountBook()
    changes = book.apply_snapshot([_entry("67250")], [])
    assert len(changes) == 1
    card = changes[0].card
    assert card.status == CardStatus.PLANNED
    assert card.side == "LONG"
    assert card.planned_entry == Decimal("67250")
    assert card.filled_entry is None
    assert changes[0].reason == ChangeReason.SNAPSHOT


def test_entry_price_amendment_sets_amended() -> None:
    book = AccountBook()
    book.apply_snapshot([_entry("67250")], [])
    changes = book.apply_order_update(
        _entry("67100"),
        execution_type="AMENDMENT",
        event_time=100,
    )
    assert changes[0].reason == ChangeReason.ENTRY_AMENDED
    assert changes[0].card.status == CardStatus.AMENDED
    assert changes[0].card.planned_entry == Decimal("67100")


def test_fill_and_position_become_filled() -> None:
    book = AccountBook()
    book.apply_snapshot([_entry("67250")], [])
    filled = _entry("67250", status="FILLED")
    filled.filled_qty = Decimal("0.01")
    filled.average_price = Decimal("67248")
    changes = book.apply_order_update(filled, execution_type="TRADE", event_time=200)
    assert changes[0].reason == ChangeReason.FILL
    assert changes[0].card.status == CardStatus.FILLED
    assert changes[0].card.filled_entry == Decimal("67248")
    book.apply_position(_position("0.01", "67248"), event_time=201)
    card = book.get_card(card_key("BTCUSDT", "BOTH"))
    assert card is not None
    assert card.status == CardStatus.FILLED
    assert card.filled_entry == Decimal("67248")
    assert card.quantity == Decimal("0.01")


def test_reduce_then_close() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.02", "67250")])
    open_card = book.get_card("BTCUSDT:BOTH")
    assert open_card is not None
    assert open_card.status == CardStatus.FILLED

    reduced = book.apply_position(_position("0.01", "67250"), event_time=300)
    assert reduced[0].card.status == CardStatus.REDUCED
    assert reduced[0].card.quantity == Decimal("0.01")

    closed = book.apply_position(_position("0", "67250"), event_time=400)
    assert closed[0].reason == ChangeReason.CLOSED
    assert closed[0].card.status == CardStatus.CLOSED


def test_cancel_planned_entry_closes_card() -> None:
    book = AccountBook()
    book.apply_snapshot([_entry("67250")], [])
    canceled = _entry("67250", status="CANCELED")
    changes = book.apply_order_update(canceled, execution_type="CANCELED")
    assert changes[0].reason == ChangeReason.ENTRY_CANCELED
    assert changes[0].card.status == CardStatus.CLOSED
    assert changes[0].card.planned_entry is None


def test_tp_sl_attach_to_open_position() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.01", "67250")])
    book.apply_algo_update(_sl("66500"), event_time=500)
    changes = book.apply_algo_update(_tp("69000"), event_time=501)
    assert changes[0].reason == ChangeReason.TP_SL
    card = changes[0].card
    assert card.status == CardStatus.FILLED
    assert card.stop_loss == Decimal("66500")
    assert card.take_profit == Decimal("69000")


def test_hedge_mode_keys_are_independent() -> None:
    book = AccountBook()
    long_pos = Position("ETHUSDT", "LONG", Decimal("1"), Decimal("3000"))
    short_entry = WorkingOrder(
        order_id=2,
        symbol="ETHUSDT",
        side="SELL",
        position_side="SHORT",
        order_type="LIMIT",
        price=Decimal("3100"),
        quantity=Decimal("1"),
    )
    changes = book.apply_snapshot([short_entry], [long_pos])
    keys = {c.key for c in changes}
    assert keys == {"ETHUSDT:LONG", "ETHUSDT:SHORT"}
    assert book.get_card("ETHUSDT:LONG").status == CardStatus.FILLED
    assert book.get_card("ETHUSDT:SHORT").status == CardStatus.PLANNED
    assert book.get_card("ETHUSDT:LONG").side == "LONG"
    assert book.get_card("ETHUSDT:SHORT").side == "SHORT"


# Official-shaped user-data examples (field names from Binance docs, not live data).

ORDER_TRADE_NEW = {
    "e": "ORDER_TRADE_UPDATE",
    "E": 1568879465651,
    "T": 1568879465650,
    "o": {
        "s": "BTCUSDT",
        "c": "demo-client",
        "S": "BUY",
        "o": "LIMIT",
        "f": "GTC",
        "q": "0.001",
        "p": "67000",
        "ap": "0",
        "sp": "0",
        "x": "NEW",
        "X": "NEW",
        "i": 8886774,
        "l": "0",
        "z": "0",
        "L": "0",
        "R": False,
        "ps": "BOTH",
        "cp": False,
    },
}

ORDER_TRADE_AMEND = {
    "e": "ORDER_TRADE_UPDATE",
    "E": 1568879465700,
    "T": 1568879465699,
    "o": {
        **ORDER_TRADE_NEW["o"],
        "p": "66800",
        "x": "AMENDMENT",
        "X": "NEW",
    },
}

ACCOUNT_UPDATE_OPEN = {
    "e": "ACCOUNT_UPDATE",
    "E": 1564745798939,
    "T": 1564745798938,
    "a": {
        "m": "ORDER",
        "B": [{"a": "USDT", "wb": "100", "cw": "100", "bc": "0"}],
        "P": [
            {
                "s": "BTCUSDT",
                "pa": "0.001",
                "ep": "66800",
                "cr": "0",
                "up": "0",
                "mt": "isolated",
                "iw": "0",
                "ps": "BOTH",
            }
        ],
    },
}

ALGO_UPDATE_SL = {
    "e": "ALGO_UPDATE",
    "T": 1750515742297,
    "E": 1750515742303,
    "o": {
        "caid": "demo-sl",
        "aid": 2148719,
        "at": "CONDITIONAL",
        "o": "STOP_MARKET",
        "s": "BTCUSDT",
        "S": "SELL",
        "ps": "BOTH",
        "f": "GTC",
        "q": "0.001",
        "X": "NEW",
        "tp": "66000",
        "p": "0",
        "R": True,
        "cp": False,
    },
}


def test_apply_official_order_trade_and_account_events() -> None:
    book = AccountBook()
    planned = apply_user_event(book, ORDER_TRADE_NEW)
    assert planned[0].card.status == CardStatus.PLANNED
    assert planned[0].card.planned_entry == Decimal("67000")

    amended = apply_user_event(book, ORDER_TRADE_AMEND)
    assert amended[0].reason == ChangeReason.ENTRY_AMENDED
    assert amended[0].card.planned_entry == Decimal("66800")

    opened = apply_user_event(book, ACCOUNT_UPDATE_OPEN)
    assert opened[0].card.status == CardStatus.FILLED
    assert opened[0].card.filled_entry == Decimal("66800")

    sl = apply_user_event(book, ALGO_UPDATE_SL)
    assert sl[0].reason == ChangeReason.TP_SL
    assert sl[0].card.stop_loss == Decimal("66000")
