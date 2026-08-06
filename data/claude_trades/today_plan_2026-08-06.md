# Claude live paper-trading plan — 2026-08-06

**Read at 13:10 IST:** PIN / low-vol day. NIFTY 24,626–24,648 flat, SENSEX flat, BANKNIFTY
+0.36% RS-leader but only *holding* highs (not trending). VIX 12.3. No directional setup.

## Open paper positions (mine)
- SENSEX iron butterfly: short straddle ATM 78700, ±2 wings (width 400), credit 186.45,
  1 lot, opened 09:32. Decaying on the pin. Exit only on a hard SENSEX break or by close.

## Standing decisions
- **Direction: STAND ASIDE** unless a clear intraday trend develops (range-break + persistent
  higher-highs / lower-lows). Do NOT scalp flat chop (whipsaw) or buy premium at VIX 12.
- **If a trend develops:** scalp the correct-side ATM index option (leader = BANKNIFTY),
  record entry/exit + P&L to claude_trades.jsonl. Exit on momentum break, not an EOD clock.

## THE KEY EVENT — CAS (15:15–15:35)
- Collector writes `data/cas_history/cas_2026-08-06.jsonl` (cron 15:08). At ~15:18 read it.
- Compute aggregate imbalance of the heavyweight constituents (tbq−tsq, weighted by turnover).
- **If a strong directional imbalance** (heavyweights net-buy or net-sell decisively): index
  options trade to 15:40, so paper-trade the index in that direction (ATM option) BEFORE 15:40.
- Confirm `key=CAS` actually populated with fresh 15:1x rows; if not, note the endpoint gap.

## Close-out
- Book/settle the SENSEX fly and any scalp at the close using live prices; grade the day in
  claude_trades.jsonl vs the mark made at 13:01.

## Credit discipline
- No background Claude loop. Mechanical collector does the CAS capture. I engage only at:
  (1) ~14:10 quick trend check, (2) ~15:10 pre-CAS, (3) ~15:18 CAS trade, (4) ~15:35 grade.
  Minimal tokens per check; stop after the close is graded.
