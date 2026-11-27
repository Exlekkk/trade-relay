"""In-memory account book: snapshots + stream events -> trade cards.

State transitions (per symbol + position side):

* working entry, no position -> 开单 (or 已改价 after an entry-price change)
* unfilled entry canceled -> 已撤单
* position opened / entry filled -> 已开仓
* reduce-only / close-position LIMIT (or algo TP) working -> 已挂平仓
* that close-limit price amended -> 已改平仓
* position size decreased but still open, no close limit -> 已减仓
* position back to flat -> 已平仓
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


OPEN_POSITION_STATUSES = frozenset(
    {
        CardStatus.FILLED,
        CardStatus.REDUCED,
        CardStatus.CLOSE_WORKING,
        CardStatus.CLOSE_AMENDED,
    }
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
        self._close_fills: dict[str, WorkingOrder] = {}
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
        self._close_fills = {}
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
        self._maybe_reset_cycle(key, order)
        terminal = order.status in TERMINAL_ORDER_STATUSES
        if self._is_reducing_fill(order, execution_type):
            self._close_fills[key] = order
        elif (execution_type.upper() == "TRADE" or order.filled_qty > ZERO) and order.is_entry():
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
        previous_order = self.orders.get(store_key)
        terminal = order.status in TERMINAL_ALGO_STATUSES
        if terminal:
            self.orders.pop(store_key, None)
        else:
            self.orders[store_key] = order
        key = card_key(order.symbol, order.position_side)
        if order.is_close_exit() or order.is_take_profit():
            reason = self._reason_for_close(order, previous_order, execution_type=order.execution_type)
        else:
            reason = ChangeReason.TP_SL
        return self._publish(key, reason, event_time)

    def apply_position(
        self,
        position: Position,
        *,
        event_time: int = 0,
    ) -> list[CardChange]:
        key_tuple = (position.symbol, position.position_side)
        previous = self.positions.get(key_tuple)
        key = card_key(position.symbol, position.position_side)
        prev_card = self._cards.get(key)
        if (
            position.amount != 0
            and prev_card is not None
            and prev_card.status in {CardStatus.CLOSED, CardStatus.CANCELED}
        ):
            self._close_fills.pop(key, None)
            if key not in self._entry_fills:
                self._entry_fills.pop(key, None)
        if position.amount == 0:
            self.positions.pop(key_tuple, None)
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

    def _maybe_reset_cycle(self, key: str, order: WorkingOrder) -> None:
        prev = self._cards.get(key)
        if prev is None or prev.status not in {CardStatus.CLOSED, CardStatus.CANCELED}:
            return
        if order.is_entry() or (
            (order.execution_type.upper() == "TRADE" or order.filled_qty > ZERO) and not order.is_exit_fill()
        ):
            self._close_fills.pop(key, None)
            self._entry_fills.pop(key, None)

    def _is_reducing_fill(self, order: WorkingOrder, execution_type: str) -> bool:
        exec_type = (execution_type or order.execution_type or "").upper()
        has_fill = exec_type == "TRADE" or order.filled_qty > ZERO or order.fill_price() > ZERO
        if not has_fill:
            return False
        if order.is_exit_fill():
            return True
        position = self.positions.get((order.symbol, order.position_side))
        if position is None or not position.is_open():
            return False
        if position.amount > 0 and order.side == "SELL":
            return True
        if position.amount < 0 and order.side == "BUY":
            return True
        return False

    def _reason_for_close(
        self,
        order: WorkingOrder,
        previous: Optional[WorkingOrder],
        *,
        execution_type: str = "",
    ) -> ChangeReason:
        exec_type = (execution_type or order.execution_type or "").upper()
        if exec_type == "TRADE" or order.filled_qty > ZERO or order.fill_price() > ZERO:
            if order.status in {"FILLED", "PARTIALLY_FILLED"} or exec_type == "TRADE":
                return ChangeReason.CLOSE_FILL
        if exec_type in {"CANCELED", "CANCELLED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            return ChangeReason.CLOSE_CANCELED
        if exec_type == "AMENDMENT":
            return ChangeReason.CLOSE_AMENDED
        if previous is not None and previous.close_display_price() != order.close_display_price():
            return ChangeReason.CLOSE_AMENDED
        if exec_type in {"NEW", "", "NEW_ALGO"} or previous is None:
            return ChangeReason.CLOSE_NEW
        return ChangeReason.CLOSE_NEW

    def _reason_for_order(
        self,
        order: WorkingOrder,
        execution_type: str,
        previous: Optional[WorkingOrder],
    ) -> ChangeReason:
        exec_type = (execution_type or order.execution_type or "").upper()
        if order.is_stop_loss() and not (
            exec_type == "TRADE" or order.filled_qty > ZERO or order.fill_price() > ZERO
        ):
            return ChangeReason.TP_SL
        if order.is_close_exit() or (
            (order.reduce_only or order.close_position or order.is_take_profit() or order.is_stop_loss())
            and not order.is_entry()
        ):
            return self._reason_for_close(order, previous, execution_type=exec_type)
        if exec_type == "TRADE" or order.filled_qty > ZERO:
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

    def _select_close(self, orders: list[WorkingOrder]) -> Optional[WorkingOrder]:
        closes = [o for o in orders if o.is_close_exit()]
        if not closes:
            return None

        def _rank(order: WorkingOrder) -> tuple[int, int]:
            is_limit = order.order_type in {"LIMIT", "LIMIT_MAKER"} and not order.is_algo
            return (0 if is_limit else 1, -order.order_id)

        closes.sort(key=_rank)
        return closes[0]

    def _build_card(self, key: str, event_time: int = 0) -> Optional[TradeCardState]:
        symbol, _, position_side = key.partition(":")
        if not symbol or not position_side:
            return None

        position = self.positions.get((symbol, position_side))
        orders = [o for o in self.orders.values() if _belongs(o, symbol, position_side)]
        entries = [o for o in orders if o.is_entry()]
        close_order = self._select_close(orders)
        sls = [o for o in orders if o.is_stop_loss()]

        previous = self._cards.get(key)
        entry_side = entries[0].side if entries else None
        amount = position.amount if position is not None else ZERO
        side = derive_trade_side(
            amount if amount != 0 else None,
            position_side,
            entry_side or (previous.side if previous and previous.side in {"LONG", "SHORT"} else None),
        )

        has_position = position is not None and position.is_open()
        # Only start a new cycle when a new position opens after a finished card.
        # A leftover unfilled entry still in the book must not resurrect 开单
        # over an 已平仓 card (the next ENTRY_NEW event starts the new cycle).
        starting_fresh = (
            previous is not None
            and previous.status in {CardStatus.CLOSED, CardStatus.CANCELED}
            and has_position
        )
        if starting_fresh:
            previous = None

        planned_entry = entries[0].display_price if entries else None
        if planned_entry == ZERO:
            planned_entry = None
        if planned_entry is None and previous is not None and previous.status in {
            CardStatus.PLANNED,
            CardStatus.AMENDED,
            CardStatus.CANCELED,
        }:
            planned_entry = previous.planned_entry

        previous_planned_entry = None
        if previous is not None and planned_entry is not None and previous.planned_entry is not None:
            if previous.planned_entry != planned_entry:
                if previous.status == CardStatus.AMENDED and previous.previous_planned_entry is not None:
                    previous_planned_entry = previous.previous_planned_entry
                else:
                    previous_planned_entry = previous.planned_entry
            elif previous.status == CardStatus.AMENDED:
                previous_planned_entry = previous.previous_planned_entry

        fill = self._entry_fills.get(key)
        close_fill = self._close_fills.get(key)
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
        elif previous is not None and previous.filled_entry is not None:
            filled_entry = previous.filled_entry
            quantity = previous.quantity
            notional = previous.notional
        elif entries:
            quantity = abs(entries[0].quantity - entries[0].filled_qty) or entries[0].quantity
            if planned_entry and quantity:
                notional = planned_entry * quantity

        stop_loss = sls[0].protective_price if sls and sls[0].protective_price != 0 else None
        working_close = None
        if close_order is not None:
            price = close_order.close_display_price()
            if price != 0:
                working_close = price
        take_profit = working_close

        previous_close = None
        if previous is not None and working_close is not None and previous.working_close is not None:
            if previous.working_close != working_close:
                if previous.status == CardStatus.CLOSE_AMENDED and previous.previous_close is not None:
                    previous_close = previous.previous_close
                else:
                    previous_close = previous.working_close
            elif previous.status == CardStatus.CLOSE_AMENDED:
                previous_close = previous.previous_close

        filled_close = None
        close_kind = None
        if close_fill is not None:
            price = close_fill.fill_price()
            if price > 0:
                filled_close = price
            close_kind = close_fill.exit_kind()
        elif previous is not None and previous.filled_close is not None:
            filled_close = previous.filled_close
            close_kind = previous.close_kind

        pending_fill = fill is not None and (fill.average_price > ZERO or fill.filled_qty > ZERO)
        pending_full_close = False
        if close_fill is not None:
            qty_covers_position = (
                close_fill.filled_qty >= abs(amount) if has_position and amount != 0 else False
            )
            pending_full_close = (not has_position) or close_fill.close_position or (
                close_fill.status == "FILLED" and (qty_covers_position or not has_position)
            )
        status = self._resolve_status(
            previous=previous,
            has_position=has_position,
            current_amount=abs(amount),
            has_entry=bool(entries),
            planned_entry=planned_entry,
            pending_fill=pending_fill,
            has_close=working_close is not None,
            working_close=working_close,
            pending_full_close=pending_full_close,
        )

        has_anything = (
            has_position
            or bool(orders)
            or pending_fill
            or (close_fill is not None)
            or (previous is not None and status in {CardStatus.CLOSED, CardStatus.CANCELED})
        )
        if not has_anything:
            return previous

        pre_fill = status in {CardStatus.PLANNED, CardStatus.AMENDED, CardStatus.CANCELED}
        return TradeCardState(
            key=key,
            symbol=symbol,
            position_side=position_side,
            side=side,
            status=status,
            planned_entry=planned_entry if status in {CardStatus.PLANNED, CardStatus.AMENDED, CardStatus.CANCELED} else None,
            previous_planned_entry=previous_planned_entry if status == CardStatus.AMENDED else None,
            filled_entry=None if pre_fill else filled_entry,
            quantity=quantity,
            notional=notional,
            stop_loss=None if pre_fill else stop_loss,
            take_profit=None if pre_fill else take_profit,
            working_close=None if pre_fill or status == CardStatus.CLOSED else working_close,
            previous_close=previous_close if status == CardStatus.CLOSE_AMENDED else None,
            filled_close=filled_close if status == CardStatus.CLOSED else None,
            close_kind=close_kind if status == CardStatus.CLOSED else None,
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
        has_close: bool = False,
        working_close: Optional[Decimal] = None,
        pending_full_close: bool = False,
    ) -> CardStatus:
        prev_qty = previous.quantity if previous and previous.quantity is not None else ZERO
        prev_status = previous.status if previous else None

        if pending_full_close:
            return CardStatus.CLOSED

        if has_position or (pending_fill and not has_entry):
            if has_close:
                if prev_status == CardStatus.CLOSE_AMENDED:
                    return CardStatus.CLOSE_AMENDED
                if (
                    previous is not None
                    and previous.working_close is not None
                    and working_close is not None
                    and previous.working_close != working_close
                ):
                    return CardStatus.CLOSE_AMENDED
                return CardStatus.CLOSE_WORKING
            if (
                prev_status in OPEN_POSITION_STATUSES
                and prev_qty > 0
                and current_amount < prev_qty
                and has_position
            ):
                return CardStatus.REDUCED
            if prev_status == CardStatus.REDUCED and has_position:
                return CardStatus.REDUCED
            if pending_fill and prev_status in {
                None,
                CardStatus.PLANNED,
                CardStatus.AMENDED,
                CardStatus.FILLED,
            }:
                return CardStatus.FILLED
            if has_position:
                return CardStatus.FILLED
            return CardStatus.FILLED

        if pending_fill and prev_status in {None, CardStatus.PLANNED, CardStatus.AMENDED, CardStatus.FILLED}:
            # A fill can arrive on the order stream before ACCOUNT_UPDATE.
            if has_close:
                return CardStatus.CLOSE_WORKING
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

        if prev_status in {CardStatus.PLANNED, CardStatus.AMENDED, CardStatus.CANCELED}:
            return CardStatus.CANCELED
        if prev_status in OPEN_POSITION_STATUSES or prev_status in {CardStatus.CLOSED, None}:
            if prev_status in {CardStatus.PLANNED, CardStatus.AMENDED}:
                return CardStatus.CANCELED
            return CardStatus.CLOSED
        return CardStatus.CLOSED
