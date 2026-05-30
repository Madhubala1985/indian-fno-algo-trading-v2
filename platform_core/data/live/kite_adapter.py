from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal

from kiteconnect import KiteConnect
from kiteconnect import KiteTicker

from platform_core.data.option_chain_ingestion import (
    OptionChainSnapshot,
    OptionContractSnapshot,
)


CONFIG_PATH = "configs/kite_config.json"

# NFO instrument master is ~3 MB; cache for 5 minutes
_INSTRUMENTS_CACHE_TTL: float = 300.0


class KiteAdapter:

    def __init__(self) -> None:

        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)

        self.api_key = config["api_key"]
        self.access_token = config["access_token"]

        self.kite = KiteConnect(api_key=self.api_key)
        self.kite.set_access_token(self.access_token)

        self.ticker = KiteTicker(self.api_key, self.access_token)

        self._tick_callbacks: list[Callable] = []
        self._connected = False

        # Instrument master cache — keyed by exchange
        self._instruments_cache: list[dict] | None = None
        self._instruments_cache_ts: float = 0.0
        self._instruments_exchange: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def add_tick_callback(self, callback: Callable) -> None:
        self._tick_callbacks.append(callback)

    def connect(self, *, instrument_tokens: list[int]) -> None:

        def on_connect(ws, response):
            print("\n[KITE] WebSocket connected.")
            self._connected = True
            ws.subscribe(instrument_tokens)
            ws.set_mode(ws.MODE_FULL, instrument_tokens)
            print(f"[KITE] Subscribed: {instrument_tokens}")

        def on_ticks(ws, ticks):
            for callback in self._tick_callbacks:
                callback(ticks)

        def on_close(ws, code, reason):
            self._connected = False
            print(f"\n[KITE] Closed: {code} {reason}")

        def on_error(ws, code, reason):
            print(f"\n[KITE] Error: {code} {reason}")

        self.ticker.on_connect = on_connect
        self.ticker.on_ticks = on_ticks
        self.ticker.on_close = on_close
        self.ticker.on_error = on_error

        thread = threading.Thread(
            target=self.ticker.connect,
            kwargs={"threaded": True},
            daemon=True,
        )
        thread.start()

        timeout = 10
        start = time.time()
        while not self._connected:
            if time.time() - start > timeout:
                raise TimeoutError("Kite websocket connection timeout.")
            time.sleep(0.1)

    def disconnect(self) -> None:
        self.ticker.close()
        self._connected = False

    def fetch_option_chain(
        self,
        *,
        underlying: str,
        expiry_date: date,
        spot_price: float,
        lot_size: int,
        exchange: str = "NFO",
        strikes_around_atm: int = 10,
        strike_step: float = 100.0,
    ) -> OptionChainSnapshot:
        """
        Fetch a live option chain from Kite and return a canonical
        OptionChainSnapshot consumed by OptionsContextBuilder.

        Calls kite.instruments(exchange) once per 5 minutes (cached),
        then kite.quote() for the filtered set of contracts.
        Greeks are absent from Kite quotes and will be computed by
        OptionsContextBuilder.GreekResolver via BSM.
        """
        now = time.time()
        if (
            self._instruments_cache is None
            or self._instruments_exchange != exchange
            or (now - self._instruments_cache_ts) >= _INSTRUMENTS_CACHE_TTL
        ):
            self._instruments_cache = self.kite.instruments(exchange)
            self._instruments_cache_ts = now
            self._instruments_exchange = exchange

        # Filter to target underlying, expiry, option type, and ATM ± N strikes
        atm = round(spot_price / strike_step) * strike_step
        min_strike = atm - strikes_around_atm * strike_step
        max_strike = atm + strikes_around_atm * strike_step

        eligible = [
            inst for inst in self._instruments_cache
            if inst["name"] == underlying
            and inst["expiry"] == expiry_date
            and inst["instrument_type"] in ("CE", "PE")
            and min_strike <= float(inst["strike"]) <= max_strike
        ]

        if not eligible:
            raise ValueError(
                f"No option instruments found for {underlying} "
                f"expiry={expiry_date} "
                f"strikes=[{min_strike:.0f}, {max_strike:.0f}]"
            )

        # Kite quote() accepts at most 500 symbols per call
        symbols = [f"{exchange}:{inst['tradingsymbol']}" for inst in eligible]
        quotes: dict[str, dict] = {}
        for i in range(0, len(symbols), 500):
            quotes.update(self.kite.quote(symbols[i : i + 500]))

        now_utc = datetime.now(timezone.utc)
        contracts: list[OptionContractSnapshot] = []

        for inst in eligible:
            sym_key = f"{exchange}:{inst['tradingsymbol']}"
            q = quotes.get(sym_key, {})

            depth = q.get("depth", {})
            buy_levels = depth.get("buy", [])
            sell_levels = depth.get("sell", [])

            bid_raw = buy_levels[0].get("price") if buy_levels else None
            ask_raw = sell_levels[0].get("price") if sell_levels else None
            bid_qty = int(buy_levels[0]["quantity"]) if buy_levels and buy_levels[0].get("quantity") else None
            ask_qty = int(sell_levels[0]["quantity"]) if sell_levels and sell_levels[0].get("quantity") else None

            ltp_raw = q.get("last_price")
            oi_raw = q.get("oi")
            vol_raw = q.get("volume")

            option_side = "CALL" if inst["instrument_type"] == "CE" else "PUT"

            contracts.append(
                OptionContractSnapshot(
                    contract_symbol=inst["tradingsymbol"],
                    underlying_symbol=underlying,
                    exchange=exchange,
                    expiry_date=expiry_date,
                    strike_price=Decimal(str(inst["strike"])),
                    option_side=option_side,
                    event_timestamp=now_utc,
                    received_timestamp=now_utc,
                    last_traded_price=Decimal(str(ltp_raw)) if ltp_raw is not None else None,
                    bid_price=Decimal(str(bid_raw)) if bid_raw is not None else None,
                    ask_price=Decimal(str(ask_raw)) if ask_raw is not None else None,
                    bid_quantity=bid_qty,
                    ask_quantity=ask_qty,
                    traded_volume=int(vol_raw) if vol_raw is not None else None,
                    open_interest=int(oi_raw) if oi_raw is not None else None,
                    source="kite_live",
                )
            )

        return OptionChainSnapshot(
            chain_id=f"{underlying}|{expiry_date.isoformat()}|live",
            underlying_symbol=underlying,
            expiry_date=expiry_date,
            exchange=exchange,
            event_timestamp=now_utc,
            received_timestamp=now_utc,
            contracts=tuple(contracts),
            # sequence_number bounded to int32 max to stay storage-safe
            sequence_number=int(now * 1000) % (2 ** 31),
            update_kind="SNAPSHOT",
            source="kite_live",
        )

    def check_session(self) -> tuple[bool, str]:
        """
        Verify the current access token with a lightweight profile() call.
        Returns (is_valid, error_message).
        Raises no exceptions — all errors are captured in the return value.
        """
        try:
            self.kite.profile()
            return True, ""
        except Exception as exc:
            return False, str(exc)
