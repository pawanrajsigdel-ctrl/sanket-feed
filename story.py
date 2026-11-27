"""Floorsheet story: for every stock, what the trades of each of the last days say about who bought, who sold, and how hard.
Reads data/floor/<day>.json.gz (every contract: buyer broker, seller broker, quantity, rate, id) and writes data/story/<SYMBOL>.json.gz.
Nothing here needs the database or any website: it only reads the archive in this repository.

Estimates, not facts about intent:
  * buying / selling pressure uses the tick rule: a trade above the previous trade's price counts as buyer-driven, below as seller-driven, equal keeps the last direction.
  * "sliced order" = one broker repeating same-sized small trades on one side, which is how a big order is usually hidden.
  * "flip" = a broker that was a net seller in the first half of the day's trades and a net buyer in the second half (or the other way round).
  * "wash" = trades where the buyer and the seller broker are the same.
"""
import gzip, json, os, re, sys, glob, statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
FLOOR = os.path.join(ROOT, "data", "floor")
OUT = os.path.join(ROOT, "data", "story")
KEEP = int(os.environ.get("STORY_DAYS", "60"))


def r2(x):
    return round(float(x), 2)


def day_story(rows):
    """rows: trades of ONE stock on ONE day, in time order (by contract id)."""
    n = len(rows)
    qty = sum(r[3] for r in rows)
    amt = sum(r[5] for r in rows)
    if not n or qty <= 0:
        return None
    # tick rule
    up = down = flat = 0.0
    last_dir = 0
    prev = None
    for r in rows:
        rate, q = r[4], r[3]
        if prev is not None:
            if rate > prev:
                last_dir = 1
            elif rate < prev:
                last_dir = -1
        if last_dir > 0:
            up += q
        elif last_dir < 0:
            down += q
        else:
            flat += q
        prev = rate
    buy = defaultdict(lambda: [0.0, 0.0, 0])   # broker -> qty, amount, trades
    sell = defaultdict(lambda: [0.0, 0.0, 0])
    wash = 0.0
    for r in rows:
        b, s, q, a = r[1], r[2], r[3], r[5]
        buy[b][0] += q; buy[b][1] += a; buy[b][2] += 1
        sell[s][0] += q; sell[s][1] += a; sell[s][2] += 1
        if b == s:
            wash += q
    brokers = set(buy) | set(sell)
    net = {k: (buy[k][0] - sell[k][0], buy[k][1] - sell[k][1]) for k in brokers if k in buy or k in sell}
    top = lambda d, k=5: [[b, int(v[0]), r2(v[1] / v[0]) if v[0] else 0] for b, v in sorted(d.items(), key=lambda kv: -kv[1][0])[:k]]
    share = lambda d, k=3: r2(sum(v[0] for _, v in sorted(d.items(), key=lambda kv: -kv[1][0])[:k]) / qty * 100)
    nb = [[b, int(v[0]), r2(buy[b][1] / buy[b][0]) if buy[b][0] else 0] for b, v in sorted(net.items(), key=lambda kv: -kv[1][0])[:6] if v[0] > 0]
    ns = [[b, int(-v[0]), r2(sell[b][1] / sell[b][0]) if sell[b][0] else 0] for b, v in sorted(net.items(), key=lambda kv: kv[1][0])[:6] if v[0] < 0]
    # sliced orders
    sliced = []
    for side, d, tag in (("B", buy, 1), ("S", sell, 2)):
        for b, v in d.items():
            if v[2] >= 8 and v[0] >= qty * 0.05:
                qs = [r[3] for r in rows if r[tag] == b]
                m = statistics.mean(qs)
                if m > 0 and statistics.pstdev(qs) / m <= 0.6:
                    sliced.append([b, side, v[2], int(m), int(v[0])])
    sliced.sort(key=lambda x: -x[4])
    # flips and timelines
    half = n // 2
    first = defaultdict(float); second = defaultdict(float)
    for i, r in enumerate(rows):
        t = first if i < half else second
        t[r[1]] += r[3]; t[r[2]] -= r[3]
    flips = []
    for b in brokers:
        a, c = first.get(b, 0), second.get(b, 0)
        if a * c < 0 and min(abs(a), abs(c)) >= qty * 0.03:
            flips.append([b, int(a), int(c)])
    flips.sort(key=lambda x: -(abs(x[1]) + abs(x[2])))
    busiest = sorted(brokers, key=lambda b: -(buy[b][0] + sell[b][0]))[:4]
    tl = []
    for b in busiest:
        cum = 0.0; pts = []
        step = max(1, n // 10)
        for i, r in enumerate(rows):
            if r[1] == b: cum += r[3]
            if r[2] == b: cum -= r[3]
            if (i + 1) % step == 0 or i == n - 1:
                pts.append(int(cum))
        tl.append([b, pts[:10] if len(pts) > 10 else pts])
    return {
        "trades": n, "qty": int(qty), "amt": int(amt), "vwap": r2(amt / qty), "open": rows[0][4], "close": rows[-1][4],
        "hi": max(r[4] for r in rows), "lo": min(r[4] for r in rows),
        "up": int(up), "down": int(down), "flat": int(flat), "wash": int(wash),
        "buyers": top(buy), "sellers": top(sell), "top3b": share(buy), "top3s": share(sell),
        "nb": nb, "ns": ns, "sliced": sliced[:4], "flips": flips[:3], "tl": tl,
    }


def main():
    files = sorted(glob.glob(os.path.join(FLOOR, "20*.json.gz")))[-KEEP:]
    if not files:
        print("no floorsheet files"); return 1
    per = defaultdict(dict)   # symbol -> day -> story
    for f in files:
        d = json.loads(gzip.open(f).read())
        day = d["d"]
        by = defaultdict(list)
        for r in d["rows"]:
            by[r[0]].append(r)
        for sym, rows in by.items():
            rows.sort(key=lambda r: int(r[7]) if str(r[7]).isdigit() else 0)
            s = day_story(rows)
            if s:
                per[sym][day] = s
    os.makedirs(OUT, exist_ok=True)
    keep = set()
    for sym, days in per.items():
        out = {"s": sym, "upto": max(days), "days": [dict(d=k, **v) for k, v in sorted(days.items())]}
        fn = re.sub(r"[^A-Z0-9_.-]", "_", sym.upper()) + ".json.gz"   # same file naming as the price and flow archives
        p = os.path.join(OUT, fn)
        with gzip.open(p, "wt", encoding="utf-8", compresslevel=9) as fh:
            json.dump(out, fh, separators=(",", ":"))
        keep.add(fn)
    for f in os.listdir(OUT):
        if f.endswith(".json.gz") and f not in keep:
            os.remove(os.path.join(OUT, f))
    with open(os.path.join(OUT, "index.json"), "w") as fh:
        json.dump({"upto": max(os.path.basename(f)[:10] for f in files), "days": [os.path.basename(f)[:10] for f in files], "symbols": sorted(per)}, fh, separators=(",", ":"))
    print(f"{len(per)} stocks, {len(files)} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
