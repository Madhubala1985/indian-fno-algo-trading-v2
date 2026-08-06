"""Causal replay harness — feeds Claude the market state UP TO a checkpoint only (no lookahead),
so trading these historical days is a genuine test of adaptive skill, not hindsight.

Usage:
  frame  <INDEX> <DATE> <HHMM_IST>   -> print causal state at that time + mark my open positions
  settle <INDEX> <DATE>              -> price my open positions at the day's LAST snapshot (EOD)

State from the real recorded chains (data/option_history/). Spot via put-call parity. My trades
live in data/claude_trades/replay_<INDEX>_<DATE>.jsonl (append entries/exits externally).
"""
import sys, json, glob, os

def load(index, date):
    files = sorted(f for f in glob.glob(f"data/option_history/{index}_{date}_*.jsonl")
                   if not os.path.basename(f).startswith("_"))
    if not files:
        return None, []
    path = files[0]  # nearest expiry (name sorts ascending by expiry date)
    per = {}
    for line in open(path):
        try: d = json.loads(line)
        except Exception: continue
        if not d.get("ltp") or d["ltp"] <= 0: continue
        per.setdefault(d["ts"], {})[(round(float(d["strike"])), d["type"])] = float(d["ltp"])
    rows = []
    for ts in sorted(per):
        legs = per[ts]; strikes = sorted({k[0] for k in legs})
        atm = None
        for s in strikes:
            ce, pe = legs.get((s, "CE")), legs.get((s, "PE"))
            if ce and pe and (atm is None or abs(ce - pe) < atm[0]): atm = (abs(ce - pe), s)
        if atm:
            spot = atm[1] + (legs[(atm[1], "CE")] - legs[(atm[1], "PE")])
            rows.append((ts, atm[1], spot, legs, strikes))
    return path, rows

def hhmm(ts):
    t = ts[11:16]; h = int(t[:2]) + 5; m = int(t[3:5]) + 30
    if m >= 60: m -= 60; h += 1
    return h * 100 + m

def blotter(index, date):
    return f"data/claude_trades/replay_{index}_{date}.jsonl"

def mark_open(index, date, legs):
    bl = blotter(index, date)
    if not os.path.exists(bl): return
    for line in open(bl):
        t = json.loads(line)
        if t.get("status") != "OPEN": continue
        if t.get("kind") == "IRON_FLY":
            k, w = t["strike"], t["wing"]
            body = [legs.get((k, "CE")), legs.get((k, "PE"))]
            wings = [legs.get((k + w, "CE")), legs.get((k - w, "PE"))]
            if all(x is not None for x in body + wings):
                now = (body[0] + body[1]) - (wings[0] + wings[1])
                pnl = (t["entry_px"] - now) * t["lot"]
                print(f"  [OPEN IRON-FLY {k}±{w} @{t['entry_px']}] now {now:.1f} -> P&L {pnl:+.0f}")
            else:
                print(f"  [OPEN IRON-FLY {k}±{w}] wing/body quote missing at this snap")
        elif t.get("kind") == "STRADDLE_SHORT":
            ce = legs.get((t["strike"], "CE")); pe = legs.get((t["strike"], "PE"))
            if ce and pe:
                pnl = (t["entry_px"] - (ce + pe)) * t["lot"]
                print(f"  [OPEN SHORT-STRADDLE {t['strike']} @{t['entry_px']}] now {ce+pe:.1f} -> P&L {pnl:+.0f}")
        else:
            px = legs.get((t["strike"], t["type"]))
            if px:
                sign = 1 if t["side"] == "LONG" else -1
                print(f"  [OPEN {t['side']} {t['strike']}{t['type']} @{t['entry_px']}] now {px} -> P&L {(px-t['entry_px'])*t['lot']*sign:+.0f}")

def frame(index, date, cut):
    path, rows = load(index, date)
    if not rows: print("NO DATA", index, date); return
    up = [r for r in rows if hhmm(r[0]) <= cut]
    if not up: print(f"no data yet at {cut}"); return
    spots = [r[2] for r in up]
    o, hi, lo, last = spots[0], max(spots), min(spots), spots[-1]
    cur = up[-1]; atm = cur[1]; legs = cur[3]; strikes = cur[4]
    strad = (legs.get((atm, "CE")) or 0) + (legs.get((atm, "PE")) or 0)
    print(f"=== {index} {date}  {cut//100:02d}:{cut%100:02d} IST  [causal — no future] ===")
    print(f"open {o:.0f}  high {hi:.0f}  low {lo:.0f}  last {last:.0f}  range {100*(hi-lo)/o:.2f}%  net {100*(last-o)/o:+.2f}%")
    step = max(1, len(up)//12)
    print("path: " + " ".join(f"{r[2]:.0f}" for r in up[::step]) + f" (now {last:.0f})")
    def mom(n):
        n = min(n, len(spots)-1); return spots[-1]-spots[-1-n] if n>0 else 0
    import datetime as _dt
    exp = os.path.basename(path).rsplit("_", 1)[1][:10]
    dte = (_dt.date.fromisoformat(exp) - _dt.date.fromisoformat(date)).days
    pct = 100 * strad / last
    print(f"mom: last15 {mom(15):+.0f}  last30 {mom(30):+.0f}  |  ATM straddle {strad:.1f} = {pct:.2f}% of spot "
          f"| expiry {exp} DTE {dte} -> per-sqrt-day {pct/max(dte,1)**0.5:.2f}%")
    below = [s for s in strikes if s < atm][-4:]; above = [s for s in strikes if s > atm][:4]
    print("ladder (strike: CE / PE):")
    for s in below + [atm] + above:
        print(f"  {s:>8.0f}:  CE {legs.get((s,'CE'))}   PE {legs.get((s,'PE'))}")
    mark_open(index, date, legs)

def settle(index, date):
    path, rows = load(index, date)
    if not rows: print("NO DATA"); return
    legs = rows[-1][3]; print(f"=== {index} {date} EOD settle @ {hhmm(rows[-1][0])//100}:xx  spot {rows[-1][2]:.0f} ===")
    mark_open(index, date, legs)

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "frame": frame(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif cmd == "settle": settle(sys.argv[2], sys.argv[3])
