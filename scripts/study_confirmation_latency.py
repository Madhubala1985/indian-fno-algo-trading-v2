"""
study_confirmation_latency — how late is exec_bias on intraday reversals?

Motivation: three independent sightings suggested directional confirmation
trails reversals (TURN detector lags pivots 0-25min and fired SHORT at the
2026-07-15 low; bias-invalidation confirmed BANKNIFTY LONG at 14:40 on 07-20,
28min after the 14:12 high; bias sat NEUTRAL through that whole V-recovery).
This measures it across every recorded reversal instead of anecdotes.

PRE-REGISTERED before computing:
  corpus  regime_shadow.jsonl (per-minute close) JOIN htf_shadow.jsonl
          (exec_bias/exec_conf) on (underlying, ts). 16 sessions x 3 underlyings.
  turn    W=30min, M=0.25%: price moved >=M into minute t over W AND >=M back
          out over W in the opposite direction, t is the extreme in [t-W,t+W].
          Strongest turn wins within +/-W. V-bottom => LONG, inverted-V => SHORT.
  confirm first minute c >= t where exec_bias == post-turn direction.
          latency = c - t. No confirmation by 15:15 IST = CENSORED: reported
          separately, never imputed, never dropped.
  headline metric = CAPTURE FRACTION, not minutes: how much of the post-turn
          move is still on the table at confirmation. Capture uses the post-turn
          extreme (perfect hindsight exit), so it is an UPPER BOUND on what was
          actually reachable.

KEY RESULT (2026-07-20, n=14 turns over 16 days):
  The headline "median latency 0 min" is an ARTEFACT and must not be quoted.
  Every 0 comes from a turn where exec_bias was ALREADY LONG before the turn --
  stickiness on a long-biased book, not detection. Stratified:
    A) bias already aligned (n=5, all LONG): latency 0 by construction.
    B) bias NOT aligned (n=9) -- the real test:
         confirmed 4, NEVER confirmed 5
         latency median 194 min (range 24-288)
         capture median 22.7% => ~77% of the move gone on arrival
    C) asymmetry: LONG turns 8/11 confirmed; SHORT turns 1/3, median 249 min.
       At 2 SHORT turns the bias was actively LONG -- both never confirmed.
  Read: this is not a reversal detector. It confirms directions it already
  held. Caveat n=14, LONG-skewed (11 vs 3) because the corpus period drifted up.

Run: PYTHONPATH=. python3 scripts/study_confirmation_latency.py
Logs-only. Reads nothing but shadow logs; changes nothing.
"""
import json, collections, datetime as dt, statistics as st

W = 30            # minutes, each leg
M = 0.0025        # 0.25% minimum leg
SESS_START, SESS_END = "09:20", "15:15"

def ist(ts):
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t + dt.timedelta(hours=5, minutes=30)

# ── load price + bias, keyed (underlying, day) -> {minute_index: value} ──────
px   = collections.defaultdict(dict)   # (sym,day) -> {hhmm: close}
bias = collections.defaultdict(dict)   # (sym,day) -> {hhmm: (bias, conf)}

for line in open("logs/regime_shadow.jsonl"):
    try: r = json.loads(line)
    except Exception: continue
    if not r.get("ts") or not r.get("underlying") or not r.get("close"): continue
    t = ist(r["ts"]); key = (r["underlying"], t.strftime("%Y-%m-%d"))
    px[key][t.strftime("%H:%M")] = float(r["close"])

for line in open("logs/htf_shadow.jsonl"):
    try: r = json.loads(line)
    except Exception: continue
    if not r.get("ts") or not r.get("underlying"): continue
    t = ist(r["ts"]); key = (r["underlying"], t.strftime("%Y-%m-%d"))
    b = str(r.get("exec_bias") or "").upper()
    try: c = float(r.get("exec_conf") or 0)
    except Exception: c = 0.0
    bias[key][t.strftime("%H:%M")] = (b, c)

def mins(hhmm):
    h, m = hhmm.split(":"); return int(h) * 60 + int(m)

