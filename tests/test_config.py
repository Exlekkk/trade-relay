"""Config validation and the no-trading-methods guard."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from trade_relay.binance_watch import BinanceReadClient
from trade_relay.config import ConfigError, load_config


def test_missing_required_env_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="BINANCE_API_KEY"):
        load_config(empty)


def test_placeholder_values_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / "placeholders.env"
    env.write_text(
        "\n".join(
            [
                "BINANCE_API_KEY=your_read_only_api_key",
                "BINANCE_API_SECRET=your_read_only_api_secret",
                "TELEGRAM_BOT_TOKEN=your_telegram_bot_token",
                "TELEGRAM_CHAT_ID=your_telegram_group_chat_id",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError):
        load_config(env)


def test_defaults_and_required_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / "ok.env"
    env.write_text(
        "\n".join(
            [
                "BINANCE_API_KEY=demo-key",
                "BINANCE_API_SECRET=demo-secret",
                "TELEGRAM_BOT_TOKEN=demo-token",
                "TELEGRAM_CHAT_ID=-100123",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DEBOUNCE_SECONDS",
        "SHOW_SIZE",
        "SHOW_NOTIONAL",
    ):
        monkeypatch.delenv(name, raising=False)
    config = load_config(env)
    assert config.debounce_seconds == 20
    assert config.debounce_in_recommended_range
    assert config.show_size is True
    assert config.show_notional is False
    assert config.telegram_chat_id == "-100123"


def test_read_client_has_no_trading_methods() -> None:
    allowed = {
        "aclose",
        "close_listen_key",
        "create_listen_key",
        "keepalive_listen_key",
        "open_algo_orders",
        "open_orders",
        "position_risk",
        "snapshot",
    }
    names = [name for name, _ in inspect.getmembers(BinanceReadClient, predicate=inspect.isfunction)]
    public = {name for name in names if not name.startswith("_")}
    assert public == allowed
    source = inspect.getsource(BinanceReadClient)
    # Placement / cancel / transfer paths must not appear as request targets.
    # Compare quoted path literals so /fapi/v1/openOrders is not a false hit.
    for path in (
        '"/fapi/v1/order"',
        '"/fapi/v1/batchOrders"',
        '"/fapi/v1/algoOrder"',
        '"/fapi/v1/algoOpenOrders"',
        '"/sapi/',
    ):
        assert path not in source, path
    assert "/withdraw" not in source
