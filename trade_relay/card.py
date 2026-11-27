"""Render a compact Chinese Telegram trade card (HTML parse mode)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trade_relay.models import CardStatus, TradeCardState, format_decimal

STATUS_LABELS = {
    CardStatus.PLANNED: "计划开仓",
    CardStatus.FILLED: "已开仓",
    CardStatus.AMENDED: "已改价",
    CardStatus.REDUCED: "已减仓",
    CardStatus.CLOSED: "已平仓",
}

SIDE_LABELS = {
    "LONG": "做多",
    "SHORT": "做空",
    "BUY": "做多",
    "SELL": "做空",
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


def _entry_line(card: TradeCardState) -> str:
    if card.status in {CardStatus.FILLED, CardStatus.REDUCED, CardStatus.CLOSED}:
        if card.filled_entry is not None:
            return f"开仓价：{format_decimal(card.filled_entry)}（成交均价）"
        if card.planned_entry is not None:
            return f"开仓价：{format_decimal(card.planned_entry)}（挂单）"
        return "开仓价：—"
    if card.planned_entry is not None:
        return f"开仓价：{format_decimal(card.planned_entry)}（挂单）"
    if card.filled_entry is not None:
        return f"开仓价：{format_decimal(card.filled_entry)}（成交均价）"
    return "开仓价：—"


def render_card(
    card: TradeCardState,
    *,
    show_size: bool = True,
    show_notional: bool = False,
    display_tz: str = "Asia/Shanghai",
) -> str:
    """Return Telegram HTML for one live trade card."""
    side = SIDE_LABELS.get(card.side, card.side)
    status = STATUS_LABELS.get(card.status, card.status.value)
    symbol = escape(card.symbol)
    lines = [
        f"<b>📊 {symbol}</b>  {escape(side)} · {escape(status)}",
        "",
        escape(_entry_line(card)),
    ]
    if show_size and card.quantity is not None:
        lines.append(escape(f"数量：{format_decimal(card.quantity)}"))
    if show_notional and card.notional is not None:
        lines.append(escape(f"名义价值：{format_decimal(card.notional)} USDT"))
    sl = format_decimal(card.stop_loss) if card.stop_loss is not None else "—"
    tp = format_decimal(card.take_profit) if card.take_profit is not None else "—"
    lines.append(escape(f"止损：{sl}"))
    lines.append(escape(f"止盈：{tp}"))
    lines.append(escape(f"状态：{status}"))
    lines.append(escape(f"更新：{_when(card, display_tz)}"))
    return "\n".join(lines)
