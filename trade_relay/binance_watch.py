"""Read-only USD-M futures watch: listenKey user stream + REST snapshots.

This module intentionally exposes no trading methods. The REST client can
create/keep-alive a listenKey and GET open orders, positions, and open algo
orders. It never calls order placement, cancel, transfer, or withdrawal
endpoints.

Event field names follow the official USD-M futures user-data stream:

* ORDER_TRADE_UPDATE — https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Order-Update
* ACCOUNT_UPDATE — https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Balance-and-Position-Update
* ALGO_UPDATE — https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Algo-Order-Update

The private WebSocket base (after the 2026-04-23 legacy URL retirement) is
``wss://fstream.binance.com/private/ws/<listenKey>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import websockets

from trade_relay.book import AccountBook, CardChange
from trade_relay.models import ZERO, Position, WorkingOrder, d

logger = logging.getLogger(__name__)

# listenKey is valid for 60 minutes; refresh well before that.
LISTENKEY_KEEPALIVE_SECONDS = 30 * 60
# Each websocket connection should be recycled within 24 hours; reconnect sooner.
WS_MAX_AGE_SECONDS = 12 * 60 * 60

# Documented ORDER_TRADE_UPDATE ``o`` fields used here (not an exhaustive map):
# s symbol, c clientOrderId, S side, o type, q origQty, p price, ap avgPrice,
# sp stopPrice, x executionType, X orderStatus, i orderId, l lastFilledQty,
# z cumFilledQty, R reduceOnly, ps positionSide, cp closePosition.
#
# Documented ACCOUNT_UPDATE ``a.P[]`` fields used here:
# s symbol, pa positionAmt, ep entryPrice, up unrealizedPnl, ps positionSide.
# ``notional`` is not on the stream event; it is taken from the REST snapshot
# when present (positionRisk ``notional``).
#
# Documented ALGO_UPDATE ``o`` fields used here:
# aid algoId, caid clientAlgoId, at algoType, o orderType, s symbol, S side,
# ps positionSide, q quantity, X algoStatus, tp triggerPrice, p price,
# R reduceOnly, cp closePosition.

SnapshotHandler = Callable[[list[CardChange]], Awaitable[None] | None]
ChangeHandler = Callable[[list[CardChange]], Awaitable[None] | None]


class BinanceReadError(RuntimeError):
    """A read-only Binance REST call failed."""


class BinanceReadClient:
    """Signed GET + listenKey management for USD-M futures. No trading APIs."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        fapi_base: str = "https://fapi.binance.com",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._base = fapi_base.rstrip("/")
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def create_listen_key(self) -> str:
        # POST /fapi/v1/listenKey — API key required, HMAC not required.
        body = await self._request("POST", "/fapi/v1/listenKey", signed=False)
        key = body.get("listenKey")
        if not key:
            raise BinanceReadError(f"listenKey missing in response: {body}")
        return str(key)

    async def keepalive_listen_key(self, listen_key: str) -> None:
        await self._request(
            "PUT",
            "/fapi/v1/listenKey",
            signed=False,
            params={"listenKey": listen_key},
        )

    async def close_listen_key(self, listen_key: str) -> None:
        try:
            await self._request(
                "DELETE",
                "/fapi/v1/listenKey",
                signed=False,
                params={"listenKey": listen_key},
            )
        except BinanceReadError as exc:
            logger.warning("Failed to close listenKey: %s", exc)

    async def open_orders(self) -> list[WorkingOrder]:
        payload = await self._request("GET", "/fapi/v1/openOrders", signed=True)
        if not isinstance(payload, list):
            raise BinanceReadError("openOrders did not return a list")
        return [parse_rest_open_order(item) for item in payload if isinstance(item, dict)]

    async def position_risk(self) -> list[Position]:
        payload = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        if not isinstance(payload, list):
            raise BinanceReadError("positionRisk did not return a list")
        positions = [parse_rest_position(item) for item in payload if isinstance(item, dict)]
        return [p for p in positions if p.amount != 0]

    async def open_algo_orders(self) -> list[WorkingOrder]:
        payload = await self._request("GET", "/fapi/v1/openAlgoOrders", signed=True)
        if not isinstance(payload, list):
            raise BinanceReadError("openAlgoOrders did not return a list")
        return [parse_rest_algo_order(item) for item in payload if isinstance(item, dict)]

    async def snapshot(self) -> tuple[list[WorkingOrder], list[Position]]:
        """Open orders + open algo orders + non-zero positions."""
        orders = await self.open_orders()
        positions = await self.position_risk()
        try:
            algo_orders = await self.open_algo_orders()
        except BinanceReadError as exc:
            logger.warning("openAlgoOrders snapshot failed (will rely on ALGO_UPDATE): %s", exc)
            algo_orders = []
        return orders + algo_orders, positions

    async def _client_obj(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        signed: bool,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        query = dict(params or {})
        headers = {"X-MBX-APIKEY": self._api_key}
        if signed:
            query["timestamp"] = int(time.time() * 1000)
            encoded = urlencode(query, doseq=True)
            signature = hmac.new(self._api_secret, encoded.encode("utf-8"), hashlib.sha256).hexdigest()
            query["signature"] = signature
        client = await self._client_obj()
        url = f"{self._base}{path}"
        response = await client.request(method, url, params=query, headers=headers)
        try:
            body = response.json()
        except ValueError as exc:
            raise BinanceReadError(f"{method} {path} returned non-JSON ({response.status_code})") from exc
        if response.status_code >= 400 or (isinstance(body, dict) and "code" in body and body.get("code") not in (0, "0", None) and "listenKey" not in body):
            # Successful listenKey POST is {"listenKey": "..."}; errors look like
            # {"code": -2015, "msg": "..."}.
            if isinstance(body, dict) and "code" in body and "listenKey" not in body:
                code = body.get("code")
                msg = body.get("msg") or body
                raise BinanceReadError(f"{method} {path} failed: code={code} msg={msg}")
            if response.status_code >= 400:
                raise BinanceReadError(f"{method} {path} failed: HTTP {response.status_code} {body}")
        return body


def parse_rest_open_order(item: dict[str, Any]) -> WorkingOrder:
    """Map GET /fapi/v1/openOrders fields (official REST schema)."""
    return WorkingOrder(
        order_id=int(item["orderId"]),
        symbol=str(item["symbol"]),
        side=str(item.get("side", "")),
        position_side=str(item.get("positionSide", "BOTH")),
        order_type=str(item.get("type") or item.get("origType") or ""),
        price=d(item.get("price")),
        quantity=d(item.get("origQty")),
        filled_qty=d(item.get("executedQty")),
        average_price=d(item.get("avgPrice")),
        stop_price=d(item.get("stopPrice")),
        trigger_price=d(item.get("stopPrice")),
        reduce_only=bool(item.get("reduceOnly", False)),
        close_position=bool(item.get("closePosition", False)),
        status=str(item.get("status", "NEW")),
        client_order_id=str(item.get("clientOrderId", "")),
        is_algo=False,
    )


def parse_rest_position(item: dict[str, Any]) -> Position:
    """Map GET /fapi/v2/positionRisk fields (official REST schema)."""
    return Position(
        symbol=str(item["symbol"]),
        position_side=str(item.get("positionSide", "BOTH")),
        amount=d(item.get("positionAmt")),
        entry_price=d(item.get("entryPrice")),
        unrealized_pnl=d(item.get("unRealizedProfit")),
        notional=d(item.get("notional")),
    )


def parse_rest_algo_order(item: dict[str, Any]) -> WorkingOrder:
    """Map GET /fapi/v1/openAlgoOrders fields (same shape as allAlgoOrders)."""
    return WorkingOrder(
        order_id=int(item["algoId"]),
        symbol=str(item["symbol"]),
        side=str(item.get("side", "")),
        position_side=str(item.get("positionSide", "BOTH")),
        order_type=str(item.get("orderType", "")),
        price=d(item.get("price")),
        quantity=d(item.get("quantity")),
        filled_qty=ZERO,  # open algo orders are untriggered; field is not on the REST schema
        average_price=d(item.get("actualPrice")),
        stop_price=d(item.get("triggerPrice")),
        trigger_price=d(item.get("triggerPrice")),
        reduce_only=bool(item.get("reduceOnly", False)),
        close_position=bool(item.get("closePosition", False)),
        status=str(item.get("algoStatus", "NEW")),
        client_order_id=str(item.get("clientAlgoId", "")),
        is_algo=True,
    )


def parse_order_trade_update(payload: dict[str, Any]) -> Optional[tuple[WorkingOrder, str, int]]:
    """Return (order, execution_type, event_time) for an ORDER_TRADE_UPDATE."""
    if payload.get("e") != "ORDER_TRADE_UPDATE":
        return None
    o = payload.get("o")
    if not isinstance(o, dict):
        return None
    order = WorkingOrder(
        order_id=int(o["i"]),
        symbol=str(o.get("s", "")),
        side=str(o.get("S", "")),
        position_side=str(o.get("ps", "BOTH")),
        order_type=str(o.get("o") or o.get("ot") or ""),
        price=d(o.get("p")),
        quantity=d(o.get("q")),
        filled_qty=d(o.get("z")),
        average_price=d(o.get("ap")),
        stop_price=d(o.get("sp")),
        trigger_price=d(o.get("sp")),
        reduce_only=bool(o.get("R", False)),
        close_position=bool(o.get("cp", False)),
        status=str(o.get("X", "")),
        client_order_id=str(o.get("c", "")),
        is_algo=False,
        execution_type=str(o.get("x", "")),
    )
    event_time = int(payload.get("E") or payload.get("T") or 0)
    return order, str(o.get("x", "")), event_time


def parse_account_update_positions(payload: dict[str, Any]) -> Optional[tuple[list[Position], int]]:
    """Return positions from an ACCOUNT_UPDATE event."""
    if payload.get("e") != "ACCOUNT_UPDATE":
        return None
    a = payload.get("a") or {}
    rows = a.get("P") or []
    positions: list[Position] = []
    if isinstance(rows, list):
        for item in rows:
            if not isinstance(item, dict):
                continue
            positions.append(
                Position(
                    symbol=str(item.get("s", "")),
                    position_side=str(item.get("ps", "BOTH")),
                    amount=d(item.get("pa")),
                    entry_price=d(item.get("ep")),
                    unrealized_pnl=d(item.get("up")),
                )
            )
    event_time = int(payload.get("E") or payload.get("T") or 0)
    return positions, event_time


def parse_algo_update(payload: dict[str, Any]) -> Optional[tuple[WorkingOrder, int]]:
    """Return (order, event_time) for an ALGO_UPDATE."""
    if payload.get("e") != "ALGO_UPDATE":
        return None
    o = payload.get("o")
    if not isinstance(o, dict):
        return None
    order = WorkingOrder(
        order_id=int(o["aid"]),
        symbol=str(o.get("s", "")),
        side=str(o.get("S", "")),
        position_side=str(o.get("ps", "BOTH")),
        order_type=str(o.get("o", "")),
        price=d(o.get("p")),
        quantity=d(o.get("q")),
        filled_qty=d(o.get("aq")),
        average_price=d(o.get("ap")),
        stop_price=d(o.get("tp")),
        trigger_price=d(o.get("tp")),
        reduce_only=bool(o.get("R", False)),
        close_position=bool(o.get("cp", False)),
        status=str(o.get("X", "")),
        client_order_id=str(o.get("caid", "")),
        is_algo=True,
        execution_type=str(o.get("X", "")),
    )
    event_time = int(payload.get("E") or payload.get("T") or 0)
    return order, event_time


def is_listen_key_expired(payload: dict[str, Any]) -> bool:
    event = str(payload.get("e") or "")
    return event in {"listenKeyExpired", "LISTEN_KEY_EXPIRED"}


def apply_user_event(book: AccountBook, payload: dict[str, Any]) -> list[CardChange]:
    """Dispatch a single user-data payload onto the account book."""
    parsed_order = parse_order_trade_update(payload)
    if parsed_order is not None:
        order, execution_type, event_time = parsed_order
        return book.apply_order_update(order, execution_type=execution_type, event_time=event_time)

    parsed_pos = parse_account_update_positions(payload)
    if parsed_pos is not None:
        positions, event_time = parsed_pos
        changes: list[CardChange] = []
        for position in positions:
            changes.extend(book.apply_position(position, event_time=event_time))
        return changes

    parsed_algo = parse_algo_update(payload)
    if parsed_algo is not None:
        order, event_time = parsed_algo
        return book.apply_algo_update(order, event_time=event_time)

    return []


class UserDataWatch:
    """listenKey stream with REST snapshots on start and reconnect."""

    def __init__(
        self,
        client: BinanceReadClient,
        book: AccountBook,
        *,
        ws_base: str = "wss://fstream.binance.com",
        on_changes: Optional[ChangeHandler] = None,
        on_snapshot: Optional[SnapshotHandler] = None,
    ) -> None:
        self._client = client
        self._book = book
        self._ws_base = ws_base.rstrip("/")
        self._on_changes = on_changes
        self._on_snapshot = on_snapshot
        self._stop = asyncio.Event()
        self._listen_key: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def _stream_url(self, listen_key: str) -> str:
        # Official connect example after the /private split:
        # wss://fstream.binance.com/private/ws/<listenKey>
        return f"{self._ws_base}/private/ws/{listen_key}"

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("User data watch session ended; reconnecting in %.0fs", backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
        if self._listen_key:
            await self._client.close_listen_key(self._listen_key)
            self._listen_key = None

    async def _session(self) -> None:
        listen_key = await self._client.create_listen_key()
        self._listen_key = listen_key
        await self._emit_snapshot()
        url = self._stream_url(listen_key)
        logger.info("Connecting to USD-M user data stream")
        keepalive = asyncio.create_task(self._keepalive_loop(listen_key), name="listenKey-keepalive")
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                started = time.monotonic()
                while not self._stop.is_set():
                    if time.monotonic() - started > WS_MAX_AGE_SECONDS:
                        logger.info("Recycling user data websocket before 24h limit")
                        break
                    remaining = WS_MAX_AGE_SECONDS - (time.monotonic() - started)
                    try:
                        recv_timeout = min(60.0, max(1.0, remaining))
                        raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Ignoring non-JSON user data payload")
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if is_listen_key_expired(payload):
                        logger.warning("listenKey expired; reconnecting")
                        break
                    changes = apply_user_event(self._book, payload)
                    if changes:
                        await _maybe_await(self._on_changes, changes)
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass

    async def _emit_snapshot(self) -> None:
        orders, positions = await self._client.snapshot()
        event_time = int(time.time() * 1000)
        changes = self._book.apply_snapshot(orders, positions, event_time=event_time)
        await _maybe_await(self._on_snapshot, changes)

    async def _keepalive_loop(self, listen_key: str) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=LISTENKEY_KEEPALIVE_SECONDS)
                return
            except asyncio.TimeoutError:
                try:
                    await self._client.keepalive_listen_key(listen_key)
                    logger.debug("listenKey keepalive ok")
                except BinanceReadError:
                    logger.exception("listenKey keepalive failed")
                    return


async def _maybe_await(handler: Optional[Callable[..., Awaitable[None] | None]], argument: Any) -> None:
    if handler is None:
        return
    result = handler(argument)
    if asyncio.iscoroutine(result):
        await result
