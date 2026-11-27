# trade-relay

Broadcast the leader's Binance USD-M futures orders to a private Telegram group as one editable live trade card.

Friends read the card and place their own orders. This is a **signal broadcast**, not Binance copy trading, and the bot **never places, amends, or cancels orders** for anyone.

## What it is / is not

**Is**

- Watch a leader account with a **read-only** Binance API key
- Track working orders (new / amend / cancel), fills and position changes, reduce-only close limits, and TP/SL (including algo / conditional orders)
- Edit a single Telegram message per trade when something changes, instead of spamming a new message for every tweak
- After 已撤单 or 已平仓, the next trade on the same symbol and side starts a new Telegram message

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
3. An in-memory book turns those events into a per-trade card (symbol + position side): 开单 / 已改价 / 已开仓 / 已挂平仓 / 已改平仓 / 已减仓 / 已平仓 / 已撤单. Quantity and notional stay off unless configured. A stop-loss line is shown only when a real SL is working; a reduce-only LIMIT is the close-limit (平仓挂单), not an empty TP placeholder.
4. Every card change, including entry-price and close-limit amendments, updates Telegram immediately.
5. Telegram `sendMessage` creates the card; later changes on the same trade use `editMessageText`. After 已撤单 or 已平仓, the next trade on that key is a new `sendMessage`. Message ids and the last published status are stored in a local, gitignored state file.

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

Unit tests cover card rendering, book state transitions, Telegram send-or-edit, and publish routing only. They do not call Binance or Telegram.

```bash
pytest
```
