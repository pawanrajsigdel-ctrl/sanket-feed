"""Keeps a free, permanent copy of every record the feed reads, as compressed files in this repository (data/events, data/fund, data/mf, data/shares).
The site reads them straight from GitHub, so they cost the database nothing, are never blocked by its daily limits, and Ask Sanket can search them.
feed.send() calls save(kind, body) BEFORE it posts to the API, so a blocked database never loses a record.
"""
import datetime as dt
import gzip
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(ROOT, "data")


def _read(path, default):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


def _safe(s):
    return re.sub(r"[^A-Z0-9_.-]", "_", str(s).upper())


def events(rows):
    """Calendar: AGM/SGM days, book close, bonus, dividend, right shares, IPO, FPO, auction, listing. One file per year of the event date."""
    by = {}
    for r in rows:
        d = str(r.get("d") or "")
        if len(d) == 10:
            by.setdefault(d[:4], []).append(r)
    for y, rs in by.items():
        p = os.path.join(D, "events", y + ".json.gz")
        cur = _read(p, {"year": y, "rows": []})
        seen = {(x["d"], x["t"]) for x in cur["rows"]}
        for r in rs:
            k = (r["d"], r["title"])
            if k not in seen:
                seen.add(k)
                cur["rows"].append({"d": r["d"], "t": r["title"], "u": r.get("url", "")})
        cur["rows"].sort(key=lambda x: (x["d"], x["t"]))
        _write(p, cur)
    ys = sorted(x[:4] for x in os.listdir(os.path.join(D, "events")) if x.endswith(".json.gz")) if os.path.isdir(os.path.join(D, "events")) else []
    json.dump({"years": ys, "updated": dt.datetime.utcnow().isoformat() + "Z"}, open(os.path.join(D, "events", "index.json"), "w"))


def fund(rows):
    """Company figures: latest of every company in one file, and a per-company history that grows only when a figure changes."""
    today = dt.datetime.utcnow().date().isoformat()
    p = os.path.join(D, "fund", "latest.json.gz")
    cur = _read(p, {"updated": "", "rows": {}})
    for r in rows:
        s = str(r.get("symbol", "")).upper()
        if not s:
            continue
        body = {k: v for k, v in r.items() if k != "symbol"}
        old = cur["rows"].get(s)
        if old is None or {k: v for k, v in old.items() if k != "asof"} != body:
            hp = os.path.join(D, "fund", "hist", _safe(s) + ".json.gz")
            h = _read(hp, {"s": s, "rows": []})
            h["rows"].append(dict(body, asof=today))
            _write(hp, h)
        cur["rows"][s] = dict(body, asof=today if old is None or {k: v for k, v in old.items() if k != "asof"} != body else old.get("asof", today))
    cur["updated"] = today
    _write(p, cur)


def mfnav(rows):
    """Mutual fund NAV per unit by month: one file for all funds, dated by the article day."""
    p = os.path.join(D, "mf", "nav.json.gz")
    cur = _read(p, {"rows": []})
    seen = {(x["d"], x["s"]) for x in cur["rows"]}
    for r in rows:
        k = (str(r.get("d")), str(r.get("symbol")))
        if k not in seen:
            seen.add(k)
            cur["rows"].append({"d": k[0], "s": k[1], "n": r.get("name", ""), "m": r.get("label", ""), "nav": r.get("nav"), "prev": r.get("prev")})
    cur["rows"].sort(key=lambda x: (x["d"], x["s"]))
    _write(p, cur)


def shares(rows):
    """Promoter, public and listed shares of every company, with the earlier figures kept when they change."""
    today = dt.datetime.utcnow().date().isoformat()
    p = os.path.join(D, "shares.json.gz")
    cur = _read(p, {"updated": "", "rows": {}})
    for r in rows:
        s = str(r.get("symbol", "")).upper()
        new = {"promoter": r.get("promoter"), "public": r.get("public"), "listed": r.get("listed")}
        old = cur["rows"].get(s)
        if old is None or any(old.get(k) != new[k] for k in new):
            prev = [] if old is None else (old.get("prev") or []) + [{k: old.get(k) for k in new} | {"until": today}]
            cur["rows"][s] = dict(new, asof=today, prev=prev[-12:])
    cur["updated"] = today
    _write(p, cur)


def save(kind, body):
    try:
        rows = body.get("rows") or []
        if rows and kind in HANDLERS:
            HANDLERS[kind](rows)
    except Exception as e:  # an archive problem must never stop the feed
        print("  ! archive %s: %r" % (kind, e))


HANDLERS = {"events": events, "fund": fund, "mfnav": mfnav, "shares": shares}
