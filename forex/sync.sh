#!/usr/bin/env bash
# Keep the repo and the Windows deployment directory identical.
#
# WHY THIS EXISTS: on the Indian programme the VM became a second author and push_to_vm.sh
# silently reverted VM-written code (see memory project_vm_rsync_clobber). The same thing
# started here — manager.py, discretionary.py, pyramid.py and protect_now.py were written on
# the Windows side on 2026-09-02 and existed nowhere in git. Run this after editing either side.
#
#   ./sync.sh pull     Windows -> repo   (capture work done on the trading host)
#   ./sync.sh push     repo -> Windows   (deploy)
#   ./sync.sh diff     show what differs, change nothing
set -euo pipefail

WIN="/mnt/c/Users/madhu/gold_shadow"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# repo name : windows name   (they differ for the two originals; keep new files identical)
MAP=(
  "gold_shadow.py:shadow.py"
  "gold_trader.py:trader.py"
  "sizer.py:sizer.py"
  "edge_ledger.py:edge_ledger.py"
  "test_sizer.py:test_sizer.py"
  "manager.py:manager.py"
  "discretionary.py:discretionary.py"
  "pyramid.py:pyramid.py"
  "protect_now.py:protect_now.py"
  "pos.py:pos.py"
  "preflight.py:preflight.py"
  "tape.py:tape.py"
  "mkt.py:mkt.py"
  "mkt2.py:mkt2.py"
  "hist.py:hist.py"
  "tz.py:tz.py"
  "algocheck.py:algocheck.py"
)

cmd="${1:-diff}"
for pair in "${MAP[@]}"; do
  r="$REPO/${pair%%:*}"; w="$WIN/${pair##*:}"
  case "$cmd" in
    pull) [ -f "$w" ] && cp "$w" "$r" && echo "pull  ${pair##*:} -> ${pair%%:*}" ;;
    push) [ -f "$r" ] && cp "$r" "$w" && echo "push  ${pair%%:*} -> ${pair##*:}" ;;
    diff)
      if [ ! -f "$w" ]; then echo "MISSING ON WINDOWS: ${pair##*:}"
      elif [ ! -f "$r" ]; then echo "MISSING IN REPO:    ${pair%%:*}"
      elif ! cmp -s "$r" "$w"; then echo "DIFFERS: ${pair%%:*}  (repo $(stat -c%y "$r" | cut -d. -f1) | win $(stat -c%y "$w" | cut -d. -f1))"
      fi ;;
    *) echo "usage: sync.sh [pull|push|diff]"; exit 1 ;;
  esac
done
[ "$cmd" = diff ] && echo "(no output above = repo and Windows are identical)"
exit 0
