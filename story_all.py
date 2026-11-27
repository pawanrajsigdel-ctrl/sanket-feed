"""Whole-history floorsheet story: every trading day since 2015, for every stock and every broker.
Reads the open floorsheet dataset (one CSV per day: transaction_no, symbol, buyer, seller, quantity, rate, amount) and writes
  data/story/y/<year>/<SYMBOL>.json.gz   per stock, per year: the same daily record as story.py (pressure, top buyers/sellers, sliced, flips, timeline)
  data/broker/<ID>.json.gz               per broker: every day's total bought/sold and the stocks it was net buying / net selling most
  data/broker/index.json
Resumable: each day is cached in CACHE, so the script can be run again and again until it says "all days done".
  python story_all.py <path to nepse-history/data/floorsheet> [seconds budget] [--combine]
Trade order inside a day is the contract number (NEPSE numbers contracts in the order they happen), so "first half / later in the day" is exact even
though the dataset has no clock time.
"""
import gzip, json, os, re, sys, time
from collections import defaultdict
from multiprocessing import Pool

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from story import day_story  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("STORY_CACHE", "/tmp/storycache")
OUT = os.path.join(ROOT, "data", "story", "y")
BROKER = os.path.join(ROOT, "data", "broker")


def one_day(path):
    day = os.path.basename(path)[:10]
    out = os.path.join(CACHE, day + ".json")
    if os.path.exists(out):
        return day, 0
    try:
        df = pd.read_csv(path, usecols=["transaction_no", "symbol", "buyer", "seller", "quantity", "rate", "amount"],
                         dtype={"symbol": str, "transaction_no": "int64"})
    except Exception:
        df = None
    if df is None or len(df) < 200:        # holiday (empty file) or a broken day
        json.dump(None, open(out, "w"))
        return day, 0
    json.dump(build(df), open(out + ".tmp", "w"), separators=(",", ":"))
    os.replace(out + ".tmp", out)
    return day, len(df)


def build(df):
    """One day of trades (columns transaction_no, symbol, buyer, seller, quantity, rate, amount) -> {"s": stories per stock, "b": rows per broker}."""
    df = df.dropna(subset=["symbol"]).copy()
    df["buyer"] = pd.to_numeric(df["buyer"], errors="coerce").fillna(0).astype(int)
    df["seller"] = pd.to_numeric(df["seller"], errors="coerce").fillna(0).astype(int)
    df = df.sort_values("transaction_no")
    # ---- brokers: totals, and the stocks each was net buying / selling most
    bq = df.groupby(["buyer", "symbol"]).agg(q=("quantity", "sum"), a=("amount", "sum")).reset_index()
    sq = df.groupby(["seller", "symbol"]).agg(q=("quantity", "sum"), a=("amount", "sum")).reset_index()
    brokers = {}
    for r in bq.itertuples(index=False):
        b = brokers.setdefault(int(r.buyer), {"bq": 0, "ba": 0, "sq": 0, "sa": 0, "net": defaultdict(float)})
        b["bq"] += r.q; b["ba"] += r.a; b["net"][r.symbol] += r.q
    for r in sq.itertuples(index=False):
        b = brokers.setdefault(int(r.seller), {"bq": 0, "ba": 0, "sq": 0, "sa": 0, "net": defaultdict(float)})
        b["sq"] += r.q; b["sa"] += r.a; b["net"][r.symbol] -= r.q
    bro = {}
    for k, b in brokers.items():
        if k <= 0:
            continue
        nets = sorted(b["net"].items(), key=lambda kv: kv[1])
        bro[k] = [int(b["bq"]), int(b["ba"]), int(b["sq"]), int(b["sa"]),
                  [[s, int(q)] for s, q in nets[::-1][:3] if q > 0], [[s, int(-q)] for s, q in nets[:3] if q < 0]]
    # ---- stocks: the same daily record the stock page already uses
    stories = {}
    for sym, g in df.groupby("symbol", sort=False):
        rows = [[sym, int(b), int(s), float(q), float(r), float(a), "", i]
                for b, s, q, r, a, i in zip(g["buyer"], g["seller"], g["quantity"], g["rate"], g["amount"], g["transaction_no"])]
        s = day_story(rows)
        if s:
            stories[sym.upper()] = s
    return {"s": stories, "b": bro}


def safe(sym):
    return re.sub(r"[^A-Z0-9_.-]", "_", sym.upper())