turns = []
for key in sorted(px):
    sym, day = key
    series = {k: v for k, v in px[key].items() if SESS_START <= k <= SESS_END}
    if len(series) < 3 * W: continue
    ks = sorted(series)
    idx = {k: i for i, k in enumerate(ks)}
    cand = []
    for i, k in enumerate(ks):
        if i < W or i + W >= len(ks): continue
        c0, cm, cp = series[ks[i - W]], series[k], series[ks[i + W]]
        win = [series[x] for x in ks[i - W:i + W + 1]]
        down_in  = (c0 - cm) / c0 >= M          # fell into t
        up_out   = (cp - cm) / cm >= M          # rose out of t
        up_in    = (cm - c0) / c0 >= M
        down_out = (cm - cp) / cm >= M
        if down_in and up_out and cm == min(win):
            cand.append((k, "LONG", (c0 - cm) / c0 + (cp - cm) / cm))
        elif up_in and down_out and cm == max(win):
            cand.append((k, "SHORT", (cm - c0) / c0 + (cm - cp) / cm))
    # dedupe: keep strongest within +/- W
    cand.sort(key=lambda x: -x[2])
    kept = []
    for k, d, s in cand:
        if all(abs(mins(k) - mins(kk)) > W for kk, _, _ in kept):
            kept.append((k, d, s))
    for k, d, s in sorted(kept, key=lambda x: x[0]):
        turns.append((sym, day, k, d, s))

print(f"PRE-REGISTERED PARAMS: W={W}min  M={M:.2%}  window {SESS_START}-{SESS_END} IST")
print(f"corpus: {len(px)} (symbol,day) pairs, {len(set(d for _, d in px))} days")
print(f"\nTURNS DETECTED: {len(turns)}  "
      f"(LONG/V-bottom {sum(1 for t in turns if t[3]=='LONG')}, "
      f"SHORT/inv-V {sum(1 for t in turns if t[3]=='SHORT')})")

rows = []
for sym, day, k, d, s in turns:
    key = (sym, day)
    bser = bias.get(key, {})
    ks = sorted(x for x in px[key] if SESS_START <= x <= SESS_END)
    after = [x for x in ks if mins(x) >= mins(k)]
    at_turn = bser.get(k, ("<none>", 0.0))
    conf_min, lat = None, None
    for x in after:
        b, _ = bser.get(x, ("", 0))
        if b == d:
            conf_min, lat = x, mins(x) - mins(k); break
    # post-turn extreme (best the move ever offered) and capture fraction
    p_turn = px[key][k]
    ext = max((px[key][x] for x in after), default=p_turn) if d == "LONG" \
          else min((px[key][x] for x in after), default=p_turn)
    full = abs(ext - p_turn) / p_turn
    if conf_min is not None:
        p_conf = px[key][conf_min]
        rest = (ext - p_conf) / p_conf if d == "LONG" else (p_conf - ext) / p_conf
        capture = (rest / full) if full > 0 else None
    else:
        capture = None
    rows.append(dict(sym=sym, day=day, t=k, dir=d, at_turn=at_turn[0],
                     conf_at=conf_min, lat=lat, full=full, capture=capture))

conf_rows = [r for r in rows if r["lat"] is not None]
cens = [r for r in rows if r["lat"] is None]

def pct(v): return f"{100*v:.1f}%"
def q(xs, p):
    xs = sorted(xs); return xs[min(int(len(xs)*p), len(xs)-1)] if xs else float("nan")

print(f"\n{'='*66}\nRESULT — CONFIRMATION LATENCY\n{'='*66}")
print(f"confirmed: {len(conf_rows)}/{len(rows)}   NEVER confirmed (censored): {len(cens)}")
if conf_rows:
    L = [r["lat"] for r in conf_rows]
    print(f"latency minutes: median {st.median(L):.0f}   p25 {q(L,.25):.0f}   "
          f"p75 {q(L,.75):.0f}   max {max(L)}   min {min(L)}")
    for d in ("LONG", "SHORT"):
        sub = [r["lat"] for r in conf_rows if r["dir"] == d]
        allsub = [r for r in rows if r["dir"] == d]
        if sub:
            print(f"  {d:5} n={len(sub)}/{len(allsub)}  median {st.median(sub):.0f} min"
                  f"   p75 {q(sub,.75):.0f}")
        else:
            print(f"  {d:5} n=0/{len(allsub)}  NEVER confirmed")

    caps = [r["capture"] for r in conf_rows if r["capture"] is not None]
    if caps:
        print(f"\nCAPTURE FRACTION (share of the post-turn move still available at confirmation):")
        print(f"  median {pct(st.median(caps))}   p25 {pct(q(caps,.25))}   p75 {pct(q(caps,.75))}")
        print(f"  turns where >50% of the move was already gone: "
              f"{sum(1 for c in caps if c < 0.5)}/{len(caps)}")
        print(f"  turns where the ENTIRE move was gone (capture<=0): "
              f"{sum(1 for c in caps if c <= 0)}/{len(caps)}")

