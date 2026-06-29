from __future__ import annotations

import json
import logging as _logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal

from kiteconnect import KiteConnect
from kiteconnect import KiteTicker


class _SuppressNullKiteClose(_logging.Filter):
    """Suppress spurious 'Connection closed: None - None' from pykiteconnect.

    pykiteconnect/ticker.py always logs this via log.error() on clean teardown
    (code=None, reason=None). Real disconnects have non-None codes and are also
    surfaced by our own on_close callback, so suppressing only the null variant
    loses nothing diagnostically.
    """

    def filter(self, record: _logging.LogRecord) -> bool:
        return record.getMessage() != "Connection closed: None - None"


_logging.getLogger("kiteconnect.ticker").addFilter(_SuppressNullKiteClose())

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

    _MAX_RECONNECTS:       int   = 10
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

        # Tick-staleness watchdog: monotonic timestamp of the last tick batch
        # received.  A silent WebSocket stall (socket stays open but no data
        # flows) does NOT fire on_close, so the reconnect machinery above never
        # triggers.  An external watchdog polls seconds_since_last_tick() and
        # calls force_reconnect() when the feed goes quiet during market hours.
        self._last_tick_ts: float = 0.0

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

        # Retry the initial connect: a transient handshake timeout (code 1006,
        # "peer did not finish the opening handshake in time") at startup must
        # not hard-fail the entire pipeline before the market run even begins.
        _attempts = 3
        for _attempt in range(1, _attempts + 1):
            self._do_connect()
            start = time.time()
            while not self._connected and time.time() - start < 10.0:
                time.sleep(0.1)
            if self._connected:
                return
            print(
                f"[KITE] Initial connect attempt {_attempt}/{_attempts} "
                f"timed out (10 s)" + (" — retrying…" if _attempt < _attempts else ".")
            )
        raise TimeoutError(
            f"Kite WebSocket initial connection failed after {_attempts} attempts (10 s each)."
        )

    def disconnect(self) -> None:
        """Explicitly close the WebSocket. Suppresses automatic reconnect."""
        self._stopping = True
        if self.ticker is not None:
            self.ticker.close()
        self._connected = False

    def seconds_since_last_tick(self) -> float:
        """
        Seconds since the last tick batch arrived.  Returns a large sentinel
        (inf) before the first tick so callers can distinguish "never ticked"
        from "ticked recently".  Used by the staleness watchdog to detect a
        silent stall that does not fire on_close.
        """
        if self._last_tick_ts <= 0.0:
            return float("inf")
        return time.time() - self._last_tick_ts

    def force_reconnect(self, reason: str = "") -> None:
        """
        Force a WebSocket reconnect when the feed has gone silent without an
        on_close event (half-open socket).  Marks the connection down and
        schedules the normal bounded-backoff reconnect, which closes the stale
        ticker and opens a fresh one.  No-op if shutting down or a reconnect is
        already in flight.
        """
        if self._stopping or self._reconnecting:
            return
        print(f"\n[KITE] Forcing reconnect — {reason or 'stale feed'}")
        self._connected = False
        # Reset the tick clock so the watchdog does not re-fire while the new
        # connection is being established.
        self._last_tick_ts = time.time()
        # Give the recovery a FRESH reconnect budget. Without this, once the
        # adapter exhausted _MAX_RECONNECTS (e.g. a long network outage) it would
        # stay permanently dead: every subsequent _schedule_reconnect() would
        # no-op on the maxed counter, and the stall watchdog could never revive
        # the feed. Resetting here guarantees the feed is always recoverable
        # during market hours (the watchdog's own cooldown prevents spamming).
        self._reconnect_count = 0
        self._schedule_reconnect()

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
            self._last_tick_ts = time.time()    # baseline so watchdog waits for a real gap
            ws.subscribe(self._instrument_tokens)
            ws.set_mode(ws.MODE_FULL, self._instrument_tokens)
            print(
                f"\n[KITE] WebSocket connected.  "
                f"Subscribed {len(self._instrument_tokens)} token(s): "
                f"{self._instrument_tokens}"
            )

        def on_ticks(ws, ticks) -> None:
            # Stamp arrival time first so the staleness watchdog sees liveness
            # even if a downstream callback raises.
            self._last_tick_ts = time.time()
            for callback in self._tick_callbacks:
                callback(ticks)

        def on_close(ws, code, reason) -> None:
            # Ignore callbacks from a stale ticker we already replaced during a
            # reconnect: only the current self.ticker's lifecycle is live. The
            # socket we just closed fires on_close *late* (async teardown), after
            # its replacement is already connected — acting on it would flip the
            # healthy connection's state and schedule a redundant reconnect,
            # producing a self-perpetuating connect->close->reconnect storm.
            if ws is not self.ticker:
                return
            if self._stopping:
                self._connected = False
                print(f"\n[KITE] Disconnected (operator request): {code} {reason}")
                return
            # A null-code close is pykiteconnect's spurious clean teardown, not a
            # real network drop (those carry a non-None code, e.g. 1006). Genuine
            # silent stalls are caught separately by the staleness watchdog via
            # force_reconnect(), so a code=None here is never a real disconnect.
            if code is None:
                return
            self._connected = False
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

    def fetch_ltp(self, *, exchange: str, tradingsymbol: str) -> float | None:
        """
        Real last-traded price for a single option contract via kite.quote().

        Used to mark open paper positions to the LIVE market price at exit
        instead of a Black-Scholes recompute. Returns None on any error or when
        no price is available, so callers fall back to the BSM mark.
        """
        try:
            key = f"{exchange}:{tradingsymbol}"
            q = self.kite.quote([key])
            rec = q.get(key, {})
            ltp = rec.get("last_price")
            if ltp is None:
                return None
            ltp = float(ltp)
            return ltp if ltp > 0.0 else None
        except Exception:
            return None

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