def combine():
    per = defaultdict(lambda: defaultdict(dict))   # year -> symbol -> day -> story
    perb = defaultdict(dict)                       # broker -> day -> row
    days = []
    for fn in sorted(os.listdir(CACHE)):
        if not fn.endswith(".json"):
            continue
        d = json.load(open(os.path.join(CACHE, fn)))
        if not d:
            continue
        day = fn[:10]
        days.append(day)
        for sym, s in d["s"].items():
            per[day[:4]][sym][day] = s
        for b, row in d["b"].items():
            perb[b][day] = row
    os.makedirs(OUT, exist_ok=True); os.makedirs(BROKER, exist_ok=True)
    tot = 0
    for year, syms in per.items():
        os.makedirs(os.path.join(OUT, year), exist_ok=True)
        for sym, dd in syms.items():
            body = {"s": sym, "y": year, "days": [dict(d=k, **v) for k, v in sorted(dd.items())]}
            with gzip.open(os.path.join(OUT, year, safe(sym) + ".json.gz"), "wt", encoding="utf-8", compresslevel=9) as fh:
                json.dump(body, fh, separators=(",", ":"))
            tot += 1
    for b, dd in perb.items():
        body = {"b": int(b), "cols": ["d", "bq", "ba", "sq", "sa", "nb", "ns"], "rows": [[k] + v for k, v in sorted(dd.items())]}
        with gzip.open(os.path.join(BROKER, "%s.json.gz" % b), "wt", encoding="utf-8", compresslevel=9) as fh:
            json.dump(body, fh, separators=(",", ":"))
    with open(os.path.join(OUT, "index.json"), "w") as fh:
        json.dump({"from": days[0], "upto": days[-1], "n": len(days), "years": sorted(per)}, fh, separators=(",", ":"))
    with open(os.path.join(BROKER, "index.json"), "w") as fh:
        json.dump({"brokers": sorted(int(b) for b in perb), "from": days[0], "upto": days[-1]}, fh, separators=(",", ":"))
    print("combined: %d days, %d stock-year files, %d brokers" % (len(days), tot, len(perb)))


def daily():
    """Nightly: adds the days found in data/floor/ (the newest 120 trading days, every trade) that the history does not hold yet."""
    import glob
    idxp = os.path.join(OUT, "index.json")
    idx = json.load(open(idxp)) if os.path.exists(idxp) else {"from": "", "upto": "", "n": 0, "years": []}
    added = []
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "floor", "20*.json.gz"))):
        day = os.path.basename(f)[:10]
        if day <= idx["upto"]:
            continue
        d = json.loads(gzip.open(f).read())
        df = pd.DataFrame(d["rows"], columns=["symbol", "buyer", "seller", "quantity", "rate", "amount", "time", "transaction_no"])
        df["transaction_no"] = pd.to_numeric(df["transaction_no"], errors="coerce").fillna(0).astype("int64")
        if len(df) < 200:
            continue
        res = build(df)
        for sym, st in res["s"].items():
            p = os.path.join(OUT, day[:4], safe(sym) + ".json.gz")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            body = json.loads(gzip.open(p).read()) if os.path.exists(p) else {"s": sym, "y": day[:4], "days": []}
            body["days"] = [x for x in body["days"] if x["d"] != day] + [dict(d=day, **st)]
            body["days"].sort(key=lambda x: x["d"])
            with gzip.open(p, "wt", encoding="utf-8", compresslevel=9) as fh:
                json.dump(body, fh, separators=(",", ":"))
        for b, row in res["b"].items():
            p = os.path.join(BROKER, "%s.json.gz" % b)
            body = json.loads(gzip.open(p).read()) if os.path.exists(p) else {"b": int(b), "cols": ["d", "bq", "ba", "sq", "sa", "nb", "ns"], "rows": []}
            body["rows"] = [x for x in body["rows"] if x[0] != day] + [[day] + row]
            body["rows"].sort(key=lambda x: x[0])
            with gzip.open(p, "wt", encoding="utf-8", compresslevel=9) as fh:
                json.dump(body, fh, separators=(",", ":"))
        idx["upto"] = day; idx["n"] += 1
        if day[:4] not in idx["years"]:
            idx["years"].append(day[:4])
        added.append(day)
    json.dump(idx, open(idxp, "w"), separators=(",", ":"))
    bp = os.path.join(BROKER, "index.json")
    if os.path.exists(bp):
        bi = json.load(open(bp)); bi["upto"] = idx["upto"]; json.dump(bi, open(bp, "w"), separators=(",", ":"))
    print("history: %d new days %s" % (len(added), added[-3:]))


def main():
    if "--daily" in sys.argv:
        return daily()
    src = sys.argv[1]
    budget = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else 500
    os.makedirs(CACHE, exist_ok=True)
    paths = []
    for y in sorted(os.listdir(src)):
        for f in sorted(os.listdir(os.path.join(src, y))):
            if f.endswith(".csv") and not os.path.exists(os.path.join(CACHE, f[:10] + ".json")):
                paths.append(os.path.join(src, y, f))
    t0 = time.time()
    done = 0
    if paths:
        with Pool(2) as pool:
            for i in range(0, len(paths), 8):
                if time.time() - t0 > budget:
                    break
                for _ in pool.imap_unordered(one_day, paths[i:i + 8]):
                    done += 1
        left = len(paths) - done
        print("%d days done this run, %d left" % (done, max(left, 0)))
    if not paths or len(paths) - done <= 0:
        print("all days done")
    if "--combine" in sys.argv:
        combine()


if __name__ == "__main__":
    main()
