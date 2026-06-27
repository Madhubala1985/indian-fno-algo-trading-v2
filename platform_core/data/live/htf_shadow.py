"""
htf_shadow.py — Phase-1 SHADOW wiring of HTF-bias alignment (no trade effect).

Per docs/intelligence_wiring_plan.md. The higher-timeframe biases (daily / h4 /
h1 / m15) are already computed every bar inside the MarketContext but are NOT
used to gate option execution (logged only as ``[DIAG HTF — not used …]``). This
module records, per bar, how the execution bias aligns with those HTF biases and
appends one JSONL record to ``logs/htf_shadow.jsonl``. The harness joins these to
executed-trade outcomes to test whether HTF-aligned trades win more — BEFORE HTF
ever gates or sizes a real trade.

Observational only. Every call is wrapped so a shadow failure can never disturb
the trade path.
"""
from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

# Execution bias -> the HTF directional label that AGREES with it.
_DIR_OF_BIAS = {"LONG": "BULLISH", "SHORT": "BEARISH"}


class HTFShadow:
    """Records HTF-bias alignment per bar — logging only, never affects trades."""

    def __init__(self, *, out_path: str = "logs/htf_shadow.jsonl") -> None:
        self._out = Path(out_path)
        self.records = 0

    def observe(
        self,
        *,
        underlying: str,
        timestamp,
        exec_bias=None,
        exec_conf=None,
        daily_bias=None,
        h4_bias=None,
        h1_bias=None,
        m15_bias=None,
        is_aligned=None,
        htf_confidence=None,
    ) -> None:
        """Record HTF alignment for one completed bar. NEVER raises."""
        try:
            want = _DIR_OF_BIAS.get(str(exec_bias).upper())
            agree = against = 0
            if want is not None:
                opp = "BEARISH" if want == "BULLISH" else "BULLISH"
                for v in (daily_bias, h4_bias, h1_bias, m15_bias):
                    if v == want:
                        agree += 1
                    elif v == opp:
                        against += 1
            try:
                ts = timestamp.astimezone(timezone.utc).isoformat()
            except Exception:
                ts = str(timestamp)
            rec = {
                "ts": ts,
                "underlying": underlying,
                "exec_bias": str(exec_bias),
                "exec_conf": exec_conf,
                "daily": daily_bias,
                "h4": h4_bias,
                "h1": h1_bias,
                "m15": m15_bias,
                "is_aligned": is_aligned,
                "htf_conf": htf_confidence,
                "htf_agree": agree,      # # of HTF timeframes agreeing with exec bias (0-4)
                "htf_against": against,  # # opposing (0-4)
            }
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
        except Exception:
            # Shadow path must NEVER affect live trading.
            pass
