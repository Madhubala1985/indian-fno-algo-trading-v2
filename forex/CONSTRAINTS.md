# Gold Programme — Binding Constraints
Written 2026-09-02, before any order has been placed. These exist because the Indian
programme lost Rs 31,948 over three months with no constraint that could say "this isn't working".

## A. Instrument — XAUUSDm ONLY
Everything else is excluded **by measurement, not preference**. Round-trip spread as a share of
the average move:

| | 5m | 15m | 60m |
|---|---|---|---|
| **XAUUSD** | **21%** | **12%** | **5.8%** |
| BTCUSD | 44% | 25% | 12% |
| XAGUSD | 81% | 46% | 23% |
| EURUSD | **116%** | 67% | 33% |

FX majors are arithmetically untradeable intraday: the spread exceeds the whole 5-minute move.
No instrument may be added without passing this table first.

## B. Time — 13:00-15:00 UTC, Mon-Fri
~2 hours of 24. Gold's US-macro window: lowest friction (13%), highest volatility.
Adjacent hours were **measured negative** (11-12 UTC: -2,537; 12-13: -1,565). Do not widen.

> TZ NOTE (verified 2026-09-02): the MT5 **Python API returns TRUE UTC epochs**; the terminal only
> *displays* server time (UTC+2). An early `-2h` correction mislabelled this window as 11-13 UTC.
> Bars analysed were always the correct ones — only the label was wrong. Never apply an offset
> to `copy_rates_*` output; measure it with `forex/tz_check` if ever in doubt.

## C. Signal
Momentum, 2h lookback, 4h hold, direction = sign of the 2h move. Acts only on the top 30% of
|2h moves|, threshold from a **trailing 60-day window** — never the full sample. Mean-reversion
in this window was strongly negative and is excluded.

## D. THE NO-TRADE CONSTRAINT
**Zero orders until Section E passes.** `gold_shadow.py` contains no order-sending code at all —
this is enforced by absence, not by a flag. The in-sample result was **89% driven by 2026 alone**
(2024 was negative), so the hypothesis is currently *unproven*, not *proven-and-waiting*.

## E. Graduation gate — ALL must hold on FORWARD data before one real order
1. >= 60 forward observations (~12 weeks)
2. Net positive after spread
3. Positive **ex-best-day**
4. Positive in **both halves** of the forward period
5. Positive in **>= 2 of 3** forward months
6. **Median trade > 0** — not carried by the tail

Gates 4-6 exist because gates 2-3 alone passed a signal that later proved to be a bull-regime
artifact. Year-by-year stability was the test that caught it.

## F. If it graduates — risk limits
- **One position at a time.** Never overlapping.
- **0.01 lot fixed** for the first 30 live trades. No sizing discretion.
  (0.01 lot = 1 oz = $1 per $1 gold move; margin is only $2.16 at 1:2000)
- **Stop: $30 adverse** (= 1% of equity). Mean 4h move is ~$9, so this is ~3 sigma.
- **Daily loss limit $60 (2%)** -> flat for the day.
- **Cumulative: equity < $2,700 (-10%) -> full stop**, no discretion, re-evaluate from scratch.
- Size may not increase until 30 trades are complete AND net positive.

**Leverage is 1:2000 here — margin will never stop a bad decision. Only this list will.**

## G. Kill condition
If forward net is negative after 60 observations, the hypothesis is **dead**. It gets reported
as dead and not retried with tweaked parameters. The 4.2-year backtest dataset is mined out
(1,000+ implicit comparisons) and can no longer referee anything.

---

## H. Capital & compounding (added 2026-09-03, after the operator asked to scale)

The compounding law is **one function**, `sizer.size()`, fed by **one measurement**,
`edge_ledger.measure()`. Nothing else may set a lot size.

```
lot = quarter_Kelly(measured edge) x equity / (stop_distance x 100)
```

Three properties, all deliberate:

1. **It compounds with equity.** Risk is a fraction, so size grows on wins and shrinks on
   losses with nobody deciding anything.
2. **It compounds with EVIDENCE.** Kelly is fed the **Wilson 95% lower bound** of the win
   rate, not the observed one. A 67% win rate over 3 trades has a lower bound of 21% and
   sizes to nothing; the same rate over 100 trades sizes properly. This is the only honest
   way to scale into an edge that is still being measured.
3. **It returns 0.0.** A 50-52% win rate at 1:1 has no expectancy after the $0.39 toll and
   gets zero lots. Refusing is the machinery working.

Loss brakes are now **fractions, not dollars** — a compounding account whose brakes are fixed
in dollars gets looser every time it wins. Daily limit 2% of current equity; hard halt at
**10% below the equity high-water mark** (persisted in `hwm.json`), not 10% below $3,000.

### H.1 MINIMUM VIABLE CAPITAL — the broker minimum is the binding constraint

0.01 lot is the smallest tradeable size and it risks **$1 per $1 of stop distance**. So the
minimum position has a fixed dollar risk, and the account must be large enough to absorb it:

| stop | min equity @1% risk | @1.5% | @2% |
|---|---|---|---|
| $10 | $1,000 | $667 | $500 |
| $20 | $2,000 | $1,333 | $1,000 |
| $30 | $3,000 | $2,000 | $1,500 |
| **$50** (current disaster stop) | **$5,000** | $3,333 | **$2,500** |

**A $1,000 account cannot trade this system.** At the $50 disaster stop the minimum lot is
5% of equity — 2.5x the ceiling — and `sizer.size()` returns 0.0 lots with that reason.
It is not a judgement call; it is arithmetic, and it is enforced in code.

### H.2 Why small accounts cannot compound *smoothly*

Size moves in 0.01 steps, so the granularity of the risk ladder depends entirely on equity
(shown at a $20 stop):

| equity | 0.01 | 0.02 | 0.03 | 0.05 |
|---|---|---|---|---|
| $1,000 | 2.0% | 4.0% | 6.0% | 10.0% |
| $3,000 | 0.7% | 1.3% | 2.0% | 3.3% |
| $10,000 | 0.2% | 0.4% | 0.6% | 1.0% |

Below ~$3,000 the smallest available size increase is a **doubling of risk**. Compounding
needs a size ladder with rungs closer together than the thing being compounded. That is a
property of capital, not of skill, and no cleverness substitutes for it.

### H.3 Order of operations for scaling — evidence first, capital second

1. Forward test to **n>=30** at fixed 0.01 lot. Size may not move before this.
2. `edge_ledger.py` reports `may_size_up: true` — i.e. n>=30 AND net>0 AND conservative
   Kelly>0. This is the ONLY authorisation to increase size.
3. Full graduation (all six Section E gates, n>=60) before **real** money.
4. Only then does more capital help. Adding capital before step 3 buys a bigger loss.
