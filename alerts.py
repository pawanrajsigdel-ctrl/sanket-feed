"""Broker alerts for the latest trading day, written to data/alerts/latest.json (plain JSON, read by the Sanket page straight from GitHub).
For every stock it follows each broker's running net position over all days since 2015 (the daily top net buyers and sellers kept in data/story) and flags:
  selling  - a broker that built a position and is now net selling it
  outside  - a broker selling far more than it ever bought here, with little history in the stock (shares came from elsewhere)
  first    - a broker net buying a stock for the first time
  building - a broker still adding strongly to a position
These are patterns in the open floorsheet, which has no client ids. They are not proof of who is behind a broker, and not advice."""
import glob, gzip, json, os, re, sys, datetime as dt

WIN = 20


def load(sym):
    days = []
    for f in sorted(glob.glob("data/story/y/*/%s.json.gz" % sym)):
        try:
            days += json.load(gzip.open(f, "rt", encoding="utf-8")).get("days", [])
        except Exception:
            pass
    seen, out = set(), []
    for d in sorted(days, key=lambda x: x["d"]):
        if d["d"] not in seen:
            seen.add(d["d"]); out.append(d)
    return out


def analyse(sym, days):
    if len(days) < WIN + 10:
        return []
    cut = len(days) - WIN
    win_q = sum(d["qty"] for d in days[cut:])
    last = days[-1]
    px = last.get("close") or last.get("vwap") or 0
    B = {}
    for i, d in enumerate(days):
        inw = i >= cut
        for side, sign in (("nb", 1), ("ns", -1)):
            for row in d.get(side, []):
                b, q, a = row[0], row[1], (row[2] if len(row) > 2 else 0)
                x = B.setdefault(b, {"prior": 0, "win": 0, "cum": 0, "seen_before": 0, "buy_q": 0, "buy_v": 0, "sell_q": 0})
                x["cum"] += sign * q
                if inw:
                    x["win"] += sign * q
                else:
                    x["prior"] += sign * q
                    x["seen_before"] += 1
                if sign > 0:
                    x["buy_q"] += q; x["buy_v"] += q * a
                else:
                    x["sell_q"] += q
    out = []
    big = max(1.0, win_q * 0.01)
    for b, x in B.items():
        avg = x["buy_v"] / x["buy_q"] if x["buy_q"] else 0
        rec = {"s": sym, "b": b, "win": round(x["win"]), "prior": round(x["prior"]), "avg": round(avg, 1), "px": px,
               "pnl": round((px / avg - 1) * 100, 1) if avg and px else None, "val": round(abs(x["win"]) * px)}
        if x["prior"] > 0 and x["win"] < 0 and -x["win"] >= big and -x["win"] >= 0.1 * x["prior"]:
            out.append({**rec, "k": "selling"})
        if x["win"] < 0 and -x["win"] >= big and x["cum"] < 0 and x["buy_q"] < 0.25 * x["sell_q"] and x["seen_before"] <= 6:
            out.append({**rec, "k": "outside"})
        if x["prior"] <= 0 and x["seen_before"] == 0 and x["win"] > 0 and x["win"] >= 0.02 * win_q:
            out.append({**rec, "k": "first"})
        if x["prior"] > 0 and x["win"] > 0 and x["win"] >= 0.05 * win_q:
            out.append({**rec, "k": "building"})
    return out


def main():
    # ordinary shares only: promoter shares (…PO), preference shares (…P) and anything with digits (debentures, bonds, mutual funds) behave differently
    syms = sorted(s for s in (os.path.basename(f)[:-8] for f in glob.glob("data/story/*.json.gz") if not f.endswith("index.json")) if not re.search(r"\d|PO$|P$", s))
    items, day = [], ""
    for s in syms:
        days = load(s)
        if days:
            day = max(day, days[-1]["d"])
        items += analyse(s, days)
    # only stocks traded on the latest day, biggest rupee amounts first, at most 12 of each kind
    keep = []
    for k in ("selling", "outside", "first", "building"):
        keep += sorted([i for i in items if i["k"] == k], key=lambda i: -i["val"])[:12]
    os.makedirs("data/alerts", exist_ok=True)
    with open("data/alerts/latest.json", "w", encoding="utf-8") as f:
        json.dump({"d": day, "window": WIN, "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "items": keep}, f, separators=(",", ":"))
    print("alerts:", len(keep), "for", day, {k: sum(1 for i in keep if i["k"] == k) for k in ("selling", "outside", "first", "building")})


if __name__ == "__main__":
    main()
