"""Load and validate environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DEBOUNCE_SECONDS = 20
MIN_DEBOUNCE_SECONDS = 15
MAX_DEBOUNCE_SECONDS = 30
DEFAULT_STATE_FILE = Path("data/state.json")
DEFAULT_DISPLAY_TZ = "Asia/Shanghai"
DEFAULT_FAPI_BASE = "https://fapi.binance.com"
DEFAULT_WS_BASE = "wss://fstream.binance.com"


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    raw = os.getenv(name, "").strip()
    if not raw or raw.startswith("your_"):
        raise ConfigError(
            f"Missing required setting {name}. "
            "Copy .env.example to .env and fill in real values."
        )
    return raw


def _debounce_seconds() -> int:
    raw = os.getenv("DEBOUNCE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_DEBOUNCE_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("DEBOUNCE_SECONDS must be an integer.") from exc
    if value <= 0:
        raise ConfigError("DEBOUNCE_SECONDS must be a positive integer.")
    return value


@dataclass(frozen=True)
class Config:
    binance_api_key: str
    binance_api_secret: str
    telegram_bot_token: str
    telegram_chat_id: str
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS
    state_file: Path = DEFAULT_STATE_FILE
    show_size: bool = True
    show_notional: bool = False
    display_tz: str = DEFAULT_DISPLAY_TZ
    fapi_base: str = DEFAULT_FAPI_BASE
    ws_base: str = DEFAULT_WS_BASE

    @property
    def debounce_in_recommended_range(self) -> bool:
        return MIN_DEBOUNCE_SECONDS <= self.debounce_seconds <= MAX_DEBOUNCE_SECONDS


def load_config(env_file: Path | None = None) -> Config:
    """Load `.env` (if present) and validate required variables."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)

    return Config(
        binance_api_key=_require("BINANCE_API_KEY"),
        binance_api_secret=_require("BINANCE_API_SECRET"),
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
        debounce_seconds=_debounce_seconds(),
        state_file=Path(os.getenv("STATE_FILE", str(DEFAULT_STATE_FILE))).expanduser(),
        show_size=_truthy(os.getenv("SHOW_SIZE"), False),
        show_notional=_truthy(os.getenv("SHOW_NOTIONAL"), False),
        display_tz=os.getenv("DISPLAY_TZ", DEFAULT_DISPLAY_TZ).strip() or DEFAULT_DISPLAY_TZ,
        fapi_base=os.getenv("BINANCE_FAPI_BASE", DEFAULT_FAPI_BASE).rstrip("/"),
        ws_base=os.getenv("BINANCE_WS_BASE", DEFAULT_WS_BASE).rstrip("/"),
    )
