# FILE: platform_core/execution/zerodha_integration.py
"""
platform_core.execution.zerodha_integration
═══════════════════════════════════════════════════════════════════════
Production-grade async Zerodha Kite Connect integration layer specialized for
Indian index futures and options workflows.

The module provides:
- Session bootstrap and access-token management.
- Async REST wrappers for order placement, modification, cancellation, and
  portfolio queries.
- Async WebSocket market-data and order-update streaming via a background
  thread bridge.
- Deterministic JSON-serializable request/response models.
- Replay-safe boundary contracts suitable for downstream order manager,
  execution monitor, reconciliation, and latency-monitor components.

This adapter intentionally isolates exchange/broker interaction concerns and
avoids embedding trading strategy logic. It remains executable with the
standard-library only and can optionally leverage `pykiteconnect` when present.
When the official dependency is unavailable, the module still imports safely and
raises explicit runtime errors upon live broker usage.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Callable
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen

try:
    from kiteconnect import KiteConnect  # type: ignore[import-not-found]
    from kiteconnect import KiteTicker  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency boundary
    KiteConnect = None  # type: ignore[assignment]
    KiteTicker = None  # type: ignore[assignment]


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ZerodhaIntegrationError(RuntimeError):
    """Base exception for Zerodha integration failures."""


class ZerodhaConfigurationError(ZerodhaIntegrationError):
    """Raised when Zerodha configuration is invalid."""


class ZerodhaAuthenticationError(ZerodhaIntegrationError):
    """Raised when Zerodha authentication fails."""


class ZerodhaRequestError(ZerodhaIntegrationError):
    """Raised when a Zerodha REST request fails."""


class ZerodhaStreamError(ZerodhaIntegrationError):
    """Raised when a Zerodha streaming operation fails."""


@dataclass(frozen=True, slots=True)
class ZerodhaCredentials:
    """Static API credentials required for Kite Connect."""

    api_key: str
    api_secret: str
    client_id: str | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        if not self.api_key.strip() or not self.api_secret.strip():
            raise ValueError("api_key and api_secret must be non-empty")
        if self.client_id is not None and not self.client_id.strip():
            raise ValueError("client_id must be non-empty when provided")
        if self.user_id is not None and not self.user_id.strip():
            raise ValueError("user_id must be non-empty when provided")


@dataclass(frozen=True, slots=True)
class ZerodhaSession:
    """Serializable authenticated session state."""

    access_token: str
    public_token: str | None
    login_time: datetime
    refreshable: bool
    user_id: str | None = None

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("access_token must be non-empty")
        if self.public_token is not None and not self.public_token.strip():
            raise ValueError("public_token must be non-empty when provided")
        if self.user_id is not None and not self.user_id.strip():
            raise ValueError("user_id must be non-empty when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "access_token": self.access_token,
            "public_token": self.public_token,
            "login_time": _ensure_utc(self.login_time).isoformat(),
            "refreshable": self.refreshable,
            "user_id": self.user_id,
        }


@dataclass(frozen=True, slots=True)
class ZerodhaOrderRequest:
    """Serializable order placement request."""

    exchange: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_type: str
    product: str
    variety: str = "regular"
    validity: str = "DAY"
    price: float | None = None
    trigger_price: float | None = None
    disclosed_quantity: int | None = None
    tag: str | None = None

    def __post_init__(self) -> None:
        string_fields = {
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.transaction_type,
            "order_type": self.order_type,
            "product": self.product,
            "variety": self.variety,
            "validity": self.validity,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price is not None and self.price <= 0.0:
            raise ValueError("price must be positive when provided")
        if self.trigger_price is not None and self.trigger_price <= 0.0:
            raise ValueError("trigger_price must be positive when provided")
        if self.disclosed_quantity is not None and self.disclosed_quantity <= 0:
            raise ValueError("disclosed_quantity must be positive when provided")
        if self.tag is not None and not self.tag.strip():
            raise ValueError("tag must be non-empty when provided")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.transaction_type,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "product": self.product,
            "variety": self.variety,
            "validity": self.validity,
        }
        if self.price is not None:
            payload["price"] = self.price
        if self.trigger_price is not None:
            payload["trigger_price"] = self.trigger_price
        if self.disclosed_quantity is not None:
            payload["disclosed_quantity"] = self.disclosed_quantity
        if self.tag is not None:
            payload["tag"] = self.tag
        return payload


@dataclass(frozen=True, slots=True)
class ZerodhaModifyOrderRequest:
    """Serializable order modification request."""

    order_id: str
    variety: str = "regular"
    quantity: int | None = None
    price: float | None = None
    trigger_price: float | None = None
    validity: str | None = None

    def __post_init__(self) -> None:
        if not self.order_id.strip() or not self.variety.strip():
            raise ValueError("order_id and variety must be non-empty")
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("quantity must be positive when provided")
        if self.price is not None and self.price <= 0.0:
            raise ValueError("price must be positive when provided")
        if self.trigger_price is not None and self.trigger_price <= 0.0:
            raise ValueError("trigger_price must be positive when provided")
        if self.validity is not None and not self.validity.strip():
            raise ValueError("validity must be non-empty when provided")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.quantity is not None:
            payload["quantity"] = self.quantity
        if self.price is not None:
            payload["price"] = self.price
        if self.trigger_price is not None:
            payload["trigger_price"] = self.trigger_price
        if self.validity is not None:
            payload["validity"] = self.validity
        return payload


@dataclass(frozen=True, slots=True)
class ZerodhaOrderResponse:
    """Serializable response envelope for order actions."""

    timestamp: datetime
    order_id: str
    status: str
    raw_response: dict[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.order_id.strip() or not self.status.strip():
            raise ValueError("order_id and status must be non-empty")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "order_id": self.order_id,
            "status": self.status,
            "raw_response": self.raw_response,
        }


@dataclass(frozen=True, slots=True)
class ZerodhaTickEvent:
    """Serializable tick event emitted from WebSocket callbacks."""

    timestamp: datetime
    instrument_token: int
    payload: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "instrument_token": self.instrument_token,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class ZerodhaOrderUpdateEvent:
    """Serializable order update emitted from WebSocket callbacks."""

    timestamp: datetime
    order_id: str
    status: str
    payload: dict[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.order_id.strip() or not self.status.strip():
            raise ValueError("order_id and status must be non-empty")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "order_id": self.order_id,
            "status": self.status,
            "payload": self.payload,
        }


@dataclass(slots=True)
class ZerodhaStreamState:
    """Serializable streaming state snapshot."""

    connected: bool = False
    subscribed_tokens: set[int] = field(default_factory=set)
    last_tick_timestamp: datetime | None = None
    last_order_update_timestamp: datetime | None = None
    reconnect_count: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "connected": self.connected,
            "subscribed_tokens": sorted(self.subscribed_tokens),
            "last_tick_timestamp": _ensure_utc(self.last_tick_timestamp).isoformat() if self.last_tick_timestamp else None,
            "last_order_update_timestamp": _ensure_utc(self.last_order_update_timestamp).isoformat() if self.last_order_update_timestamp else None,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }


class ZerodhaRestClient:
    """Async REST wrapper for Kite Connect using standard-library HTTP calls."""

    _BASE_URL = "https://api.kite.trade"

    def __init__(self, *, credentials: ZerodhaCredentials, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0.0:
            raise ZerodhaConfigurationError("timeout_seconds must be positive")
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._session: ZerodhaSession | None = None
        self._kite_client: Any | None = None

    @property
    def session(self) -> ZerodhaSession:
        if self._session is None:
            raise ZerodhaAuthenticationError("no authenticated Zerodha session is available")
        return self._session

    async def generate_session(self, *, request_token: str) -> ZerodhaSession:
        if not request_token.strip():
            raise ZerodhaAuthenticationError("request_token must be non-empty")
        if KiteConnect is not None:
            return await asyncio.to_thread(self._generate_session_with_sdk, request_token)
        return await asyncio.to_thread(self._generate_session_http, request_token)

    def _generate_session_with_sdk(self, request_token: str) -> ZerodhaSession:
        client = KiteConnect(api_key=self._credentials.api_key)
        response = client.generate_session(request_token, api_secret=self._credentials.api_secret)
        access_token = str(response.get("access_token", "")).strip()
        if not access_token:
            raise ZerodhaAuthenticationError("missing access token in session response")
        client.set_access_token(access_token)
        session = ZerodhaSession(
            access_token=access_token,
            public_token=_optional_str(response.get("public_token")),
            login_time=_utc_now(),
            refreshable=False,
            user_id=_optional_str(response.get("user_id")),
        )
        self._session = session
        self._kite_client = client
        return session

    def _generate_session_http(self, request_token: str) -> ZerodhaSession:
        payload = {
            "api_key": self._credentials.api_key,
            "request_token": request_token,
            "checksum": "",
        }
        raise ZerodhaAuthenticationError(
            "live session generation without pykiteconnect is not supported because Kite checksum signing is broker-specific"
        )

    async def place_order(self, *, request: ZerodhaOrderRequest) -> ZerodhaOrderResponse:
        response = await self._invoke_order_action(
            action="place",
            path=f"/orders/{request.variety}",
            payload=request.to_payload(),
        )
        order_id = _extract_order_id(response)
        return ZerodhaOrderResponse(
            timestamp=_utc_now(),
            order_id=order_id,
            status="PLACED",
            raw_response=response,
        )

    async def modify_order(self, *, request: ZerodhaModifyOrderRequest) -> ZerodhaOrderResponse:
        response = await self._invoke_order_action(
            action="modify",
            path=f"/orders/{request.variety}/{request.order_id}",
            payload=request.to_payload(),
            method="PUT",
        )
        order_id = _extract_order_id(response, fallback=request.order_id)
        return ZerodhaOrderResponse(
            timestamp=_utc_now(),
            order_id=order_id,
            status="MODIFIED",
            raw_response=response,
        )

    async def cancel_order(self, *, order_id: str, variety: str = "regular") -> ZerodhaOrderResponse:
        if not order_id.strip() or not variety.strip():
            raise ZerodhaRequestError("order_id and variety must be non-empty")
        response = await self._invoke_order_action(
            action="cancel",
            path=f"/orders/{variety}/{order_id}",
            payload={},
            method="DELETE",
        )
        return ZerodhaOrderResponse(
            timestamp=_utc_now(),
            order_id=order_id,
            status="CANCELLED",
            raw_response=response,
        )

    async def get_orders(self) -> list[dict[str, JsonValue]]:
        response = await self._request_json(method="GET", path="/orders")
        return _extract_data_list(response)

    async def get_positions(self) -> dict[str, JsonValue]:
        response = await self._request_json(method="GET", path="/portfolio/positions")
        return _extract_data_dict(response)

    async def get_margins(self, *, segment: str | None = None) -> dict[str, JsonValue]:
        path = "/user/margins"
        if segment is not None:
            if not segment.strip():
                raise ZerodhaRequestError("segment must be non-empty when provided")
            path = f"{path}/{segment}"
        response = await self._request_json(method="GET", path=path)
        return _extract_data_dict(response)

    async def get_instruments(self, *, exchange: str | None = None) -> list[dict[str, JsonValue]]:
        path = "/instruments"
        if exchange is not None:
            if not exchange.strip():
                raise ZerodhaRequestError("exchange must be non-empty when provided")
            path = f"/instruments/{exchange}"
        response = await self._request_text(method="GET", path=path)
        return [{"csv": response}]

    async def _invoke_order_action(
        self,
        *,
        action: str,
        path: str,
        payload: dict[str, Any],
        method: str = "POST",
    ) -> dict[str, JsonValue]:
        if self._kite_client is not None and KiteConnect is not None:
            return await asyncio.to_thread(self._invoke_order_action_with_sdk, action, path, payload, method)
        return await self._request_json(method=method, path=path, payload=payload)

    def _invoke_order_action_with_sdk(
        self,
        action: str,
        path: str,
        payload: dict[str, Any],
        method: str,
    ) -> dict[str, JsonValue]:
        client = self._kite_client
        if client is None:
            raise ZerodhaAuthenticationError("SDK client is not initialized")
        try:
            if action == "place":
                order_id = client.place_order(**payload)
                return {"status": "success", "data": {"order_id": order_id}}
            if action == "modify":
                order_id = client.modify_order(variety=path.split("/")[2], order_id=path.split("/")[-1], **payload)
                return {"status": "success", "data": {"order_id": order_id}}
            if action == "cancel":
                cancelled_order_id = client.cancel_order(variety=path.split("/")[2], order_id=path.split("/")[-1])
                return {"status": "success", "data": {"order_id": cancelled_order_id}}
        except Exception as exc:  # pragma: no cover - SDK boundary
            raise ZerodhaRequestError(f"Zerodha SDK {action} failed: {exc}") from exc
        raise ZerodhaRequestError(f"unsupported order action: {action}")

    async def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, JsonValue]:
        return await asyncio.to_thread(self._request_json_sync, method, path, payload)

    def _request_json_sync(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, JsonValue]:
        raw_text = self._request_sync(method=method, path=path, payload=payload)
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ZerodhaRequestError("failed to decode Zerodha JSON response") from exc
        if not isinstance(parsed, dict):
            raise ZerodhaRequestError("unexpected Zerodha JSON envelope type")
        return _ensure_json_dict(parsed)

    async def _request_text(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        return await asyncio.to_thread(self._request_sync, method, path, payload)

    def _request_sync(self, *, method: str, path: str, payload: dict[str, Any] | None) -> str:
        session = self.session
        encoded_payload: bytes | None = None
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {self._credentials.api_key}:{session.access_token}",
        }
        if payload:
            encoded_payload = urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(
            url=f"{self._BASE_URL}{path}",
            data=encoded_payload,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read().decode("utf-8")
                return body
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise ZerodhaRequestError(f"Zerodha HTTP error {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise ZerodhaRequestError(f"Zerodha network error: {exc}") from exc


class ZerodhaTickerBridge:
    """Async bridge for KiteTicker callbacks into asyncio queues."""

    def __init__(self, *, credentials: ZerodhaCredentials, session: ZerodhaSession) -> None:
        if KiteTicker is None:
            raise ZerodhaStreamError("pykiteconnect is required for Zerodha WebSocket streaming")
        self._credentials = credentials
        self._session = session
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ticker: Any | None = None
        self._thread: threading.Thread | None = None
        self._tick_queue: asyncio.Queue[ZerodhaTickEvent] = asyncio.Queue()
        self._order_update_queue: asyncio.Queue[ZerodhaOrderUpdateEvent] = asyncio.Queue()
        self._state = ZerodhaStreamState()
        self._close_event = threading.Event()
        self._started = False

    @property
    def state(self) -> ZerodhaStreamState:
        return ZerodhaStreamState(
            connected=self._state.connected,
            subscribed_tokens=set(self._state.subscribed_tokens),
            last_tick_timestamp=self._state.last_tick_timestamp,
            last_order_update_timestamp=self._state.last_order_update_timestamp,
            reconnect_count=self._state.reconnect_count,
            last_error=self._state.last_error,
        )

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run_ticker, name="zerodha-kiteticker", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not self._state.connected:
            if self._state.last_error is not None:
                raise ZerodhaStreamError(self._state.last_error)
            if time.monotonic() > deadline:
                raise ZerodhaStreamError("timed out while waiting for Zerodha ticker connection")
            await asyncio.sleep(0.05)
        self._started = True

    async def stop(self) -> None:
        self._close_event.set()
        ticker = self._ticker
        if ticker is not None:
            await asyncio.to_thread(self._close_ticker)
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 5.0)
        self._state.connected = False
        self._started = False

    async def subscribe(self, *, instrument_tokens: list[int], mode: str = "full") -> None:
        if not instrument_tokens:
            return
        if any(token <= 0 for token in instrument_tokens):
            raise ZerodhaStreamError("instrument tokens must be positive")
        ticker = self._require_ticker()
        await asyncio.to_thread(ticker.subscribe, instrument_tokens)
        await asyncio.to_thread(ticker.set_mode, mode.upper(), instrument_tokens)
        self._state.subscribed_tokens.update(instrument_tokens)

    async def unsubscribe(self, *, instrument_tokens: list[int]) -> None:
        if not instrument_tokens:
            return
        ticker = self._require_ticker()
        await asyncio.to_thread(ticker.unsubscribe, instrument_tokens)
        for token in instrument_tokens:
            self._state.subscribed_tokens.discard(token)

    async def get_tick(self) -> ZerodhaTickEvent:
        return await self._tick_queue.get()

    async def get_order_update(self) -> ZerodhaOrderUpdateEvent:
        return await self._order_update_queue.get()

    def _require_ticker(self) -> Any:
        if self._ticker is None:
            raise ZerodhaStreamError("ticker is not initialized")
        return self._ticker

    def _run_ticker(self) -> None:
        try:
            ticker = KiteTicker(self._credentials.api_key, self._session.access_token)
            self._ticker = ticker
            ticker.on_connect = self._on_connect
            ticker.on_close = self._on_close
            ticker.on_error = self._on_error
            ticker.on_ticks = self._on_ticks
            ticker.on_order_update = self._on_order_update
            ticker.on_reconnect = self._on_reconnect
            ticker.on_noreconnect = self._on_noreconnect
            ticker.connect(threaded=False)
        except Exception as exc:  # pragma: no cover - SDK boundary
            self._state.last_error = f"Zerodha ticker failed: {exc}"

    def _close_ticker(self) -> None:
        ticker = self._ticker
        if ticker is not None:
            try:
                ticker.close()
            except Exception as exc:  # pragma: no cover - SDK boundary
                self._state.last_error = f"ticker close failed: {exc}"

    def _on_connect(self, ws: Any, response: Any) -> None:
        del ws, response
        self._state.connected = True

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        del ws, code, reason
        self._state.connected = False

    def _on_error(self, ws: Any, code: Any, reason: Any) -> None:
        del ws, code
        self._state.last_error = str(reason)

    def _on_reconnect(self, ws: Any, attempts_count: int) -> None:
        del ws
        self._state.reconnect_count = attempts_count

    def _on_noreconnect(self, ws: Any) -> None:
        del ws
        self._state.last_error = "Zerodha ticker exhausted reconnect attempts"

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        del ws
        loop = self._loop
        if loop is None:
            self._state.last_error = "asyncio loop is not initialized"
            return
        for tick in ticks:
            token = int(tick.get("instrument_token", 0))
            if token <= 0:
                continue
            event = ZerodhaTickEvent(
                timestamp=_utc_now(),
                instrument_token=token,
                payload=_ensure_json_dict(tick),
            )
            self._state.last_tick_timestamp = event.timestamp
            loop.call_soon_threadsafe(self._tick_queue.put_nowait, event)

    def _on_order_update(self, ws: Any, data: dict[str, Any]) -> None:
        del ws
        loop = self._loop
        if loop is None:
            self._state.last_error = "asyncio loop is not initialized"
            return
        payload = _ensure_json_dict(data)
        event = ZerodhaOrderUpdateEvent(
            timestamp=_utc_now(),
            order_id=str(payload.get("order_id", "")),
            status=str(payload.get("status", "UNKNOWN")),
            payload=payload,
        )
        self._state.last_order_update_timestamp = event.timestamp
        loop.call_soon_threadsafe(self._order_update_queue.put_nowait, event)


class ZerodhaIntegrationService:
    """High-level async Zerodha integration facade for execution workflows."""

    def __init__(self, *, credentials: ZerodhaCredentials, timeout_seconds: float = 10.0) -> None:
        self._credentials = credentials
        self._rest_client = ZerodhaRestClient(credentials=credentials, timeout_seconds=timeout_seconds)
        self._ticker_bridge: ZerodhaTickerBridge | None = None

    @property
    def session(self) -> ZerodhaSession:
        return self._rest_client.session

    async def authenticate(self, *, request_token: str) -> ZerodhaSession:
        session = await self._rest_client.generate_session(request_token=request_token)
        return session

    async def connect_streams(self) -> None:
        session = self.session
        bridge = ZerodhaTickerBridge(credentials=self._credentials, session=session)
        await bridge.start()
        self._ticker_bridge = bridge

    async def disconnect_streams(self) -> None:
        if self._ticker_bridge is not None:
            await self._ticker_bridge.stop()
            self._ticker_bridge = None

    async def place_order(self, *, request: ZerodhaOrderRequest) -> ZerodhaOrderResponse:
        return await self._rest_client.place_order(request=request)

    async def modify_order(self, *, request: ZerodhaModifyOrderRequest) -> ZerodhaOrderResponse:
        return await self._rest_client.modify_order(request=request)

    async def cancel_order(self, *, order_id: str, variety: str = "regular") -> ZerodhaOrderResponse:
        return await self._rest_client.cancel_order(order_id=order_id, variety=variety)

    async def get_orders(self) -> list[dict[str, JsonValue]]:
        return await self._rest_client.get_orders()

    async def get_positions(self) -> dict[str, JsonValue]:
        return await self._rest_client.get_positions()

    async def get_margins(self, *, segment: str | None = None) -> dict[str, JsonValue]:
        return await self._rest_client.get_margins(segment=segment)

    async def subscribe_market_data(self, *, instrument_tokens: list[int], mode: str = "full") -> None:
        bridge = self._require_ticker_bridge()
        await bridge.subscribe(instrument_tokens=instrument_tokens, mode=mode)

    async def unsubscribe_market_data(self, *, instrument_tokens: list[int]) -> None:
        bridge = self._require_ticker_bridge()
        await bridge.unsubscribe(instrument_tokens=instrument_tokens)

    async def next_tick(self) -> ZerodhaTickEvent:
        bridge = self._require_ticker_bridge()
        return await bridge.get_tick()

    async def next_order_update(self) -> ZerodhaOrderUpdateEvent:
        bridge = self._require_ticker_bridge()
        return await bridge.get_order_update()

    def stream_state(self) -> dict[str, JsonValue]:
        if self._ticker_bridge is None:
            return ZerodhaStreamState().to_dict()
        return self._ticker_bridge.state.to_dict()

    def _require_ticker_bridge(self) -> ZerodhaTickerBridge:
        if self._ticker_bridge is None:
            raise ZerodhaStreamError("Zerodha ticker bridge is not connected")
        return self._ticker_bridge



def _extract_order_id(response: dict[str, JsonValue], fallback: str | None = None) -> str:
    data = response.get("data")
    if isinstance(data, dict):
        order_id = data.get("order_id")
        if isinstance(order_id, str) and order_id.strip():
            return order_id
    if fallback is not None and fallback.strip():
        return fallback
    raise ZerodhaRequestError("order_id missing from Zerodha response")



def _extract_data_list(response: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    data = response.get("data")
    if not isinstance(data, list):
        raise ZerodhaRequestError("expected data list in Zerodha response")
    return [_ensure_json_dict(item) for item in data]



def _extract_data_dict(response: dict[str, JsonValue]) -> dict[str, JsonValue]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ZerodhaRequestError("expected data object in Zerodha response")
    return _ensure_json_dict(data)



def _ensure_json_dict(value: Any) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ZerodhaRequestError("expected dictionary-compatible JSON value")
    return {str(key): _to_json_value(item) for key, item in value.items()}



def _to_json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_value(item) for item in value]
    return str(value)



def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None



def _ensure_utc(value: datetime | None) -> datetime:
    if value is None:
        return _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "ZerodhaAuthenticationError",
    "ZerodhaConfigurationError",
    "ZerodhaCredentials",
    "ZerodhaIntegrationError",
    "ZerodhaIntegrationService",
    "ZerodhaModifyOrderRequest",
    "ZerodhaOrderRequest",
    "ZerodhaOrderResponse",
    "ZerodhaOrderUpdateEvent",
    "ZerodhaRequestError",
    "ZerodhaRestClient",
    "ZerodhaSession",
    "ZerodhaStreamError",
    "ZerodhaStreamState",
    "ZerodhaTickEvent",
    "ZerodhaTickerBridge",
]
