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
    """
    Kite REST + WebSocket adapter with bounded automatic reconnection.

    Reconnect behaviour:
      On any non-explicit disconnect, up to _MAX_RECONNECTS attempts are made
      with exponential backoff (2 s → 4 → 8 → 16 → 32, capped at 60 s).
      Each successful on_connect resubscribes to _instrument_tokens and resets
      the attempt counter. Calling disconnect() sets _stopping=True which
      suppresses reconnect scheduling.
    """

    _MAX_RECONNECTS:       int   = 5
    _RECONNECT_BASE_DELAY: float = 2.0    # doubles each attempt, capped at 60 s

    def __init__(self) -> None:

        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)

        self.api_key = config["api_key"]
        self.access_token = config["access_token"]

        self.kite = KiteConnect(api_key=self.api_key)
        self.kite.set_access_token(self.access_token)

        # ticker is created in _do_connect(); None before first connect()
        self.ticker: KiteTicker | None = None

        self._tick_callbacks: list[Callable] = []
        self._connected = False

        # Instrument master cache — keyed per exchange so NFO and BFO never
        # evict each other.  Each entry is (instruments_list, fetch_timestamp).
        self._instruments_cache: dict[str, tuple[list[dict], float]] = {}

        # Reconnect state
        self._instrument_tokens: list[int] = []
        self._reconnect_count:   int       = 0
        self._reconnecting:      bool      = False
        self._reconnect_lock               = threading.Lock()
        self._stopping:          bool      = False

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    def add_tick_callback(self, callback: Callable) -> None:
        self._tick_callbacks.append(callback)

    def connect(self, *, instrument_tokens: list[int]) -> None:
        """
        Connect the WebSocket and subscribe to instrument_tokens.
        Blocks until the first successful connection or raises TimeoutError.
        Reconnect attempts run in background threads; this method returns
        after the initial connect.
        """
        self._instrument_tokens = list(instrument_tokens)
        self._stopping = False
        self._reconnect_count = 0
        self._do_connect()

        timeout = 10.0
        start = time.time()
        while not self._connected:
            if time.time() - start > timeout:
                raise TimeoutError("Kite WebSocket initial connection timeout (10 s).")
            time.sleep(0.1)

    def disconnect(self) -> None:
        """Explicitly close the WebSocket. Suppresses automatic reconnect."""
        self._stopping = True
        if self.ticker is not None:
            self.ticker.close()
        self._connected = False

    # ── Internal connection machinery ─────────────────────────────────────────

    def _do_connect(self) -> None:
        """
        Create a fresh KiteTicker, register callbacks, and start the connection
        thread. Closes any existing ticker first.
        """
        # Close stale ticker cleanly before replacing it
        if self.ticker is not None:
            try:
                self.ticker.close()
            except Exception:
                pass

        self.ticker = KiteTicker(self.api_key, self.access_token)

        def on_connect(ws, response) -> None:
            self._connected = True
            self._reconnect_count = 0           # reset on every successful connect
            ws.subscribe(self._instrument_tokens)
            ws.set_mode(ws.MODE_FULL, self._instrument_tokens)
            print(
                f"\n[KITE] WebSocket connected.  "
                f"Subscribed {len(self._instrument_tokens)} token(s): "
                f"{self._instrument_tokens}"
            )

        def on_ticks(ws, ticks) -> None:
            for callback in self._tick_callbacks:
                callback(ticks)

        def on_close(ws, code, reason) -> None:
            self._connected = False
            if self._stopping:
                print(f"\n[KITE] Disconnected (operator request): {code} {reason}")
                return
            print(f"\n[KITE] Connection lost: code={code}  reason={reason!r}")
            self._schedule_reconnect()

        def on_error(ws, code, reason) -> None:
            print(f"\n[KITE] Error: code={code}  reason={reason!r}")

        self.ticker.on_connect = on_connect
        self.ticker.on_ticks   = on_ticks
        self.ticker.on_close   = on_close
        self.ticker.on_error   = on_error

        thread = threading.Thread(
            target=self.ticker.connect,
            kwargs={"threaded": True},
            daemon=True,
        )
        thread.start()

    def _schedule_reconnect(self) -> None:
        """
        Schedule one reconnect attempt, with storm protection via a lock and
        the _reconnecting flag. Returns immediately; reconnect runs in a
        background thread.
        """
        with self._reconnect_lock:
            if self._stopping or self._reconnecting:
                return

            self._reconnect_count += 1

            if self._reconnect_count > self._MAX_RECONNECTS:
                print(
                    f"\n{'='*60}\n"
                    f"[KITE] Max reconnects ({self._MAX_RECONNECTS}) exceeded.\n"
                    f"  The WebSocket cannot be restored automatically.\n"
                    f"  Check: network connectivity, access token expiry.\n"
                    f"  Action: run scripts/generate_kite_session.py and restart.\n"
                    f"{'='*60}"
                )
                return

            delay = min(
                self._RECONNECT_BASE_DELAY * (2 ** (self._reconnect_count - 1)),
                60.0,
            )
            print(
                f"\n[KITE] Reconnect {self._reconnect_count}/{self._MAX_RECONNECTS} "
                f"scheduled in {delay:.0f} s"
            )
            self._reconnecting = True

        thread = threading.Thread(
            target=self._reconnect_after_delay,
            args=(self._reconnect_count, delay),
            daemon=True,
        )
        thread.start()

    def _reconnect_after_delay(self, attempt: int, delay: float) -> None:
        """Sleep for delay, then attempt to reconnect and resubscribe."""
        time.sleep(delay)

        with self._reconnect_lock:
            self._reconnecting = False

        if self._stopping:
            return

        print(f"\n[KITE] Attempting reconnect {attempt}/{self._MAX_RECONNECTS}…")
        try:
            self._do_connect()

            # Wait for on_connect to fire (up to 15 s)
            start = time.time()
            while not self._connected and time.time() - start < 15.0:
                time.sleep(0.1)

            if self._connected:
                print(
                    f"[KITE] Reconnect {attempt} succeeded.  "
                    f"Subscriptions restored for {len(self._instrument_tokens)} token(s)."
                )
            else:
                print(f"[KITE] Reconnect {attempt} timed out — scheduling next attempt.")
                self._schedule_reconnect()

        except Exception as exc:
            print(f"[KITE] Reconnect {attempt} raised exception: {exc}")
            self._schedule_reconnect()

    # ── REST helpers ───────────────────────────────────────────────────────────

    def _get_instruments(self, exchange: str) -> list[dict]:
        """
        Return the Kite instrument master for `exchange`, using a per-exchange
        5-minute cache.  NFO and BFO are cached independently so fetching one
        never evicts the other.
        """
        now = time.time()
        cached = self._instruments_cache.get(exchange)
        if cached is None or (now - cached[1]) >= _INSTRUMENTS_CACHE_TTL:
            instruments = self.kite.instruments(exchange)
            self._instruments_cache[exchange] = (instruments, now)
        return self._instruments_cache[exchange][0]

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
        instruments = self._get_instruments(exchange)

        # Filter to target underlying, expiry, option type, and ATM ± N strikes
        atm = round(spot_price / strike_step) * strike_step
        min_strike = atm - strikes_around_atm * strike_step
        max_strike = atm + strikes_around_atm * strike_step

        eligible = [
            inst for inst in instruments
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
            oi_raw  = q.get("oi")
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

    def nearest_expiry(
        self,
        underlying: str,
        from_date: date,
        *,
        exchange: str = "NFO",
    ) -> date:
        """
        Return the nearest available option expiry date for `underlying` on or
        after `from_date`, discovered from the broker's instrument master.

        Uses the same 5-minute instrument cache as fetch_option_chain().
        Raises ValueError if no expiry is available (e.g. token expired, market
        not yet listed contracts).
        """
        instruments = self._get_instruments(exchange)
        expiries = sorted(
            {
                inst["expiry"]
                for inst in instruments
                if inst["name"] == underlying
                and inst["instrument_type"] in ("CE", "PE")
                and inst["expiry"] >= from_date
            }
        )
        if not expiries:
            raise ValueError(
                f"No option expiry found for {underlying} on or after {from_date} "
                f"in {exchange} instrument master."
            )
        return expiries[0]

    def resolve_option_metadata(
        self,
        *,
        underlying: str,
        exchange: str,
        reference_lot_size: int | None = None,
        reference_strike_step: float | None = None,
    ) -> dict:
        """
        Query the Kite instrument master and return metadata for `underlying`.

        Returns a dict with:
          num_option_instruments  — total CE+PE instruments in master
          nearest_3_expiries      — up to 3 nearest available expiries
          observed_lot_sizes      — set of lot_size values seen in master
          inferred_strike_step    — minimum gap between consecutive strikes
          sample_ce               — first CE tradingsymbol found (or None)
          sample_pe               — first PE tradingsymbol found (or None)
          lot_size_matches_ref    — True/None (None when reference_lot_size absent)
          strike_step_matches_ref — True/None (None when reference_strike_step absent)
          warnings                — list of human-readable warning strings

        Never raises — on any failure returns a dict with 'error' key.
        """
        try:
            instruments = self._get_instruments(exchange)
        except Exception as exc:
            return {"error": f"instrument master fetch failed: {exc}"}

        today = date.today()
        option_insts = [
            inst for inst in instruments
            if inst.get("name") == underlying
            and inst.get("instrument_type") in ("CE", "PE")
        ]

        if not option_insts:
            return {
                "error": f"no CE/PE instruments found for {underlying} in {exchange}",
                "num_option_instruments": 0,
            }

        # Available expiries (future only)
        all_expiries = sorted({
            inst["expiry"] for inst in option_insts if inst["expiry"] >= today
        })
        nearest_3 = all_expiries[:3]

        # Lot sizes seen in master
        observed_lot_sizes: set[int] = {
            int(inst["lot_size"]) for inst in option_insts if inst.get("lot_size")
        }

        # Infer strike step from nearest expiry
        inferred_step: float | None = None
        if nearest_3:
            near_expiry = nearest_3[0]
            strikes = sorted({
                float(inst["strike"])
                for inst in option_insts
                if inst["expiry"] == near_expiry
            })
            if len(strikes) >= 2:
                gaps = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
                min_gap = min(gaps)
                inferred_step = min_gap if min_gap > 0 else None

        # Sample symbols
        sample_ce = next(
            (inst["tradingsymbol"] for inst in option_insts if inst["instrument_type"] == "CE"),
            None,
        )
        sample_pe = next(
            (inst["tradingsymbol"] for inst in option_insts if inst["instrument_type"] == "PE"),
            None,
        )

        # Warnings
        warnings: list[str] = []
        lot_matches: bool | None = None
        step_matches: bool | None = None

        if reference_lot_size is not None and observed_lot_sizes:
            lot_matches = reference_lot_size in observed_lot_sizes
            if not lot_matches:
                warnings.append(
                    f"CONSTANT_LOT_MISMATCH: reference={reference_lot_size} "
                    f"master={sorted(observed_lot_sizes)}"
                )

        if reference_strike_step is not None and inferred_step is not None:
            step_matches = abs(inferred_step - reference_strike_step) < 1e-6
            if not step_matches:
                warnings.append(
                    f"CONSTANT_STEP_MISMATCH: reference={reference_strike_step} "
                    f"inferred={inferred_step}"
                )

        return {
            "underlying": underlying,
            "exchange": exchange,
            "num_option_instruments": len(option_insts),
            "nearest_3_expiries": [e.isoformat() for e in nearest_3],
            "observed_lot_sizes": sorted(observed_lot_sizes),
            "inferred_strike_step": inferred_step,
            "sample_ce": sample_ce,
            "sample_pe": sample_pe,
            "lot_size_matches_ref": lot_matches,
            "strike_step_matches_ref": step_matches,
            "warnings": warnings,
        }

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
