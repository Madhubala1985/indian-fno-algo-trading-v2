#!/usr/bin/env bash
# run_live_oi_diagnostics.sh
# ──────────────────────────────────────────────────────────────────────
# Run OI support diagnostics against live Kite option chain.
# Execute during market hours (09:30–15:20 IST) for meaningful data.
#
# Read-only. No orders placed. No live pipeline imported.
#
# Usage:
#   bash scripts/run_live_oi_diagnostics.sh
#   bash scripts/run_live_oi_diagnostics.sh NIFTY       # single symbol
#   bash scripts/run_live_oi_diagnostics.sh BANKNIFTY NIFTY
#
# Output is printed to terminal only — no logs written.
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail
cd "$(dirname "$0")/.."

SYMBOLS="${*:-BANKNIFTY NIFTY SENSEX}"

echo "========================================================================"
echo "  LIVE OI DIAGNOSTICS — $(date '+%Y-%m-%d %H:%M:%S IST')"
echo "  Read-only. No orders. No pipeline import."
echo "========================================================================"
echo ""

for SYM in $SYMBOLS; do
    echo "── $SYM ──────────────────────────────────────────────────────────────"
    PYTHONPATH=. python3 scripts/diagnose_oi_support.py --live --symbol "$SYM" || {
        echo "[ERROR] $SYM diagnostic failed (check access token / market hours)"
    }
    echo ""
done

echo "========================================================================"
echo "  Done. Interpret results:"
echo "  - oi_support < 0.10 for near-ATM: check if chain_max_OI is far-OTM"
echo "  - raw OI < 100,000: genuinely thin — gate is protecting you"
echo "  - raw OI > 500,000 but oi_support still low: normalization issue"
echo "========================================================================"