print(f"\nWHAT THE BIAS SAID *AT* THE TURN MINUTE:")
for b, n in collections.Counter(r["at_turn"] for r in rows).most_common():
    print(f"  {b:10} {n:3}  ({pct(n/len(rows))})")
print("  (agreeing with the post-turn direction at the turn itself: "
      f"{sum(1 for r in rows if r['at_turn']==r['dir'])}/{len(rows)})")

print(f"\nPER-TURN DETAIL (first 25):")
print(f"  {'sym':10} {'day':11} {'turn':6} {'dir':5} {'@turn':8} {'conf':6} {'lat':>5} {'move':>7} {'capture':>8}")
for r in rows[:25]:
    print(f"  {r['sym']:10} {r['day']:11} {r['t']:6} {r['dir']:5} {r['at_turn']:8} "
          f"{(r['conf_at'] or '-'):6} {(str(r['lat']) if r['lat'] is not None else 'never'):>5} "
          f"{pct(r['full']):>7} {(pct(r['capture']) if r['capture'] is not None else '-'):>8}")

# ── STRATIFY: was the bias ALREADY in the post-turn direction at the turn? ──
print(f"\n{'='*66}\nSTRATIFIED — the median-0 above is bias STICKINESS, not speed\n{'='*66}")
already = [r for r in rows if r["at_turn"] == r["dir"]]
notyet  = [r for r in rows if r["at_turn"] != r["dir"]]

print(f"\nA) bias ALREADY aligned at the turn: n={len(already)}")
print("   latency is 0 BY CONSTRUCTION — the engine held this view before the turn,")
print("   it did not detect it. All are LONG turns on a book that sits LONG.")
for r in already:
    print(f"     {r['sym']:10} {r['day']} {r['t']} {r['dir']:5} lat={r['lat']}")

print(f"\nB) bias NOT aligned at the turn — the real test: n={len(notyet)}")
cb = [r for r in notyet if r["lat"] is not None]
cn = [r for r in notyet if r["lat"] is None]
print(f"   confirmed: {len(cb)}    NEVER confirmed: {len(cn)}")
if cb:
    L = [r["lat"] for r in cb]
    C = [r["capture"] for r in cb if r["capture"] is not None]
    print(f"   latency  median {st.median(L):.0f} min   range {min(L)}-{max(L)}")
    if C:
        print(f"   capture  median {pct(st.median(C))}   range {pct(min(C))}-{pct(max(C))}")
        print(f"   => when the engine does NOT already hold the view, it arrives with")
        print(f"      ~{pct(1-st.median(C))} of the move already gone.")
    for r in cb:
        print(f"     {r['sym']:10} {r['day']} {r['t']} {r['dir']:5} @turn={r['at_turn']:8} "
              f"lat={r['lat']:>4}min  capture={pct(r['capture']) if r['capture'] is not None else '-'}")
for r in cn:
    print(f"     {r['sym']:10} {r['day']} {r['t']} {r['dir']:5} @turn={r['at_turn']:8} NEVER confirmed")

print(f"\nC) DIRECTIONAL ASYMMETRY")
for d in ("LONG", "SHORT"):
    sub = [r for r in rows if r["dir"] == d]
    okc = [r for r in sub if r["lat"] is not None]
    print(f"   {d:5} turns n={len(sub):2}  confirmed {len(okc)}  never {len(sub)-len(okc)}"
          + (f"  median lat {st.median([r['lat'] for r in okc]):.0f} min" if okc else ""))
wrong = [r for r in rows if r["at_turn"] not in ("NEUTRAL", "<none>", r["dir"])]
print(f"\n   bias actively WRONG-WAY at the turn (holding the opposite view): {len(wrong)}")
for r in wrong:
    print(f"     {r['sym']:10} {r['day']} {r['t']} turn={r['dir']:5} but bias={r['at_turn']}"
          f"  -> {'confirmed +%dmin' % r['lat'] if r['lat'] is not None else 'NEVER confirmed'}")
