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


def _close_limit(price: str, *, order_id: int = 20, status: str = "NEW") -> WorkingOrder:
    return WorkingOrder(
        order_id=order_id,
        symbol="BTCUSDT",
        side="SELL",
        position_side="BOTH",
        order_type="LIMIT",
        price=Decimal(price),
        quantity=Decimal("0.01"),
        reduce_only=True,
        close_position=False,
        status=status,
    )


def test_derive_trade_side_one_way_and_hedge() -> None:
    assert derive_trade_side(Decimal("0.01"), "BOTH", "BUY") == "LONG"
    assert derive_trade_side(Decimal("-0.01"), "BOTH", "SELL") == "SHORT"
    assert derive_trade_side(None, "BOTH", "SELL") == "SHORT"
    assert derive_trade_side(None, "LONG", "SELL") == "LONG"
    assert derive_trade_side(None, "SHORT", "BUY") == "SHORT"


def test_new_limit_entry_is_planned() -> None:
    book = AccountBook()
    changes = book.apply_snapshot([_entry("61696.1")], [])
    assert len(changes) == 1
    card = changes[0].card
    assert card.status == CardStatus.PLANNED
    assert card.side == "LONG"
    assert card.planned_entry == Decimal("61696.1")
    assert card.filled_entry is None
    assert card.previous_planned_entry is None
    assert changes[0].reason == ChangeReason.SNAPSHOT


def test_entry_price_amendment_sets_amended_and_keeps_previous() -> None:
    book = AccountBook()
    book.apply_snapshot([_entry("61696.1")], [])
    changes = book.apply_order_update(
        _entry("61500"),
        execution_type="AMENDMENT",
        event_time=100,
    )
    assert changes[0].reason == ChangeReason.ENTRY_AMENDED
    assert changes[0].card.status == CardStatus.AMENDED
    assert changes[0].card.planned_entry == Decimal("61500")
    assert changes[0].card.previous_planned_entry == Decimal("61696.1")

    # Rapid second tweak keeps the original 改前, not the intermediate price.
    again = book.apply_order_update(
        _entry("61400"),
        execution_type="AMENDMENT",
        event_time=101,
    )
    assert again[0].card.status == CardStatus.AMENDED
    assert again[0].card.planned_entry == Decimal("61400")
    assert again[0].card.previous_planned_entry == Decimal("61696.1")


def test_fill_and_position_become_filled() -> None:
    book = AccountBook()
    book.apply_snapshot([_entry("61696.1")], [])
    filled = _entry("61696.1", status="FILLED")
    filled.filled_qty = Decimal("0.01")
    filled.average_price = Decimal("61696.1")
    changes = book.apply_order_update(filled, execution_type="TRADE", event_time=200)
    assert changes[0].reason == ChangeReason.FILL
    assert changes[0].card.status == CardStatus.FILLED
    assert changes[0].card.filled_entry == Decimal("61696.1")
    book.apply_position(_position("0.01", "61696.1"), event_time=201)
    card = book.get_card(card_key("BTCUSDT", "BOTH"))
    assert card is not None
    assert card.status == CardStatus.FILLED
    assert card.filled_entry == Decimal("61696.1")
    assert card.quantity == Decimal("0.01")
    assert card.working_close is None


def test_reduce_only_limit_is_close_working() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.01", "61696.1")])
    changes = book.apply_order_update(_close_limit("62367.7"), execution_type="NEW", event_time=300)
    assert changes[0].reason == ChangeReason.CLOSE_NEW
    card = changes[0].card
    assert card.status == CardStatus.CLOSE_WORKING
    assert card.filled_entry == Decimal("61696.1")
    assert card.working_close == Decimal("62367.7")
    assert card.stop_loss is None
    assert card.take_profit == Decimal("62367.7")


def test_close_limit_amendment_sets_amended_and_keeps_previous() -> None:
    book = AccountBook()
    book.apply_snapshot([_close_limit("62367.7")], [_position("0.01", "61696.1")])
    card = book.get_card("BTCUSDT:BOTH")
    assert card is not None
    assert card.status == CardStatus.CLOSE_WORKING
    changes = book.apply_order_update(
        _close_limit("62500"),
        execution_type="AMENDMENT",
        event_time=301,
    )
    assert changes[0].reason == ChangeReason.CLOSE_AMENDED
    assert changes[0].card.status == CardStatus.CLOSE_AMENDED
    assert changes[0].card.working_close == Decimal("62500")
    assert changes[0].card.previous_close == Decimal("62367.7")
    assert changes[0].card.filled_entry == Decimal("61696.1")


