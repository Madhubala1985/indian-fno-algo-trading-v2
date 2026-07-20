"""
study_or_width_pnl — does opening-range WIDTH predict the book losing money?

Asked after night-07-20 H_A found wide-OR days become INSIDE (chop) days more
often (narrow 0/29, mid 1/29, wide 5/30, base 7%). That is a CLASSIFICATION
result. Wiring a day-type FILTER needs the MONEY result, which this measures.

VERDICT: DO NOT WIRE. OR width does not predict book P&L.

  Spearman(OR width, trade pnl) = +0.019   (n=66 trades, 25 symbol-days)

  tercile  trades      total     mean  win%   ex-best-day
  narrow       22    -19,947     -907   23%       -19,947
  mid          23     35,755    1,555   61%        -8,347
  wide         21     -1,423      -68   33%       -16,405

Three reasons the filter fails:

1. WIDE IS NOT THE BAD BUCKET. Wide is the LEAST negative of the three
   (-1,423). Narrow is the worst (-19,947). Blocking wide recovers +1,423;
   blocking narrow would recover +19,947. The hypothesis is backwards in P&L
   terms -- though with rho=+0.019 the ordering is almost certainly noise.

2. THE TERCILES ARE NOT DISTINGUISHABLE. Cuts land at narrow<0.307% and
   wide>0.326% -- 0.019 percentage points apart. This sample has almost no
   spread in OR width, so the "terciles" are splitting noise.

3. EVERY TERCILE IS NEGATIVE EX-BEST-DAY. Book total +14,385 but ex-06-29
   -29,718. OR width is not the discriminator; the book is simply negative
   outside one day. A day-type filter cannot fix that.

METHODOLOGICAL NOTE (kept deliberately): the pre-registered decision rule was
"wide is net-negative AND stays negative ex-best-day". Wide PASSES that rule --
and the rule is WRONG, because it has no COMPARATIVE clause. A bucket being
negative means nothing when every bucket is negative. Applying the rule blindly
would have wired a filter that blocks the least-bad third of days. Any future
tercile study must require the target bucket to be materially WORSE than the
others, not merely negative.

Separately: the inside-day outcome H_A identifies is ALREADY handled by the
existing OR chop guard -- 2026-07-20 was an inside day, the guard fired 221
times, the book traded 0 and lost 0, and the night agent graded that A. A
wide-OR pre-filter would be redundant with a mechanism that already works.

Run: PYTHONPATH=. python3 scripts/study_or_width_pnl.py
Logs-only. Reads spot CSVs + trades.db; changes nothing.
"""
import csv, glob, sqlite3, collections, statistics as st

FILES = {"NIFTY": "nifty", "BANKNIFTY": "banknifty", "SENSEX": "sensex"}

def or_widths():
    out = {}
    for sym, stub in FILES.items():
        best = sorted(glob.glob(f"data/{stub}_1m_*.csv"))[-1]
        days = collections.defaultdict(list)
        for r in csv.reader(open(best)):
            if not r or r[0] == "timestamp":
                continue
            days[r[0][:10]].append((r[0][11:16], float(r[2]), float(r[3]), float(r[1])))
        for d, bars in days.items():
            orb = [b for b in bars if "09:15" <= b[0] <= "09:29"]
            if len(orb) < 10:
                continue
            hi = max(b[1] for b in orb); lo = min(b[2] for b in orb)
            op = orb[0][3]
            if op > 0:
                out[(sym, d)] = (hi - lo) / op
    return out

W = or_widths()
c = sqlite3.connect("logs/trades.db")
rows = []
for und, day, pnl in c.execute(
        "select underlying, substr(entry_ts,1,10), pnl from pt_trades"):
    w = W.get((und, day))
    if w is not None:
        rows.append((und, day, w, pnl or 0.0))

print(f"joined trades: {len(rows)}  "
      f"(symbol,day) pairs: {len(set((r[0], r[1]) for r in rows))}")
if not rows:
    raise SystemExit("no overlap between trade days and spot OR data")

