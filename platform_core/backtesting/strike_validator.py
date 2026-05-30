# FILE: platform_core/backtesting/strike_validator.py
"""
StrikeSelectionValidator — quantitative audit of strike and expiry selection.

For every completed trade, records the selected contract and all alternatives
available at entry (from options_ctx.strike_candidates), then re-prices each
alternative at exit using closed-form BSM to compute counterfactual P&L.

Answers:
  • Does StrikeSelectionEngine choose better contracts than ATM?
  • Does it choose better contracts than adjacent strikes?
  • Does it choose the right expiry (weekly vs biweekly)?
  • What % of the time is the selected strike the best available?
  • What is the average improvement vs the naïve ATM choice?
  • Where does selection most often fail?

Usage (BacktestRunner):
    validator = StrikeSelectionValidator(cfg.risk_free_rate, cfg.dividend_yield)
    # At entry:
    validator.record_entry(trade_id, bar_index, strike_rec, all_candidates,
                           entry_spot, entry_iv, lots, direction)
    # At exit:
    validator.record_exit(trade_id, exit_spot, exit_iv, exit_date,
                          closed.entry_price, closed.exit_price, closed.pnl,
                          exit_reason)
    # At end of run:
    validator.print_report()
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import mean, stdev
from typing import Any


# ── BSM pricer (standalone, no external dependency) ───────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bsm_price(S: float, K: float, T: float, r: float, q: float,
               sigma: float, is_call: bool) -> float:
    """Black-Scholes-Merton option price."""
    if T <= 0.0:
        return max(0.0, (S - K) if is_call else (K - S))
    if sigma <= 0.0 or not math.isfinite(sigma):
        return max(0.0, (S - K) if is_call else (K - S))
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    if is_call:
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class AltSnapshot:
    """One alternative contract captured at entry."""
    option_symbol: str
    strike_price: float
    option_type: str       # "CALL" or "PUT"
    expiry_date: date
    tte_years: float       # at entry
    entry_premium: float
    implied_volatility: float
    delta: float
    lot_size: int
    is_selected: bool
    is_atm: bool           # |delta| closest to 0.50 for same type+expiry
    same_expiry_as_selected: bool
    composite_score: float  # 0.0 for non-selected alternatives


@dataclass(slots=True)
class TradeRecord:
    """Full entry context for one trade, with counterfactuals filled at exit."""
    trade_id: int
    entry_bar: int
    direction: str          # "LONG" or "SHORT"
    lots: int
    entry_spot: float
    entry_iv: float         # ATM IV at entry
    risk_free_rate: float
    dividend_yield: float
    selected: AltSnapshot
    alternatives: list[AltSnapshot]

    # Filled at exit
    exit_spot: float = 0.0
    exit_iv: float = 0.0
    exit_date: date | None = None
    elapsed_days: float = 0.0
    actual_entry_price: float = 0.0
    actual_exit_price: float = 0.0
    actual_pnl: float = 0.0
    exit_reason: str = ""
    is_closed: bool = False

    # Computed at exit — parallel to (selected + alternatives)
    all_return_pcts: list[float] = field(default_factory=list)  # index 0 = selected
    selected_rank: int = 0          # 1 = best, len = worst
    selected_is_best: bool = False
    selected_in_top3: bool = False


# ── Validator ─────────────────────────────────────────────────────────────────

class StrikeSelectionValidator:

    def __init__(self, risk_free_rate: float, dividend_yield: float) -> None:
        self._rfr = risk_free_rate
        self._div = dividend_yield
        self._records: list[TradeRecord] = []
        self._pending: TradeRecord | None = None

    # ── Entry recording ───────────────────────────────────────────────────────

    def record_entry(
        self,
        trade_id: int,
        bar_index: int,
        strike_rec: Any,        # StrikeRecommendation
        all_candidates: list,   # list[StrikeCandidate] from options_ctx
        entry_spot: float,
        entry_iv: float,
        lots: int,
        direction: str,
    ) -> None:
        # Find ATM per (option_type, expiry_date) group
        atm_keys: dict[tuple, str] = {}  # (type, expiry) → option_symbol with |delta| nearest 0.50
        for c in all_candidates:
            key = (c.option_type, c.expiry_date)
            if key not in atm_keys or abs(abs(c.delta) - 0.50) < abs(abs(_delta_of(all_candidates, atm_keys[key])) - 0.50):
                atm_keys[key] = c.option_symbol

        selected_alt = AltSnapshot(
            option_symbol=strike_rec.option_symbol,
            strike_price=strike_rec.strike_price,
            option_type=strike_rec.option_type,
            expiry_date=strike_rec.expiry_date,
            tte_years=strike_rec.days_to_expiry / 365.0,
            entry_premium=strike_rec.last_traded_price,
            implied_volatility=strike_rec.implied_volatility,
            delta=strike_rec.delta,
            lot_size=strike_rec.lot_size,
            is_selected=True,
            is_atm=(atm_keys.get((strike_rec.option_type, strike_rec.expiry_date)) == strike_rec.option_symbol),
            same_expiry_as_selected=True,
            composite_score=strike_rec.composite_score,
        )

        alts: list[AltSnapshot] = []
        for c in all_candidates:
            if c.option_symbol == strike_rec.option_symbol:
                continue
            if c.last_traded_price <= 0.0 or c.implied_volatility <= 0.0:
                continue
            key = (c.option_type, c.expiry_date)
            alts.append(AltSnapshot(
                option_symbol=c.option_symbol,
                strike_price=c.strike_price,
                option_type=c.option_type,
                expiry_date=c.expiry_date,
                tte_years=c.time_to_expiry_years,
                entry_premium=c.last_traded_price,
                implied_volatility=c.implied_volatility,
                delta=c.delta,
                lot_size=c.lot_size,
                is_selected=False,
                is_atm=(atm_keys.get(key) == c.option_symbol),
                same_expiry_as_selected=(c.expiry_date == strike_rec.expiry_date),
                composite_score=0.0,
            ))

        self._pending = TradeRecord(
            trade_id=trade_id,
            entry_bar=bar_index,
            direction=direction,
            lots=lots,
            entry_spot=entry_spot,
            entry_iv=entry_iv,
            risk_free_rate=self._rfr,
            dividend_yield=self._div,
            selected=selected_alt,
            alternatives=alts,
        )

    # ── Exit recording & counterfactual computation ───────────────────────────

    def record_exit(
        self,
        exit_spot: float,
        exit_iv: float,
        exit_date: date,
        entry_price: float,
        exit_price: float,
        pnl: float,
        exit_reason: str,
        trade_id: int = -1,  # kept for API compat, not used for matching
    ) -> None:
        rec = self._pending
        if rec is None:
            return

        elapsed_years = max(
            (exit_date - rec.selected.expiry_date).days * -1 / 365.0
            + rec.selected.tte_years,
            0.0,
        )
        elapsed_days = (exit_date - _approx_entry_date(rec)).days

        rec.exit_spot = exit_spot
        rec.exit_iv = exit_iv if exit_iv and exit_iv > 0 else rec.entry_iv
        rec.exit_date = exit_date
        rec.elapsed_days = float(elapsed_days)
        rec.actual_entry_price = entry_price
        rec.actual_exit_price = exit_price
        rec.actual_pnl = pnl
        rec.exit_reason = exit_reason
        rec.is_closed = True

        # Actual return of selected
        actual_ret = _return_pct(entry_price, exit_price)

        # Counterfactual return for each alternative using BSM re-pricing
        all_snaps = [rec.selected] + rec.alternatives
        returns: list[float] = []
        iv_for_exit = rec.exit_iv

        for snap in all_snaps:
            tte_at_exit = max(snap.tte_years - elapsed_days / 365.0, 0.0)
            is_call = snap.option_type.upper() == "CALL"
            exit_bsm = _bsm_price(
                exit_spot, snap.strike_price, tte_at_exit,
                self._rfr, self._div, iv_for_exit, is_call,
            )
            # Apply direction: LONG = bought option (profit when price rises)
            # SHORT = bought put (profit when price falls)
            entry_p = snap.entry_premium
            ret = _return_pct(entry_p, exit_bsm)
            returns.append(ret)

        rec.all_return_pcts = returns

        # Rank selected (index 0) against same-option-type alternatives only.
        # Comparing a PUT against CALLs during a bearish move is not meaningful:
        # the algorithm would never select a CALL on SHORT bias.
        sel_type = rec.selected.option_type
        same_type_returns = [
            ret
            for snap, ret in zip([rec.selected] + rec.alternatives, returns)
            if snap.option_type == sel_type
        ]
        sorted_same = sorted(same_type_returns, reverse=True)
        selected_return = returns[0]
        rec.selected_rank = sorted_same.index(selected_return) + 1 if selected_return in sorted_same else len(sorted_same)
        rec.selected_is_best = rec.selected_rank == 1
        rec.selected_in_top3 = rec.selected_rank <= 3

        self._records.append(rec)
        self._pending = None

    # ── Report ────────────────────────────────────────────────────────────────

    def print_report(self) -> None:
        closed = [r for r in self._records if r.is_closed]
        n = len(closed)
        if n == 0:
            print("\n[STRIKE VALIDATOR]  No closed trades to analyse.")
            return

        W = 72
        print("\n" + "=" * W)
        print("  STRIKE & EXPIRY SELECTION AUDIT")
        print("=" * W)
        print(f"  Trades analysed:  {n}")

        # ── 1. Selection rank distribution ────────────────────────────────────
        best_count   = sum(1 for r in closed if r.selected_is_best)
        top3_count   = sum(1 for r in closed if r.selected_in_top3)
        ranks = [r.selected_rank for r in closed]
        print(f"\n  A. SELECTION RANK (lower = better)")
        print(f"     Selected was #1 (best):    {best_count:3d} / {n}  ({best_count/n:.1%})")
        print(f"     Selected in top-3:         {top3_count:3d} / {n}  ({top3_count/n:.1%})")
        print(f"     Median rank:               {_median(ranks):.1f}")
        print(f"     Mean rank:                 {mean(ranks):.1f}")

        # ── 2. vs ATM ─────────────────────────────────────────────────────────
        vs_atm = _vs_atm(closed)
        if vs_atm:
            beats_atm = sum(1 for d in vs_atm if d > 0)
            avg_imp = mean(vs_atm)
            print(f"\n  B. SELECTED vs ATM (same option type & expiry)")
            print(f"     Beats ATM:                 {beats_atm:3d} / {len(vs_atm)}  ({beats_atm/len(vs_atm):.1%})")
            print(f"     Avg improvement vs ATM:    {avg_imp:+.1f}%  (return %-point)")
            print(f"     Median improvement:        {_median(vs_atm):+.1f}%")

        # ── 3. vs adjacent strikes (±1 step same expiry) ──────────────────────
        adj_above, adj_below = _vs_adjacent(closed)
        if adj_above:
            beats_above = sum(1 for d in adj_above if d > 0)
            beats_below = sum(1 for d in adj_below if d > 0)
            print(f"\n  C. SELECTED vs ADJACENT STRIKES (same expiry)")
            print(f"     Beats strike +1 step above: {beats_above:3d} / {len(adj_above)}  ({beats_above/len(adj_above):.1%})")
            print(f"     Beats strike -1 step below: {beats_below:3d} / {len(adj_below)}  ({beats_below/len(adj_below):.1%})")
            print(f"     Avg vs +1:                  {mean(adj_above):+.1f}%")
            print(f"     Avg vs -1:                  {mean(adj_below):+.1f}%")

        # ── 4. Expiry selection (weekly vs alternative) ────────────────────────
        expiry_deltas, selected_labels, alt_labels = _vs_alt_expiry(closed)
        if expiry_deltas:
            beats_alt_expiry = sum(1 for d in expiry_deltas if d > 0)
            print(f"\n  D. EXPIRY SELECTION (selected expiry vs nearest alternative)")
            print(f"     Beats alt expiry:           {beats_alt_expiry:3d} / {len(expiry_deltas)}  "
                  f"({beats_alt_expiry/len(expiry_deltas):.1%})")
            print(f"     Avg improvement vs alt:     {mean(expiry_deltas):+.1f}%")

        # ── 5. Score vs realised rank correlation ─────────────────────────────
        print(f"\n  E. COMPOSITE SCORE vs REALISED RANK")
        corr = _score_rank_correlation(closed)
        print(f"     Score-rank correlation:     {corr:+.3f}  "
              f"(-1.0 = perfect, 0 = none, +1.0 = inverted)")
        _print_score_buckets(closed)

        # ── 6. Failure cases ──────────────────────────────────────────────────
        worst = sorted(closed, key=lambda r: r.selected_rank, reverse=True)[:5]
        print(f"\n  F. WORST SELECTION CASES")
        for r in worst:
            n_alts = len(r.alternatives)
            print(f"     Trade {r.trade_id:3d}  rank={r.selected_rank}/{n_alts+1:2d}  "
                  f"reason={r.exit_reason:15s}  "
                  f"selected_ret={r.all_return_pcts[0]:+5.1f}%  "
                  f"best_available={max(r.all_return_pcts):+5.1f}%")

        # ── 7. Verdict ────────────────────────────────────────────────────────
        print(f"\n  G. VERDICT")
        strike_adds_value  = bool(vs_atm)  and mean(vs_atm) > 0
        expiry_adds_value  = bool(expiry_deltas) and mean(expiry_deltas) > 0
        selection_reliable = top3_count / n >= 0.50
        print(f"     Strike selection adds value vs ATM:    {'YES' if strike_adds_value  else 'NO'}")
        print(f"     Expiry selection adds value:           {'YES' if expiry_adds_value  else 'NO'}")
        print(f"     Selected in top-3 majority of time:   {'YES' if selection_reliable else 'NO'}")

        # Largest weakness
        weakness = _identify_weakness(closed, best_count, n, vs_atm, expiry_deltas, corr)
        print(f"     Largest remaining weakness:            {weakness}")

        print("=" * W)


# ── Statistical helpers ───────────────────────────────────────────────────────

def _return_pct(entry: float, exit_: float) -> float:
    if entry <= 0:
        return 0.0
    return (exit_ - entry) / entry * 100.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _delta_of(candidates: list, symbol: str) -> float:
    for c in candidates:
        if c.option_symbol == symbol:
            return c.delta
    return 0.0


def _approx_entry_date(rec: TradeRecord) -> date:
    """Approximate entry date from TTE and expiry."""
    from datetime import timedelta
    tte_days = int(rec.selected.tte_years * 365)
    return rec.selected.expiry_date - timedelta(days=tte_days)


def _vs_atm(records: list[TradeRecord]) -> list[float]:
    deltas = []
    for r in records:
        selected_ret = r.all_return_pcts[0]
        # Find ATM in alternatives (same type, same expiry, is_atm=True)
        for i, snap in enumerate([r.selected] + r.alternatives):
            if snap.is_atm and snap.same_expiry_as_selected and not snap.is_selected:
                atm_ret = r.all_return_pcts[i]
                deltas.append(selected_ret - atm_ret)
                break
    return deltas


def _vs_adjacent(records: list[TradeRecord]) -> tuple[list[float], list[float]]:
    """Compare selected against ±1 strike step in same expiry."""
    above, below = [], []
    for r in records:
        sel_strike = r.selected.strike_price
        sel_expiry = r.selected.expiry_date
        sel_type   = r.selected.option_type
        sel_ret    = r.all_return_pcts[0]

        # Find nearest strikes above and below (same expiry, same type)
        candidates_same = [
            (snap, ret)
            for snap, ret in zip([r.selected] + r.alternatives, r.all_return_pcts)
            if snap.expiry_date == sel_expiry and snap.option_type == sel_type
            and not snap.is_selected and snap.entry_premium > 0
        ]
        above_cands = [(s, ret) for s, ret in candidates_same if s.strike_price > sel_strike]
        below_cands = [(s, ret) for s, ret in candidates_same if s.strike_price < sel_strike]

        if above_cands:
            nearest_above = min(above_cands, key=lambda x: x[0].strike_price - sel_strike)
            above.append(sel_ret - nearest_above[1])
        if below_cands:
            nearest_below = max(below_cands, key=lambda x: x[0].strike_price)
            below.append(sel_ret - nearest_below[1])

    return above, below


def _vs_alt_expiry(records: list[TradeRecord]) -> tuple[list[float], list[str], list[str]]:
    """Compare selected against same strike in the alternative expiry."""
    deltas, sel_labels, alt_labels = [], [], []
    for r in records:
        sel_strike = r.selected.strike_price
        sel_expiry = r.selected.expiry_date
        sel_type   = r.selected.option_type
        sel_ret    = r.all_return_pcts[0]

        # Find closest strike in a DIFFERENT expiry, same type
        alt_cands = [
            (snap, ret)
            for snap, ret in zip([r.selected] + r.alternatives, r.all_return_pcts)
            if snap.expiry_date != sel_expiry and snap.option_type == sel_type
            and abs(snap.strike_price - sel_strike) < 500  # within range
            and snap.entry_premium > 0
        ]
        if not alt_cands:
            continue
        nearest_alt = min(alt_cands, key=lambda x: abs(x[0].strike_price - sel_strike))
        deltas.append(sel_ret - nearest_alt[1])
        sel_labels.append(str(sel_expiry))
        alt_labels.append(str(nearest_alt[0].expiry_date))

    return deltas, sel_labels, alt_labels


def _score_rank_correlation(records: list[TradeRecord]) -> float:
    """Spearman-like: correlation between composite_score and realised rank."""
    scores = [r.selected.composite_score for r in records]
    ranks  = [float(r.selected_rank) for r in records]
    if len(scores) < 2:
        return 0.0
    try:
        n = len(scores)
        mean_s = mean(scores)
        mean_r = mean(ranks)
        num = sum((s - mean_s) * (rk - mean_r) for s, rk in zip(scores, ranks))
        den = math.sqrt(
            sum((s - mean_s) ** 2 for s in scores)
            * sum((rk - mean_r) ** 2 for rk in ranks)
        )
        return num / den if den > 0 else 0.0
    except Exception:
        return 0.0


def _print_score_buckets(records: list[TradeRecord]) -> None:
    """Show rank distribution across composite_score quartiles."""
    if not records:
        return
    scores = sorted(r.selected.composite_score for r in records)
    q1 = scores[len(scores) // 4]
    q3 = scores[3 * len(scores) // 4]
    buckets = {
        "low (Q1)":    [r for r in records if r.selected.composite_score <= q1],
        "mid":         [r for r in records if q1 < r.selected.composite_score <= q3],
        "high (Q3+)":  [r for r in records if r.selected.composite_score > q3],
    }
    for label, recs in buckets.items():
        if not recs:
            continue
        avg_rank = mean(r.selected_rank for r in recs)
        best_pct = sum(1 for r in recs if r.selected_is_best) / len(recs)
        print(f"     Score {label:10s} n={len(recs):3d}  avg_rank={avg_rank:.1f}  "
              f"best_rate={best_pct:.1%}")


def _identify_weakness(
    records: list[TradeRecord],
    best_count: int,
    n: int,
    vs_atm: list[float],
    expiry_deltas: list[float],
    corr: float,
) -> str:
    """Identify the largest single weakness."""
    if n == 0:
        return "insufficient data"
    issues = []
    if best_count / n < 0.30:
        issues.append(("selected rarely best (<30%)", 3))
    if vs_atm and mean(vs_atm) < -5:
        issues.append(("selected underperforms ATM significantly", 3))
    if expiry_deltas and mean(expiry_deltas) < -5:
        issues.append(("expiry selection destroys value", 3))
    if corr > 0.2:
        issues.append((f"composite score inversely predicts outcome (corr={corr:+.2f})", 2))
    if best_count / n < 0.45:
        issues.append((f"selected is #1 only {best_count/n:.0%} of the time", 2))
    if not issues:
        stop_exits = sum(1 for r in records if "STOP" in r.exit_reason.upper())
        if stop_exits / n > 0.5:
            issues.append((f"stop-loss exits dominate ({stop_exits/n:.0%}) — delta too low or strikes too OTM", 1))
        else:
            issues.append(("selection quality appears adequate on this sample", 0))
    issues.sort(key=lambda x: -x[1])
    return issues[0][0]