def test_limit_close_fill_is_closed_with_limit_kind() -> None:
    book = AccountBook()
    book.apply_snapshot([_close_limit("62367.7")], [_position("0.01", "61696.1")])
    filled = _close_limit("62367.7", status="FILLED")
    filled.filled_qty = Decimal("0.01")
    filled.average_price = Decimal("62367.7")
    changes = book.apply_order_update(filled, execution_type="TRADE", event_time=400)
    assert changes[0].reason == ChangeReason.CLOSE_FILL
    assert changes[0].card.status == CardStatus.CLOSED
    assert changes[0].card.filled_entry == Decimal("61696.1")
    assert changes[0].card.filled_close == Decimal("62367.7")
    assert changes[0].card.close_kind == "limit"
    assert changes[0].card.working_close is None

    # Later ACCOUNT_UPDATE keeps the close price; does not invent a new one.
    closed = book.apply_position(_position("0", "61696.1"), event_time=401)
    assert closed[0].reason == ChangeReason.CLOSED
    assert closed[0].card.status == CardStatus.CLOSED
    assert closed[0].card.filled_close == Decimal("62367.7")
    assert closed[0].card.close_kind == "limit"


def test_market_close_is_closed_with_market_kind() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.01", "61696.1")])
    market = WorkingOrder(
        order_id=30,
        symbol="BTCUSDT",
        side="SELL",
        position_side="BOTH",
        order_type="MARKET",
        price=Decimal("0"),
        quantity=Decimal("0.01"),
        filled_qty=Decimal("0.01"),
        average_price=Decimal("61280"),
        reduce_only=True,
        close_position=True,
        status="FILLED",
        last_fill_price=Decimal("61280"),
    )
    changes = book.apply_order_update(market, execution_type="TRADE", event_time=500)
    assert changes[0].reason == ChangeReason.CLOSE_FILL
    assert changes[0].card.status == CardStatus.CLOSED
    assert changes[0].card.filled_close == Decimal("61280")
    assert changes[0].card.close_kind == "market"
    assert changes[0].card.filled_entry == Decimal("61696.1")


def test_position_close_without_fill_omits_close_price() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.01", "61696.1")])
    closed = book.apply_position(_position("0", "61696.1"), event_time=510)
    assert closed[0].card.status == CardStatus.CLOSED
    assert closed[0].card.filled_entry == Decimal("61696.1")
    assert closed[0].card.filled_close is None
    assert closed[0].card.close_kind is None


def test_reduce_then_close() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.02", "67250")])
    open_card = book.get_card("BTCUSDT:BOTH")
    assert open_card is not None
    assert open_card.status == CardStatus.FILLED

    reduced = book.apply_position(_position("0.01", "67250"), event_time=300)
    assert reduced[0].card.status == CardStatus.REDUCED
    assert reduced[0].card.quantity == Decimal("0.01")
    assert reduced[0].card.filled_entry == Decimal("67250")

    closed = book.apply_position(_position("0", "67250"), event_time=400)
    assert closed[0].reason == ChangeReason.CLOSED
    assert closed[0].card.status == CardStatus.CLOSED
    assert closed[0].card.filled_entry == Decimal("67250")


def test_reduce_with_remaining_close_limit_stays_close_working() -> None:
    book = AccountBook()
    book.apply_snapshot([_close_limit("62367.7")], [_position("0.02", "61696.1")])
    # Partial close: position shrinks but the remainder close limit is still working.
    # Priority: 已挂平仓 > 已减仓.
    reduced = book.apply_position(_position("0.01", "61696.1"), event_time=350)
    assert reduced[0].card.status == CardStatus.CLOSE_WORKING
    assert reduced[0].card.working_close == Decimal("62367.7")
    assert reduced[0].card.filled_entry == Decimal("61696.1")


def test_cancel_planned_entry_is_canceled() -> None:
    book = AccountBook()
    book.apply_snapshot([_entry("61696.1")], [])
    canceled = _entry("61696.1", status="CANCELED")
    changes = book.apply_order_update(canceled, execution_type="CANCELED")
    assert changes[0].reason == ChangeReason.ENTRY_CANCELED
    assert changes[0].card.status == CardStatus.CANCELED
    assert changes[0].card.planned_entry == Decimal("61696.1")