ws = sorted(r[2] for r in rows)
t1, t2 = ws[len(ws)//3], ws[2*len(ws)//3]
print(f"tercile cuts (within joined sample): narrow <{t1:.3%}  mid  wide >{t2:.3%}\n")

def bucket(w):
    return "narrow" if w < t1 else ("wide" if w > t2 else "mid")

by = collections.defaultdict(list)
for und, day, w, pnl in rows:
    by[bucket(w)].append((day, pnl))

print(f"{'tercile':8} {'trades':>7} {'total':>11} {'mean':>9} {'win%':>6} {'ex-best-day':>13}")
verdict = {}
for b in ("narrow", "mid", "wide"):
    v = by[b]
    if not v:
        print(f"{b:8} {'0':>7}"); continue
    tot = sum(p for _, p in v)
    wins = sum(1 for _, p in v if p > 0)
    perday = collections.defaultdict(float)
    for d, p in v:
        perday[d] += p
    best = max(perday.values()) if perday else 0.0
    exbest = tot - best
    verdict[b] = (tot, exbest)
    print(f"{b:8} {len(v):>7} {tot:>11,.0f} {tot/len(v):>9,.0f} "
          f"{100*wins/len(v):>5.0f}% {exbest:>13,.0f}")

print("\n--- DECISION RULE ---")
if "wide" in verdict:
    tot, exbest = verdict["wide"]
    print(f"wide tercile: total {tot:,.0f}   ex-best-day {exbest:,.0f}")
    if tot < 0 and exbest < 0:
        print("=> PASS: wide-OR days are net-negative and stay negative ex-best-day.")
        print("   A wide-OR block is justified. Wire behind a default-OFF knob.")
    else:
        print("=> FAIL: wide-OR days are NOT reliably loss-making.")
        print("   Blocking them would forgo profitable days. DO NOT WIRE.")

# per-day view so one day cannot hide inside a tercile
print("\nper (symbol,day), sorted by OR width:")
agg = collections.defaultdict(float)
cnt = collections.Counter()
for und, day, w, pnl in rows:
    agg[(und, day, w)] += pnl
    cnt[(und, day, w)] += 1
for (und, day, w), p in sorted(agg.items(), key=lambda kv: kv[0][2]):
    print(f"  {bucket(w):6} {w:6.3%}  {und:10} {day}  n={cnt[(und,day,w)]:2}  pnl={p:>10,.0f}")

print("\n" + "="*64)
print("SANITY CHECKS THE DECISION RULE DID NOT MAKE")
print("="*64)
tot_all = sum(p for _, _, _, p in rows)
perday_all = collections.defaultdict(float)
for und, day, w, p in rows:
    perday_all[day] += p
best_day = max(perday_all, key=perday_all.get)
print(f"book total {tot_all:,.0f}   ex-best-day({best_day}) "
      f"{tot_all - perday_all[best_day]:,.0f}")

print("\nex-best-day BY TERCILE (the comparative the rule omitted):")
for b in ("narrow", "mid", "wide"):
    if b in verdict:
        print(f"  {b:8} total {verdict[b][0]:>10,.0f}   ex-best {verdict[b][1]:>10,.0f}")
print("  -> if ALL are negative ex-best, OR width is not the discriminator;")
print("     the book is simply negative outside one day.")

print("\nwhat BLOCKING each tercile would have done to the book:")
for b in ("narrow", "mid", "wide"):
    if b in verdict:
        print(f"  block {b:8}: {tot_all:,.0f} -> {tot_all - verdict[b][0]:,.0f}"
              f"   ({-verdict[b][0]:+,.0f})")

print(f"\ntercile cut spread: narrow<{t1:.3%}  wide>{t2:.3%}"
      f"   -> only {100*(t2-t1):.3f}pp separates them")

# rank correlation OR width vs pnl
pairs = [(w, p) for _, _, w, p in rows]
n = len(pairs)
rw = {v: i for i, v in enumerate(sorted(set(w for w, _ in pairs)))}
rp = {v: i for i, v in enumerate(sorted(set(p for _, p in pairs)))}
xs = [rw[w] for w, _ in pairs]; ys = [rp[p] for _, p in pairs]
mx, my = st.mean(xs), st.mean(ys)
num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
den = (sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys)) ** 0.5
print(f"Spearman(OR width, trade pnl) = {num/den if den else float('nan'):+.3f}  (n={n})")
