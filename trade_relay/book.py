"""In-memory account book: snapshots + stream events -> trade cards.

State transitions (per symbol + position side):

* working entry, no position -> planned (or amended after an entry-price change)
* position opened / entry filled -> filled
* position size decreased but still open -> reduced
* position back to flat -> closed
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

from trade_relay.models import (
    TERMINAL_ALGO_STATUSES,
    TERMINAL_ORDER_STATUSES,
    ZERO,
    CardChange,
    CardStatus,
    ChangeReason,
    Position,
    TradeCardState,
    WorkingOrder,
    card_key,
    derive_trade_side,
)


def _order_store_key(order: WorkingOrder) -> tuple[str, int, bool]:
    return (order.symbol, order.order_id, order.is_algo)


def _belongs(order: WorkingOrder, symbol: str, position_side: str) -> bool:
    return order.symbol == symbol and order.position_side == position_side


class AccountBook:
    """Applies official USD-M snapshots and user-data events to card state."""

    def __init__(self) -> None:
        self.orders: dict[tuple[str, int, bool], WorkingOrder] = {}
        self.positions: dict[tuple[str, str], Position] = {}
        self._cards: dict[str, TradeCardState] = {}
        self._entry_fills: dict[str, WorkingOrder] = {}
        self._last_event_time: int = 0

    def known_keys(self) -> list[str]:
        return list(self._cards)

    def get_card(self, key: str) -> Optional[TradeCardState]:
        return self._cards.get(key)

    def apply_snapshot(
        self,
        orders: Iterable[WorkingOrder],
        positions: Iterable[Position],
        event_time: int = 0,
    ) -> list[CardChange]:
        self.orders = {_order_store_key(o): o for o in orders}
        self.positions = {}
        self._entry_fills = {}
        for pos in positions:
            if pos.amount != 0:
                self.positions[(pos.symbol, pos.position_side)] = pos
        keys = self._all_keys()
        changes: list[CardChange] = []
        for key in keys:
            card = self._build_card(key, event_time=event_time)
            if card is None:
                continue
            previous = self._cards.get(key)
            self._cards[key] = card
            changes.append(CardChange(key=key, reason=ChangeReason.SNAPSHOT, card=card, previous=previous))
        return changes

    def apply_order_update(
        self,
        order: WorkingOrder,
        *,
        execution_type: str,
        event_time: int = 0,
    ) -> list[CardChange]:
        store_key = _order_store_key(order)
        previous_order = self.orders.get(store_key)
        key = card_key(order.symbol, order.position_side)
        terminal = order.status in TERMINAL_ORDER_STATUSES
        if (execution_type.upper() == "TRADE" or order.filled_qty > ZERO) and order.is_entry():
            # Remember the fill so the card can show "filled" before ACCOUNT_UPDATE.
            self._entry_fills[key] = order
        if terminal:
            self.orders.pop(store_key, None)
        else:
            self.orders[store_key] = order

        reason = self._reason_for_order(order, execution_type, previous_order)
        return self._publish(key, reason, event_time)

    def apply_algo_update(
        self,
        order: WorkingOrder,
        *,
        event_time: int = 0,
    ) -> list[CardChange]:
        store_key = _order_store_key(order)
        terminal = order.status in TERMINAL_ALGO_STATUSES
        if terminal:
            self.orders.pop(store_key, None)
        else:
            self.orders[store_key] = order
        key = card_key(order.symbol, order.position_side)
        return self._publish(key, ChangeReason.TP_SL, event_time)

    def apply_position(
        self,
        position: Position,
        *,
        event_time: int = 0,
    ) -> list[CardChange]:
        key_tuple = (position.symbol, position.position_side)
        previous = self.positions.get(key_tuple)
        key = card_key(position.symbol, position.position_side)
        if position.amount == 0:
            self.positions.pop(key_tuple, None)
            self._entry_fills.pop(key, None)
        else:
            self.positions[key_tuple] = position
        reason = ChangeReason.POSITION
        if position.amount == 0 and previous is not None and previous.amount != 0:
            reason = ChangeReason.CLOSED
        elif (
            previous is not None
            and previous.amount != 0
            and position.amount != 0
            and abs(position.amount) < abs(previous.amount)
        ):
            reason = ChangeReason.FILL
        elif previous is not None and previous.amount == 0 and position.amount != 0:
            reason = ChangeReason.FILL
        elif previous is None and position.amount != 0:
            reason = ChangeReason.FILL
        return self._publish(key, reason, event_time)

    def _reason_for_order(
        self,
        order: WorkingOrder,
        execution_type: str,
        previous: Optional[WorkingOrder],
    ) -> ChangeReason:
        exec_type = (execution_type or order.execution_type or "").upper()
        if order.is_protective() or order.is_take_profit() or order.is_stop_loss():
            return ChangeReason.TP_SL
        if exec_type == "TRADE" or order.filled_qty > ZERO:
            if order.reduce_only:
                return ChangeReason.FILL
            return ChangeReason.FILL
        if exec_type in {"CANCELED", "CANCELLED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            return ChangeReason.ENTRY_CANCELED
        if exec_type == "AMENDMENT":
            return ChangeReason.ENTRY_AMENDED
        if (
            previous is not None
            and previous.is_entry()
            and order.is_entry()
            and previous.price != order.price
        ):
            return ChangeReason.ENTRY_AMENDED
        if exec_type in {"NEW", ""} and order.is_entry():
            return ChangeReason.ENTRY_NEW
        if order.is_entry():
            return ChangeReason.ENTRY_NEW
        return ChangeReason.POSITION

    def _all_keys(self) -> set[str]:
        keys = {card_key(o.symbol, o.position_side) for o in self.orders.values()}
        keys.update(card_key(p.symbol, p.position_side) for p in self.positions.values())
        keys.update(self._cards)
        return keys

    def _publish(self, key: str, reason: ChangeReason, event_time: int) -> list[CardChange]:
        card = self._build_card(key, event_time=event_time)
        if card is None:
            return []
        previous = self._cards.get(key)
        self._cards[key] = card
        if previous is not None and previous == card and reason != ChangeReason.SNAPSHOT:
            return []
        return [CardChange(key=key, reason=reason, card=card, previous=previous)]

    def _build_card(self, key: str, event_time: int = 0) -> Optional[TradeCardState]:
        symbol, _, position_side = key.partition(":")
        if not symbol or not position_side:
            return None

        position = self.positions.get((symbol, position_side))
        orders = [o for o in self.orders.values() if _belongs(o, symbol, position_side)]
        entries = [o for o in orders if o.is_entry()]
        tps = [o for o in orders if o.is_take_profit()]
        sls = [o for o in orders if o.is_stop_loss() or (o.is_protective() and not o.is_take_profit())]

        previous = self._cards.get(key)
        entry_side = entries[0].side if entries else None
        amount = position.amount if position is not None else ZERO
        side = derive_trade_side(
            amount if amount != 0 else None,
            position_side,
            entry_side or (previous.side if previous and previous.side in {"LONG", "SHORT"} else None),
        )

        planned_entry = entries[0].display_price if entries else None
        if planned_entry == ZERO:
            planned_entry = None

        fill = self._entry_fills.get(key)
        filled_entry = None
        quantity: Optional[Decimal] = None
        notional: Optional[Decimal] = None
        if position is not None and position.is_open():
            filled_entry = position.entry_price if position.entry_price else None
            quantity = abs(position.amount)
            notional = abs(position.notional) if position.notional else None
            if (notional is None or notional == 0) and filled_entry and quantity:
                notional = filled_entry * quantity
        elif fill is not None and (fill.average_price > ZERO or fill.filled_qty > ZERO):
            filled_entry = fill.average_price if fill.average_price > ZERO else fill.price
            quantity = fill.filled_qty if fill.filled_qty > ZERO else fill.quantity
            if filled_entry and quantity:
                notional = filled_entry * quantity
        elif entries:
            quantity = abs(entries[0].quantity - entries[0].filled_qty) or entries[0].quantity
            if planned_entry and quantity:
                notional = planned_entry * quantity

        stop_loss = sls[0].protective_price if sls and sls[0].protective_price != 0 else None
        take_profit = tps[0].protective_price if tps and tps[0].protective_price != 0 else None

        pending_fill = fill is not None and (fill.average_price > ZERO or fill.filled_qty > ZERO)
        status = self._resolve_status(
            previous=previous,
            has_position=position is not None and position.is_open(),
            current_amount=abs(amount),
            has_entry=bool(entries),
            planned_entry=planned_entry,
            pending_fill=pending_fill,
        )

        has_anything = (
            (position is not None and position.is_open())
            or bool(orders)
            or pending_fill
            or (previous is not None and status == CardStatus.CLOSED)
        )
        if not has_anything:
            return previous

        return TradeCardState(
            key=key,
            symbol=symbol,
            position_side=position_side,
            side=side,
            status=status,
            planned_entry=planned_entry,
            filled_entry=filled_entry,
            quantity=quantity,
            notional=notional,
            stop_loss=stop_loss,
            take_profit=take_profit,
            updated_event_time=event_time or (previous.updated_event_time if previous else 0),
        )

    def _resolve_status(
        self,
        *,
        previous: Optional[TradeCardState],
        has_position: bool,
        current_amount: Decimal,
        has_entry: bool,
        planned_entry: Optional[Decimal],
        pending_fill: bool = False,
    ) -> CardStatus:
        prev_qty = previous.quantity if previous and previous.quantity is not None else ZERO
        prev_status = previous.status if previous else None

        if has_position:
            if (
                prev_status in {CardStatus.FILLED, CardStatus.REDUCED}
                and prev_qty > 0
                and current_amount < prev_qty
            ):
                return CardStatus.REDUCED
            return CardStatus.FILLED

        if pending_fill and prev_status in {None, CardStatus.PLANNED, CardStatus.AMENDED, CardStatus.FILLED}:
            # A fill can arrive on the order stream before ACCOUNT_UPDATE.
            return CardStatus.FILLED

        if has_entry:
            if prev_status in {CardStatus.AMENDED, CardStatus.PLANNED} and (
                previous is not None
                and previous.planned_entry is not None
                and planned_entry is not None
                and previous.planned_entry != planned_entry
            ):
                return CardStatus.AMENDED
            if prev_status == CardStatus.AMENDED:
                return CardStatus.AMENDED
            return CardStatus.PLANNED

        if prev_status in {CardStatus.FILLED, CardStatus.REDUCED, CardStatus.CLOSED}:
            return CardStatus.CLOSED
        if prev_status in {CardStatus.PLANNED, CardStatus.AMENDED}:
            # Entry canceled with no position: keep a closed/canceled card.
            return CardStatus.CLOSED
        return CardStatus.CLOSED