def test_algo_tp_is_close_working_and_sl_is_not() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.01", "67250")])
    sl = book.apply_algo_update(_sl("66500"), event_time=500)
    assert sl[0].reason == ChangeReason.TP_SL
    assert sl[0].card.status == CardStatus.FILLED
    assert sl[0].card.stop_loss == Decimal("66500")
    assert sl[0].card.working_close is None

    changes = book.apply_algo_update(_tp("69000"), event_time=501)
    assert changes[0].reason == ChangeReason.CLOSE_NEW
    card = changes[0].card
    assert card.status == CardStatus.CLOSE_WORKING
    assert card.stop_loss == Decimal("66500")
    assert card.working_close == Decimal("69000")
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

ORDER_TRADE_CLOSE_LIMIT_NEW = {
    "e": "ORDER_TRADE_UPDATE",
    "E": 1568879465800,
    "T": 1568879465799,
    "o": {
        "s": "BTCUSDT",
        "c": "demo-close",
        "S": "SELL",
        "o": "LIMIT",
        "f": "GTC",
        "q": "0.001",
        "p": "68000",
        "ap": "0",
        "sp": "0",
        "x": "NEW",
        "X": "NEW",
        "i": 8886775,
        "l": "0",
        "z": "0",
        "L": "0",
        "R": True,
        "ps": "BOTH",
        "cp": False,
    },
}

ORDER_TRADE_CLOSE_LIMIT_AMEND = {
    "e": "ORDER_TRADE_UPDATE",
    "E": 1568879465900,
    "T": 1568879465899,
    "o": {
        **ORDER_TRADE_CLOSE_LIMIT_NEW["o"],
        "p": "68500",
        "x": "AMENDMENT",
        "X": "NEW",
    },
}

ORDER_TRADE_CLOSE_LIMIT_FILL = {
    "e": "ORDER_TRADE_UPDATE",
    "E": 1568879466000,
    "T": 1568879465999,
    "o": {
        **ORDER_TRADE_CLOSE_LIMIT_NEW["o"],
        "p": "68500",
        "ap": "68500",
        "x": "TRADE",
        "X": "FILLED",
        "l": "0.001",
        "z": "0.001",
        "L": "68500",
        "R": True,
    },
}

ORDER_TRADE_MARKET_CLOSE = {
    "e": "ORDER_TRADE_UPDATE",
    "E": 1568879466100,
    "T": 1568879466099,
    "o": {
        "s": "BTCUSDT",
        "c": "demo-mkt-close",
        "S": "SELL",
        "o": "MARKET",
        "f": "GTC",
        "q": "0.001",
        "p": "0",
        "ap": "66100",
        "sp": "0",
        "x": "TRADE",
        "X": "FILLED",
        "i": 8886776,
        "l": "0.001",
        "z": "0.001",
        "L": "66100",
        "R": True,
        "ps": "BOTH",
        "cp": True,
    },
}

