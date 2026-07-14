"""
futures_sleeve.py — DRY-RUN index-futures trading sleeve (2026-07-14, user directive:
futures AND options, all indices; monetize days options can't — chop, slow recoveries — with
no theta and symmetric direction).

Runs INSIDE the live pipeline but fully self-contained: marked to the REAL front-month futures
ticks already subscribed for order flow. Never places orders (paper only), never touches the
options path — every public method swallows its own exceptions.

Entries (each tagged, so the two hypotheses are separable at evaluation):
  * MIRROR — the options path fired an entry → same direction in futures, 1 lot. Head-to-head:
    same signal expressed in futures (linear, no theta) vs options.
  * TURN   — the constituent-flow TURN event (crossasset_shadow.LAST_TURN, the signal that
    called the 07-13 low) → 1 lot in the turn direction. The adaptive sleeve options can't take
    (fires when structure/MCB still lags). Cooldown + daily caps below.

Exits: hard stop −0.30% adverse; breakeven ratchet arms at +0.30% (winner can't become loser);
opposite flow-turn (adaptive exit); EOD flat 15:20 IST. Risk: ≈₹5-8k/lot at the stop ≈ 1.3-2%
of ₹4L — consistent with the options max-loss cap. Margin model: 11.5% of notional per lot,
total ≤ 90% capital, one position per index. Sleeve daily-loss halt at −2% of capital.

Costs (futures, per round trip): brokerage ₹20/side + STT 0.02% (sell) + exch txn 0.00173%
(both) + 18% GST on brokerage+txn + stamp 0.002% (buy). Slippage: 0.1 index pts per side
embedded in fills. State persists to logs/futures_sleeve_state.json (crash-safe recovery);
every event appends to logs/futures_sleeve.jsonl.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_IST = timezone(timedelta(hours=5, minutes=30))

def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Rails loosened 2026-07-15 (user directive: widen and observe the win rate —
# paper book, that's what it's for). Each is env-tunable; the pre-07-15 values
# are noted so any knob reverts independently.
STOP_PCT = _envf("FUT_SLEEVE_STOP_PCT", 0.0050)              # was 0.0030
RATCHET_ARM_PCT = _envf("FUT_SLEEVE_RATCHET_PCT", 0.0030)
SLIPPAGE_PTS = 0.10        # per side, embedded in fill price
MARGIN_FRAC = 0.115        # ≈ SPAN+exposure for index futures — REAL exchange
                           # margin, not a tunable rail; do not loosen.
MAX_MARGIN_UTIL = _envf("FUT_SLEEVE_MAX_MARGIN_UTIL", 1.00)  # was 0.90; 1.0 =
                           # full capital as margin. >1.0 would be unfundable
                           # at the broker — the cap models a real account.
DAILY_LOSS_HALT_FRAC = _envf("FUT_SLEEVE_DAILY_HALT", 0.04)  # was 0.02
# Post-exit re-entry cooldown per index (was hardcoded 2700s = 45 min).
# 0 disables (2026-07-15 user directive: removed).
REENTRY_COOLDOWN_SEC = _envf("FUT_SLEEVE_REENTRY_COOLDOWN_SEC", 0.0)
TURN_COOLDOWN_SEC = 1800   # ≥30 min between TURN entries per index
MAX_TURN_ENTRIES_PER_DAY = 3
EOD_FLAT_IST = (15, 20)


def _costs(entry_px: float, exit_px: float, lot_size: int) -> float:
    buy_notional = entry_px * lot_size
    sell_notional = exit_px * lot_size
    brokerage = 40.0
    stt = 0.0002 * sell_notional
    txn = 0.0000173 * (buy_notional + sell_notional)
    gst = 0.18 * (brokerage + txn)
    stamp = 0.00002 * buy_notional
    return brokerage + stt + txn + gst + stamp


class FuturesSleeve:
    def __init__(self, *, capital: float,
                 contracts: dict,   # fut_sym -> {"underlying": str, "lot_size": int}
                 out_path: str = "logs/futures_sleeve.jsonl",
                 state_path: str = "logs/futures_sleeve_state.json") -> None:
        self._capital = float(capital)
        self._contracts = dict(contracts)
        self._out = Path(out_path)
        self._state_path = Path(state_path)
        self._pos: dict = {}            # fut_sym -> position dict
        self._ltp: dict = {}            # fut_sym -> last price
        self._day = datetime.now(_IST).date().isoformat()
        self._day_pnl = 0.0
        self._halted = False
        self._turn_seen: dict = {}      # underlying -> last processed turn epoch
        self._turn_last_entry: dict = {}  # underlying -> epoch of last TURN entry
        self._turn_count: dict = {}     # underlying -> TURN entries today
        self._sig_count: dict = {}      # fut_sym -> direction-signal entries today
        self._last_exit_epoch: dict = {}  # fut_sym -> epoch of last exit (re-entry cooldown)
        self._load_state()

    # ── integration points ────────────────────────────────────────────────────
    def on_tick(self, fut_sym: str, ltp: float, *, now=None) -> None:
        """Mark price; run exits; poll TURN entries. O(1); NEVER raises."""
        try:
            if not ltp or ltp <= 0:
                return
            self._roll_day()
            self._ltp[fut_sym] = float(ltp)
            now = now or datetime.now(_IST)
            self._manage_exits(fut_sym, float(ltp), now)
            self._poll_turn_entries(now)
        except Exception:
            pass

    def on_option_entry(self, underlying: str, direction: str, *, now=None) -> None:
        """MIRROR: the options path just entered — express the same signal in futures."""
        try:
            self._roll_day()
            fut_sym = self._fut_for(underlying)
            if fut_sym:
                self._enter(fut_sym, direction, tag="mirror", now=now)
        except Exception:
            pass

    def on_direction_signal(self, underlying: str, direction: str, *, now=None) -> None:
        """DIRECTION signal from the live pipeline (all directional gates green) — the futures
        entry path that does NOT depend on option strike selection (07-14 fix). Per-bar caller,
        so rails here: one position per index (in _enter), ≤2 signal entries/index/day, and an
        optional post-exit re-entry cooldown (REENTRY_COOLDOWN_SEC; 0 = off since 2026-07-15)."""
        try:
            self._roll_day()
            fut_sym = self._fut_for(underlying)
            if not fut_sym or fut_sym in self._pos:
                return
            if self._sig_count.get(fut_sym, 0) >= 2:
                return
            if (REENTRY_COOLDOWN_SEC > 0
                    and time.time() - self._last_exit_epoch.get(fut_sym, 0.0) < REENTRY_COOLDOWN_SEC):
                return
            if self._enter(fut_sym, direction, tag="signal", now=now):
                self._sig_count[fut_sym] = self._sig_count.get(fut_sym, 0) + 1
        except Exception:
            pass

    # ── entries ───────────────────────────────────────────────────────────────
    def _poll_turn_entries(self, now) -> None:
        try:
            from platform_core.data.live.crossasset_shadow import LAST_TURN
        except Exception:
            return
        for und, ev in list(LAST_TURN.items()):
            epoch = ev.get("epoch", 0.0)
            if epoch <= self._turn_seen.get(und, 0.0):
                continue
            self._turn_seen[und] = epoch
            if time.time() - epoch > 180:      # stale event (>3 min) — don't chase
                continue
            if self._turn_count.get(und, 0) >= MAX_TURN_ENTRIES_PER_DAY:
                continue
            if time.time() - self._turn_last_entry.get(und, 0.0) < TURN_COOLDOWN_SEC:
                continue
            fut_sym = self._fut_for(und)
            direction = ev.get("to")
            if fut_sym and direction in ("LONG", "SHORT"):
                if self._enter(fut_sym, direction, tag="turn", now=now):
                    self._turn_last_entry[und] = time.time()
                    self._turn_count[und] = self._turn_count.get(und, 0) + 1

    def _enter(self, fut_sym: str, direction: str, *, tag: str, now=None) -> bool:
        if self._halted or direction not in ("LONG", "SHORT"):
            return False
        if fut_sym in self._pos:                      # one position per index
            return False
        now = now or datetime.now(_IST)
        if (now.hour, now.minute) >= EOD_FLAT_IST or (now.hour, now.minute) < (9, 16):
            return False
        ltp = self._ltp.get(fut_sym)
        if not ltp:
            return False
        meta = self._contracts.get(fut_sym) or {}
        lot = int(meta.get("lot_size") or 0)
        if lot <= 0:
            return False
        # margin rail
        margin = MARGIN_FRAC * ltp * lot
        used = sum(MARGIN_FRAC * p["entry"] * p["lot_size"] for p in self._pos.values())
        if used + margin > MAX_MARGIN_UTIL * self._capital:
            self._log({"event": "entry_rejected", "fut_sym": fut_sym, "tag": tag,
                       "reason": f"margin {used + margin:,.0f} > {MAX_MARGIN_UTIL:.0%} of capital"})
            return False
        px = ltp + SLIPPAGE_PTS if direction == "LONG" else ltp - SLIPPAGE_PTS
        self._pos[fut_sym] = {
            "underlying": meta.get("underlying", fut_sym), "direction": direction,
            "entry": px, "lot_size": lot, "tag": tag, "armed": False,
            "entry_ts": datetime.now(timezone.utc).isoformat(),
        }
        self._log({"event": "ENTRY", "fut_sym": fut_sym, "dir": direction, "tag": tag,
                   "px": round(px, 2), "lot_size": lot, "margin": round(margin)})
        self._save_state()
        return True

    # ── exits ────────────────────────────────────────────────────────────────
    def _manage_exits(self, fut_sym: str, ltp: float, now) -> None:
        p = self._pos.get(fut_sym)
        if not p:
            return
        sign = 1.0 if p["direction"] == "LONG" else -1.0
        move = sign * (ltp - p["entry"]) / p["entry"]
        if move >= RATCHET_ARM_PCT and not p["armed"]:
            p["armed"] = True
            self._log({"event": "RATCHET_ARMED", "fut_sym": fut_sym, "move_pct": round(move * 100, 3)})
        reason = None
        if (now.hour, now.minute) >= EOD_FLAT_IST:
            reason = "EOD_FLAT"
        elif move <= -STOP_PCT:
            reason = "STOP"
        elif p["armed"] and move <= 0.0:
            reason = "BREAKEVEN_RATCHET"
        else:
            # adaptive exit: fresh opposite flow-turn against the position
            try:
                from platform_core.data.live.crossasset_shadow import LAST_TURN
                ev = LAST_TURN.get(p["underlying"])
                if (ev and time.time() - ev.get("epoch", 0) < 180
                        and ev.get("to") in ("LONG", "SHORT") and ev["to"] != p["direction"]):
                    reason = "FLOW_TURN_AGAINST"
            except Exception:
                pass
        if reason:
            self._exit(fut_sym, ltp, reason)

    def _exit(self, fut_sym: str, ltp: float, reason: str) -> None:
        p = self._pos.pop(fut_sym, None)
        if not p:
            return
        sign = 1.0 if p["direction"] == "LONG" else -1.0
        px = ltp - SLIPPAGE_PTS if p["direction"] == "LONG" else ltp + SLIPPAGE_PTS
        if reason == "BREAKEVEN_RATCHET":
            px = p["entry"]                          # floor at entry (ratchet semantics)
        gross = sign * (px - p["entry"]) * p["lot_size"]
        cost = _costs(p["entry"], px, p["lot_size"])
        net = gross - cost
        self._day_pnl += net
        import time as _t
        self._last_exit_epoch[fut_sym] = _t.time()
        self._log({"event": "EXIT", "fut_sym": fut_sym, "dir": p["direction"], "tag": p["tag"],
                   "entry": round(p["entry"], 2), "exit": round(px, 2), "reason": reason,
                   "gross": round(gross), "cost": round(cost), "net": round(net),
                   "day_pnl": round(self._day_pnl)})
        if self._day_pnl <= -DAILY_LOSS_HALT_FRAC * self._capital and not self._halted:
            self._halted = True
            self._log({"event": "SLEEVE_HALT", "day_pnl": round(self._day_pnl)})
        self._save_state()

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _fut_for(self, underlying: str) -> str | None:
        u = str(underlying).upper().replace("50", "")
        for fs, meta in self._contracts.items():
            if str(meta.get("underlying", "")).upper().replace("50", "") == u:
                return fs
        return None

    def _roll_day(self) -> None:
        d = datetime.now(_IST).date().isoformat()
        if d != self._day:
            self._day, self._day_pnl, self._halted = d, 0.0, False
            self._turn_count.clear()
            self._sig_count.clear()

    def _log(self, rec: dict) -> None:
        try:
            rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(json.dumps({
                "day": self._day, "day_pnl": self._day_pnl, "halted": self._halted,
                "positions": self._pos, "turn_count": self._turn_count}))
        except Exception:
            pass

    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                s = json.loads(self._state_path.read_text())
                if s.get("day") == self._day:       # same-day crash recovery only
                    self._day_pnl = float(s.get("day_pnl") or 0.0)
                    self._halted = bool(s.get("halted"))
                    self._pos = dict(s.get("positions") or {})
                    self._turn_count = dict(s.get("turn_count") or {})
        except Exception:
            pass
