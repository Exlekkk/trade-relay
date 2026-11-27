# trade-relay

Broadcast the leader's Binance USD-M futures orders to a private Telegram group as one editable live trade card.

Friends read the card and place their own orders. This is a **signal broadcast**, not Binance copy trading, and the bot **never places, amends, or cancels orders** for anyone.

## What it is / is not

**Is**

- Watch a leader account with a **read-only** Binance API key
- Track working orders (new / amend / cancel), fills and position changes, and TP/SL (including algo / conditional orders)
- Edit a single Telegram message per trade when something changes, instead of spamming a new message for every tweak
- Debounce entry-price amendments: wait ~15–30 seconds after the last change, then update the card once

**Is not**

- One-click copy trading, or any kind of order placement on follower accounts
- A trading strategy, signal service, or chart/indicator bot
- A replacement for AlertBot (this repo is separate)

## Read-only key warning

Create a **dedicated** Binance API key for this bot:

- Enable reading. Do **not** enable withdrawals.
- If Binance requires a futures permission for the user-data stream, still never grant this project the right to trade — this code has no trading, cancel, transfer, or withdrawal endpoints.
- Restrict the key to trusted IPs when you can.
- Put the key, the Telegram bot token, and the chat id in a local `.env` only.

**Secrets must never be committed.** `.env` is gitignored. Only `.env.example` (placeholders, no real values) belongs in git.

## How it works

1. On start (and after every reconnect) the bot takes a REST snapshot of open orders, positions, and open algo / conditional orders.
2. It keeps a `listenKey` and follows the USD-M futures **user data stream** (`ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `ALGO_UPDATE`).
3. An in-memory book turns those events into a per-trade card (symbol + position side): planned / filled / amended / reduced / closed, with planned or filled entry, size or notional if configured, and SL/TP.
4. Entry-price amendments are **debounced**. Fills, cancels, new entries, TP/SL changes, and position changes update the card immediately and cancel any pending debounce for that trade.
5. Telegram `sendMessage` creates the card; later changes use `editMessageText` on the same message. Message ids are stored in a local, gitignored state file.

## Setup

Python 3.11 or newer.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with the read-only Binance key and Telegram bot token / group chat id
```

Add the Telegram bot to the private group, then run:

```bash
python -m trade_relay
```

If required env vars are missing, the process exits with a short message and does nothing else.

A sample systemd unit is in `systemd/trade-relay.service` (uses `EnvironmentFile=`; no secrets in the unit file).

## Tests

Unit tests cover debounce, card rendering, and book state transitions only. They do not call Binance or Telegram.

```bash
pytest
```
