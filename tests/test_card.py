"""Card rendering: compact Chinese text and status labels."""

from __future__ import annotations

from decimal import Decimal

from trade_relay.card import render_card
from trade_relay.models import CardStatus, TradeCardState


def _card(**overrides: object) -> TradeCardState:
    values: dict = dict(
        key="BTCUSDT:BOTH",
        symbol="BTCUSDT",
        position_side="BOTH",
        side="LONG",
        status=CardStatus.PLANNED,
        planned_entry=Decimal("67250"),
        filled_entry=None,
        quantity=Decimal("0.01"),
        notional=Decimal("672.5"),
        stop_loss=Decimal("66500"),
        take_profit=Decimal("69000"),
        updated_event_time=1732703100000,  # 2024-11-27 18:25 Asia/Shanghai
    )
    values.update(overrides)
    return TradeCardState(**values)


def test_planned_card_shows_hanging_entry_and_chinese_status() -> None:
    text = render_card(_card(), show_size=True, show_notional=False)
    assert "BTCUSDT" in text
    assert "做多" in text
    assert "计划开仓" in text
    assert "开仓价：67250（挂单）" in text
    assert "数量：0.01" in text
    assert "名义价值" not in text
    assert "止损：66500" in text
    assert "止盈：69000" in text
    assert "18:25" in text


def test_filled_card_uses_average_price() -> None:
    text = render_card(
        _card(
            status=CardStatus.FILLED,
            filled_entry=Decimal("67248.5"),
        )
    )
    assert "已开仓" in text
    assert "开仓价：67248.5（成交均价）" in text


def test_amended_reduced_closed_labels() -> None:
    assert "已改价" in render_card(_card(status=CardStatus.AMENDED))
    assert "已减仓" in render_card(_card(status=CardStatus.REDUCED, filled_entry=Decimal("67250")))
    assert "已平仓" in render_card(_card(status=CardStatus.CLOSED, filled_entry=Decimal("67250")))


def test_notional_is_opt_in() -> None:
    text = render_card(_card(), show_size=False, show_notional=True)
    assert "数量：" not in text
    assert "名义价值：672.5 USDT" in text


def test_missing_sl_tp_render_as_dash() -> None:
    text = render_card(_card(stop_loss=None, take_profit=None))
    assert "止损：—" in text
    assert "止盈：—" in text


def test_html_special_characters_in_symbol_are_escaped() -> None:
    text = render_card(_card(symbol="BTC<USDT>"))
    assert "BTC&lt;USDT&gt;" in text
    assert "<USDT>" not in text
