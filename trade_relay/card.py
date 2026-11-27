"""Render a compact Chinese Telegram trade card (HTML parse mode)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trade_relay.models import CardStatus, TradeCardState, format_decimal

STATUS_LABELS = {
    CardStatus.PLANNED: "开单",
    CardStatus.FILLED: "已开仓",
    CardStatus.AMENDED: "已改价",
    CardStatus.CLOSE_WORKING: "已挂平仓",
    CardStatus.CLOSE_AMENDED: "已改平仓",
    CardStatus.REDUCED: "已减仓",
    CardStatus.CLOSED: "已平仓",
    CardStatus.CANCELED: "已撤单",
}

SIDE_LABELS = {
    "LONG": "做多",
    "SHORT": "做空",
    "BUY": "做多",
    "SELL": "做空",
}

CLOSE_KIND_LABELS = {
    "limit": "限价",
    "market": "市价",
}


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def _when(card: TradeCardState, tz_name: str) -> str:
    tz = _tz(tz_name)
    if card.updated_event_time and card.updated_event_time > 0:
        # Binance event times are milliseconds since epoch.
        ts = card.updated_event_time / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
    else:
        dt = datetime.now(tz)
    return dt.strftime("%m-%d %H:%M")


def _pad_label(label: str) -> str:
    """Pad so prices line up, counting CJK characters as width 2."""
    width = sum(2 if ord(ch) > 127 else 1 for ch in label)
    return label + " " * max(1, 10 - width)


def _line(label: str, value: str) -> str:
    return f"{_pad_label(label)}{value}"


def render_card(
    card: TradeCardState,
    *,
    show_size: bool = False,
    show_notional: bool = False,
    display_tz: str = "Asia/Shanghai",
) -> str:
    """Return Telegram HTML for one live trade card."""
    side = SIDE_LABELS.get(card.side, card.side)
    status = STATUS_LABELS.get(card.status, card.status.value)
    symbol = escape(card.symbol)
    lines = [
        f"{symbol}  {escape(side)} · {escape(status)}",
        "",
    ]

    open_statuses = {
        CardStatus.FILLED,
        CardStatus.REDUCED,
        CardStatus.CLOSE_WORKING,
        CardStatus.CLOSE_AMENDED,
        CardStatus.CLOSED,
    }
    if card.status in open_statuses:
        if card.filled_entry is not None:
            lines.append(escape(_line("开仓均价", format_decimal(card.filled_entry))))
        elif card.planned_entry is not None:
            lines.append(escape(_line("挂单开仓", format_decimal(card.planned_entry))))
    else:
        if card.planned_entry is not None:
            lines.append(escape(_line("挂单开仓", format_decimal(card.planned_entry))))
        elif card.filled_entry is not None:
            lines.append(escape(_line("开仓均价", format_decimal(card.filled_entry))))
        if card.status == CardStatus.AMENDED and card.previous_planned_entry is not None:
            lines.append(escape(_line("改前", format_decimal(card.previous_planned_entry))))

    if card.status in {CardStatus.CLOSE_WORKING, CardStatus.CLOSE_AMENDED, CardStatus.REDUCED}:
        if card.working_close is not None:
            lines.append(escape(_line("平仓挂单", format_decimal(card.working_close))))
        if card.status == CardStatus.CLOSE_AMENDED and card.previous_close is not None:
            lines.append(escape(_line("改前", format_decimal(card.previous_close))))

    if card.status == CardStatus.CLOSED and card.filled_close is not None:
        price = format_decimal(card.filled_close)
        kind = CLOSE_KIND_LABELS.get(card.close_kind or "", "")
        value = f"{price}（{kind}）" if kind else price
        lines.append(escape(_line("平仓价", value)))

    if card.stop_loss is not None:
        lines.append(escape(_line("止损", format_decimal(card.stop_loss))))

    if show_size and card.quantity is not None:
        lines.append(escape(_line("数量", format_decimal(card.quantity))))
    if show_notional and card.notional is not None:
        lines.append(escape(f"{_pad_label('名义价值')}{format_decimal(card.notional)} USDT"))

    # Drop a trailing blank line when the body has no price rows.
    if lines[-1] == "":
        lines.pop()
    lines.append(escape(f"更新 {_when(card, display_tz)}"))
    return "\n".join(lines)