ACCOUNT_UPDATE_FLAT = {
    "e": "ACCOUNT_UPDATE",
    "E": 1564745799000,
    "T": 1564745798999,
    "a": {
        "m": "ORDER",
        "B": [{"a": "USDT", "wb": "100", "cw": "100", "bc": "0"}],
        "P": [
            {
                "s": "BTCUSDT",
                "pa": "0",
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


def test_apply_official_order_trade_and_account_events() -> None:
    book = AccountBook()
    planned = apply_user_event(book, ORDER_TRADE_NEW)
    assert planned[0].card.status == CardStatus.PLANNED
    assert planned[0].card.planned_entry == Decimal("67000")

    amended = apply_user_event(book, ORDER_TRADE_AMEND)
    assert amended[0].reason == ChangeReason.ENTRY_AMENDED
    assert amended[0].card.planned_entry == Decimal("66800")
    assert amended[0].card.previous_planned_entry == Decimal("67000")

    opened = apply_user_event(book, ACCOUNT_UPDATE_OPEN)
    assert opened[0].card.status == CardStatus.FILLED
    assert opened[0].card.filled_entry == Decimal("66800")

    sl = apply_user_event(book, ALGO_UPDATE_SL)
    assert sl[0].reason == ChangeReason.TP_SL
    assert sl[0].card.stop_loss == Decimal("66000")
    assert sl[0].card.status == CardStatus.FILLED
    assert sl[0].card.working_close is None


def test_apply_official_reduce_only_limit_and_fills() -> None:
    book = AccountBook()
    apply_user_event(book, ORDER_TRADE_NEW)
    apply_user_event(book, ACCOUNT_UPDATE_OPEN)

    hanging = apply_user_event(book, ORDER_TRADE_CLOSE_LIMIT_NEW)
    assert hanging[0].reason == ChangeReason.CLOSE_NEW
    assert hanging[0].card.status == CardStatus.CLOSE_WORKING
    assert hanging[0].card.working_close == Decimal("68000")

    amended = apply_user_event(book, ORDER_TRADE_CLOSE_LIMIT_AMEND)
    assert amended[0].reason == ChangeReason.CLOSE_AMENDED
    assert amended[0].card.status == CardStatus.CLOSE_AMENDED
    assert amended[0].card.working_close == Decimal("68500")
    assert amended[0].card.previous_close == Decimal("68000")

    filled = apply_user_event(book, ORDER_TRADE_CLOSE_LIMIT_FILL)
    assert filled[0].reason == ChangeReason.CLOSE_FILL
    assert filled[0].card.status == CardStatus.CLOSED
    assert filled[0].card.filled_close == Decimal("68500")
    assert filled[0].card.close_kind == "limit"


def test_apply_official_market_close() -> None:
    book = AccountBook()
    apply_user_event(book, ORDER_TRADE_NEW)
    apply_user_event(book, ACCOUNT_UPDATE_OPEN)
    closed = apply_user_event(book, ORDER_TRADE_MARKET_CLOSE)
    assert closed[0].reason == ChangeReason.CLOSE_FILL
    assert closed[0].card.status == CardStatus.CLOSED
    assert closed[0].card.filled_close == Decimal("66100")
    assert closed[0].card.close_kind == "market"
    flat = apply_user_event(book, ACCOUNT_UPDATE_FLAT)
    assert flat[0].card.status == CardStatus.CLOSED
    assert flat[0].card.filled_close == Decimal("66100")
    assert flat[0].card.close_kind == "market"


def test_close_position_limit_is_close_exit_not_entry_or_sl() -> None:
    hanging = WorkingOrder(
        order_id=40,
        symbol="BTCUSDT",
        side="SELL",
        position_side="BOTH",
        order_type="LIMIT",
        price=Decimal("62367.7"),
        quantity=Decimal("0.01"),
        reduce_only=False,
        close_position=True,
    )
    assert hanging.is_close_exit()
    assert not hanging.is_entry()
    assert not hanging.is_stop_loss()
    assert hanging.exit_kind() == "limit"

    reduce_only_limit = _close_limit("62367.7")
    assert reduce_only_limit.is_close_exit()
    assert not reduce_only_limit.is_entry()
    assert not reduce_only_limit.is_stop_loss()

    entry = _entry("61696.1")
    assert entry.is_entry()
    assert not entry.is_close_exit()

    sl = _sl("66500")
    assert sl.is_stop_loss()
    assert not sl.is_close_exit()
    assert not sl.is_entry()

    book = AccountBook()
    book.apply_snapshot([], [_position("0.01", "61696.1")])
    changes = book.apply_order_update(hanging, execution_type="NEW")
    assert changes[0].card.status == CardStatus.CLOSE_WORKING
    assert changes[0].card.working_close == Decimal("62367.7")
    assert changes[0].card.stop_loss is None


def test_new_entry_after_close_starts_fresh_card() -> None:
    book = AccountBook()
    book.apply_snapshot([], [_position("0.01", "61696.1")])
    market = WorkingOrder(
        order_id=30,
        symbol="BTCUSDT",
        side="SELL",
        position_side="BOTH",
        order_type="MARKET",
        price=Decimal("0"),
        quantity=Decimal("0.01"),
        filled_qty=Decimal("0.01"),
        average_price=Decimal("61280"),
        reduce_only=True,
        close_position=True,
        status="FILLED",
    )
    book.apply_order_update(market, execution_type="TRADE")
    book.apply_position(_position("0", "61696.1"))
    closed = book.get_card("BTCUSDT:BOTH")
    assert closed is not None
    assert closed.status == CardStatus.CLOSED
    assert closed.filled_close == Decimal("61280")

    changes = book.apply_order_update(_entry("61000", order_id=50), execution_type="NEW")
    assert changes[0].reason == ChangeReason.ENTRY_NEW
    card = changes[0].card
    assert card.status == CardStatus.PLANNED
    assert card.planned_entry == Decimal("61000")
    assert card.filled_entry is None
    assert card.filled_close is None
    assert card.working_close is None
    assert card.close_kind is None
