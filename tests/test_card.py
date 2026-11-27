"""Card rendering: compact Chinese text and status labels."""

from __future__ import annotations

from decimal import Decimal

from trade_relay.card import render_card
from trade_relay.models import CardStatus, TradeCardState


# 2026-11-27 13:25 Asia/Shanghai
TS_1325 = 1795757100000
# 2026-11-27 13:28 Asia/Shanghai
TS_1328 = 1795757280000
# 2026-11-27 13:40 Asia/Shanghai
TS_1340 = 1795758000000


def _card(**overrides: object) -> TradeCardState:
    values: dict = dict(
        key="BTCUSDT:BOTH",
        symbol="BTCUSDT",
        position_side="BOTH",
        side="LONG",
        status=CardStatus.PLANNED,
        planned_entry=Decimal("61696.1"),
        previous_planned_entry=None,
        filled_entry=None,
        quantity=Decimal("0.01"),
        notional=Decimal("616.961"),
        stop_loss=None,
        take_profit=None,
        working_close=None,
        previous_close=None,
        filled_close=None,
        close_kind=None,
        updated_event_time=TS_1325,
    )
    values.update(overrides)
    return TradeCardState(**values)


def test_new_limit_entry_card() -> None:
    text = render_card(_card())
    assert text == (
        "BTCUSDT  做多 · 开单\n"
        "\n"
        "挂单开仓  61696.1\n"
        "更新 11-27 13:25"
    )


def test_entry_amend_shows_previous_price() -> None:
    text = render_card(
        _card(
            status=CardStatus.AMENDED,
            planned_entry=Decimal("61500"),
            previous_planned_entry=Decimal("61696.1"),
            updated_event_time=TS_1328,
        )
    )
    assert text == (
        "BTCUSDT  做多 · 已改价\n"
        "\n"
        "挂单开仓  61500\n"
        "改前      61696.1\n"
        "更新 11-27 13:28"
    )


def test_filled_card_shows_average_entry() -> None:
    text = render_card(
        _card(
            status=CardStatus.FILLED,
            planned_entry=None,
            filled_entry=Decimal("61696.1"),
            updated_event_time=TS_1340,
        )
    )
    assert "已开仓" in text
    assert "开仓均价  61696.1" in text
    assert "（成交均价）" not in text
    assert "挂单开仓" not in text


def test_close_working_and_amended_and_closed_labels() -> None:
    hanging = render_card(
        _card(
            status=CardStatus.CLOSE_WORKING,
            planned_entry=None,
            filled_entry=Decimal("61696.1"),
            working_close=Decimal("62367.7"),
        )
    )
    assert "已挂平仓" in hanging
    assert "开仓均价  61696.1" in hanging
    assert "平仓挂单  62367.7" in hanging

    amended = render_card(
        _card(
            status=CardStatus.CLOSE_AMENDED,
            planned_entry=None,
            filled_entry=Decimal("61696.1"),
            working_close=Decimal("62500"),
            previous_close=Decimal("62367.7"),
        )
    )
    assert "已改平仓" in amended
    assert "平仓挂单  62500" in amended
    assert "改前      62367.7" in amended

    assert "已减仓" in render_card(
        _card(status=CardStatus.REDUCED, planned_entry=None, filled_entry=Decimal("61696.1"))
    )
    canceled = render_card(_card(status=CardStatus.CANCELED))
    assert "已撤单" in canceled
    assert "挂单开仓  61696.1" in canceled


def test_limit_and_market_close_price_lines() -> None:
    limit_text = render_card(
        _card(
            status=CardStatus.CLOSED,
            planned_entry=None,
            filled_entry=Decimal("61696.1"),
            filled_close=Decimal("62367.7"),
            close_kind="limit",
        )
    )
    assert "已平仓" in limit_text
    assert "开仓均价  61696.1" in limit_text
    assert "平仓价    62367.7（限价）" in limit_text

    market_text = render_card(
        _card(
            status=CardStatus.CLOSED,
            planned_entry=None,
            filled_entry=Decimal("61696.1"),
            filled_close=Decimal("61280"),
            close_kind="market",
        )
    )
    assert "平仓价    61280（市价）" in market_text


def test_closed_omits_price_when_unknown() -> None:
    text = render_card(
        _card(
            status=CardStatus.CLOSED,
            planned_entry=None,
            filled_entry=Decimal("61696.1"),
            filled_close=None,
            close_kind=None,
        )
    )
    assert "已平仓" in text
    assert "平仓价" not in text


def test_no_stop_line_when_none_and_no_placeholder_dashes() -> None:
    text = render_card(_card())
    assert "止损" not in text
    assert "止盈" not in text
    assert "—" not in text
    assert "状态：" not in text


def test_stop_loss_shown_only_when_present() -> None:
    text = render_card(_card(stop_loss=Decimal("89200")))
    assert "止损      89200" in text
    assert "止盈" not in text


def test_quantity_hidden_by_default() -> None:
    text = render_card(_card())
    assert "数量" not in text
    assert "名义价值" not in text
    text_on = render_card(_card(), show_size=True, show_notional=True)
    assert "数量      0.01" in text_on
    assert "名义价值  616.961 USDT" in text_on


def test_html_special_characters_in_symbol_are_escaped() -> None:
    text = render_card(_card(symbol="BTC<USDT>"))
    assert "BTC&lt;USDT&gt;" in text
    assert "<USDT>" not in text
    assert "做多" in text
