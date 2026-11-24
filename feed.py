"""NEPSE feed: reads market data with the open-source NepseUnofficialApi library and posts it to the Techtara API.

Setup (once, on a computer or server that can reach nepalstock.com; running it in Nepal is fastest):
    pip install git+https://github.com/basic-bgnr/NepseUnofficialApi requests
    set NP_API=https://api.techtara.com.np   (your API address)
    set NP_KEY=the same value you gave:  npx wrangler secret put NEPSE_INGEST_KEY

Commands:
    python feed.py probe             prints what the library returns, so field names can be checked first
    python feed.py live              every 3 seconds during market hours (set NP_EVERY to change): only changed prices are sent
    python feed.py close             after 15:00: today's prices, index, brokers, floorsheet totals, depth, then analysis
    python feed.py auto              does everything by itself every day: live, then close, then history
    python feed.py longhistory       ten years of month-by-month prices for the pattern finder (public dataset)
    python feed.py history           one-time: price history of every stock back to 2020 (takes a while)

The library is unofficial and NEPSE can change its site at any time, so every field is read through pick(), which tries the
usual names. Run `probe` first; if a field shows None, tell me what probe printed and I will map it.
"""
import os, sys, time, json, threading, traceback, datetime as dt
import requests

API = os.environ.get("NP_API", "").rstrip("/")
KEY = os.environ.get("NP_KEY", "")
if not API or not KEY:
    sys.exit("Set NP_API and NP_KEY first.")

try:
    from nepse import Nepse
except Exception as e:  # pragma: no cover
    sys.exit("Install the library first: pip install git+https://github.com/basic-bgnr/NepseUnofficialApi  (%s)" % e)

nepse = Nepse()
nepse.setTLSVerification(False)  # NEPSE's certificate chain is incomplete
try:  # a stuck connection must fail in seconds, not hang for a minute
    import httpx
    nepse.client.timeout = httpx.Timeout(12.0, connect=6.0)
except Exception:
    pass


def pick(row, *names, default=None):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return default


def num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


class Budget(Exception):
    pass


def send(kind, **body):
    body["kind"] = kind
    for attempt in range(3):  # a dropped connection or a 5xx is tried again
        try:
            r = requests.post(API + "/api/np/ingest", headers={"x-np-key": KEY}, json=body, timeout=30)
        except Exception as e:
            print("  ! %s network: %r (try %d)" % (kind, e, attempt + 1))
            time.sleep(2)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 and "budget" in r.text:
            raise Budget()
        print("  !", kind, r.status_code, r.text[:120])
        if r.status_code < 500:
            return None
        time.sleep(2)
    return None


DEPTH_ON = False
AUTO = False


def now_np():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=5, minutes=45)


def market_open():
    n = now_np()
    return n.weekday() in (6, 0, 1, 2, 3) and dt.time(10, 55) <= n.time() <= dt.time(15, 5)  # Sunday to Thursday


def sectors():
    out = {}
    try:
        for c in nepse.getCompanyList():
            if str(pick(c, "status", default="A")) != "A":
                continue
            out[str(pick(c, "symbol", "stockSymbol", default="")).upper()] = (pick(c, "securityName", "companyName", default=""), pick(c, "sectorName", default=""))
    except Exception as e:
        print("company list failed:", e)
    return out


def push_symbols():
    rows = [{"symbol": s, "name": v[0], "sector": v[1]} for s, v in sectors().items() if s]
    for i in range(0, len(rows), 400):
        send("symbols", rows=rows[i:i + 400])
    print("symbols:", len(rows))


def live_raw():
    """The live-market list straight from NEPSE. The library may keep a short-lived copy of getLiveMarket, so go around it when possible."""
    try:
        return nepse.requestGETAPI(url=nepse.api_end_points["live_market_url"]) or []
    except Exception:
        return nepse.getLiveMarket() or []


def newest(data):
    return max((str(pick(r, "lastUpdatedDateTime", default="")) for r in data), default="")


last_stamp = [""]


def today_rows():
    data = live_raw()
    last_stamp[0] = newest(data)
    rows = []
    for r in data:
        s = str(pick(r, "symbol", default="")).upper()
        c = num(pick(r, "lastTradedPrice", "closePrice"))
        if not s or c <= 0:
            continue
        rows.append({"symbol": s, "o": num(pick(r, "openPrice")), "h": num(pick(r, "highPrice")), "l": num(pick(r, "lowPrice")), "c": c,
                     "prev": num(pick(r, "previousClose")), "v": num(pick(r, "totalTradeQuantity")), "t": num(pick(r, "totalTradeValue")),
                     "trades": 0, "chg": num(pick(r, "percentageChange"))})
    return rows


def cmd_probe():
    start = (now_np() - dt.timedelta(days=20)).strftime("%Y-%m-%d")
    end = now_np().strftime("%Y-%m-%d")
    long_start = (now_np() - dt.timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    last_day = (now_np() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    tests = (("history 3 years NABIL (paging info)", lambda: {k: (v if k != "content" else "%d rows" % len(v)) for k, v in nepse.getCompanyPriceVolumeHistory("NABIL", long_start, end).items()}),
             ("floorsheet NABIL", lambda: nepse.getFloorSheetOf("NABIL")),
             ("floorsheet NABIL for a date", lambda: nepse.getFloorSheetOf("NABIL", last_day)),
             ("depth NABIL", lambda: nepse.getSymbolMarketDepth("NABIL")))
    for label, fn in tests:
        try:
            d = fn()
            sample = d[0] if isinstance(d, list) and d else d
            print("\n==", label, "->", type(d).__name__, len(d) if hasattr(d, "__len__") else "")
            print(json.dumps(sample, default=str)[:900])
        except Exception as e:
            print("\n==", label, "FAILED:", repr(e))
            traceback.print_exc(limit=3)
    for name in ("getSummary", "getCompanyList"):
        try:
            d = getattr(nepse, name)()
            print("\n==", name, "->", type(d).__name__, len(d) if hasattr(d, "__len__") else "")
        except Exception as e:
            print("\n==", name, "FAILED:", repr(e))


def cmd_fresh():
    """Asks for the live market 6 times, 2 seconds apart, and shows whether each answer is new."""
    for i in range(6):
        t0 = time.time()
        a = live_raw()
        t1 = time.time() - t0
        t0 = time.time()
        b = nepse.getLiveMarket() or []
        print("raw: %.2fs newest %s | library: %.2fs newest %s | turnover raw %.0f" % (t1, newest(a)[11:23], time.time() - t0, newest(b)[11:23], sum(num(pick(r, "totalTradeValue")) for r in a)))
        time.sleep(2)


def cmd_floor():
    """Tries the whole-market floorsheet once (can take a minute) and prints the shape of one trade."""
    try:
        t0 = time.time()
        d = nepse.getFloorSheet()
        print("floorsheet:", type(d).__name__, len(d), "rows in %.0fs" % (time.time() - t0))
        print(json.dumps(d[0] if d else None, default=str)[:900])
    except Exception as e:
        print("floorsheet FAILED:", repr(e))
        traceback.print_exc(limit=4)


def depth_loop(symbols_fn):
    """Market depth (buy and sell queues) for the busiest stocks, in its own thread with its own connection."""
    n2 = Nepse()
    n2.setTLSVerification(False)
    while True:
        try:
            if market_open():
                for s in symbols_fn():
                    d = n2.getSymbolMarketDepth(s) or {}
                    md = d.get("marketDepth", {})
                    side = lambda rows: [{"p": num(x.get("orderBookOrderPrice")), "q": num(x.get("quantity")), "o": num(x.get("orderCount"))} for x in rows or []]
                    send("depth", symbol=s, bids=side(md.get("buyMarketDepthList")), asks=side(md.get("sellMarketDepthList")))
        except Exception as e:
            print("depth failed:", repr(e))
        time.sleep(120)


def sheet_agg():
    """Today's trades reduced to one line per stock: the price of its newest trade, total quantity, total amount and number of trades."""
    sheet = nepse.getFloorSheet() or []
    agg = {}
    for t in sheet:
        s = str(pick(t, "stockSymbol", "symbol", default="")).upper()
        if not s:
            continue
        a = agg.setdefault(s, {"q": 0.0, "a": 0.0, "n": 0, "at": "", "p": 0.0})
        a["q"] += num(pick(t, "contractQuantity"))
        a["a"] += num(pick(t, "contractAmount"))
        a["n"] += 1
        at = str(pick(t, "tradeTime", default=""))
        if at >= a["at"]:
            a["at"], a["p"] = at, num(pick(t, "contractRate"))
    return agg, len(sheet)


def overlay(rows, agg):
    """NEPSE's live-market list is only refreshed once a minute; the trades are newer, so the last trade price wins."""
    for r in rows:
        a = agg.get(r["symbol"])
        if a and a["p"] > 0 and a["q"] >= r["v"]:
            r["c"], r["v"], r["t"], r["trades"] = a["p"], a["q"], a["a"], a["n"]
            r["h"], r["l"] = max(r["h"], a["p"]), min(r["l"] or a["p"], a["p"])
            if r["prev"] > 0:
                r["chg"] = round((a["p"] / r["prev"] - 1) * 100, 2)
    return rows


def cmd_live(until_close=False):
    """Every NP_EVERY seconds (default 3): the last trade price of each stock from today's floorsheet, on top of NEPSE's once-a-minute
    market list. Only stocks that changed are sent, so the free database is not flooded; the whole market is saved again every 15 minutes.
    Set NP_TRADES=0 to use only the once-a-minute list."""
    every = max(1.0, float(os.environ.get("NP_EVERY", "3")))
    use_trades = os.environ.get("NP_TRADES", "1") != "0"
    push_symbols()
    seen, full_at, flow_at, busiest = {}, 0.0, 0.0, []
    global DEPTH_ON
    if not DEPTH_ON:
        DEPTH_ON = True
        threading.Thread(target=depth_loop, args=(lambda: list(busiest),), daemon=True).start()
    while True:
        if not market_open():
            if until_close:
                print("market closed", now_np().strftime("%H:%M"))
                return
            print("market closed", now_np().strftime("%H:%M"))
            seen.clear()
            time.sleep(60)
            continue
        t0 = time.time()
        try:
            rows = today_rows()
            n_trades = 0
            if use_trades:
                try:
                    agg, n_trades = sheet_agg()
                    rows = overlay(rows, agg)
                except Exception as e:
                    print("floorsheet skipped:", repr(e))
            full = time.time() - full_at > 900
            out = [r for r in rows if full or seen.get(r["symbol"]) != (r["c"], r["v"])]
            for r in rows:
                seen[r["symbol"]] = (r["c"], r["v"])
            busiest[:] = [r["symbol"] for r in sorted(rows, key=lambda r: -r["t"])[:8]]
            if out or full:
                flow = time.time() - flow_at > 60
                if flow:
                    flow_at = time.time()
                send("live", total=sum(r["t"] for r in rows), persist=full, flow=flow, rows=[{"symbol": r["symbol"], "ltp": r["c"], "chg": r["chg"], "v": r["v"], "t": r["t"]} for r in out])
                if full:
                    full_at = time.time()
            print(now_np().strftime("%H:%M:%S"), "changed", len(out), "of", len(rows), "| trades", n_trades, "| %.1fs" % (time.time() - t0), "| minute list", last_stamp[0][11:19])
        except Budget:
            print("daily write budget used up: live prices still reach the browsers; saving resumes after 05:45 Nepal time")
            full_at = time.time()
            time.sleep(60)
        except Exception as e:
            print("live failed:", repr(e))
            time.sleep(5)
        time.sleep(max(0.0, every - (time.time() - t0)))


def cmd_close():
    day = now_np().strftime("%Y-%m-%d")
    push_symbols()
    rows = today_rows()
    send("prices", d=day, rows=rows)
    try:
        idx = []
        for r in (nepse.getNepseIndex() or []) + (nepse.getNepseSubIndices() or []):
            name = str(pick(r, "index", "indexName", default="")).strip()
            if name.upper() in ("NEPSE INDEX", "NEPSE"):
                name = "NEPSE"
            idx.append({"name": name, "d": day, "value": num(pick(r, "currentValue", "close")), "chg": num(pick(r, "perChange", "percentageChange")), "turnover": 0})
        send("index", rows=[x for x in idx if x["name"] and x["value"] > 0])
    except Exception as e:
        print("index failed:", e)
    stored = 0
    try:  # floorsheet totals per broker and stock (the single trades are not stored)
        agg = {}
        for t in nepse.getFloorSheet() or []:
            s = str(pick(t, "stockSymbol", "symbol", default="")).upper()
            q, a = num(pick(t, "contractQuantity", "quantity")), num(pick(t, "contractAmount", "amount"))
            b, sl = int(num(pick(t, "buyerMemberId", "buyerBroker"))), int(num(pick(t, "sellerMemberId", "sellerBroker")))
            if not s:
                continue
            x = agg.setdefault((b, s), [0, 0, 0, 0]); x[0] += q; x[1] += a
            y = agg.setdefault((sl, s), [0, 0, 0, 0]); y[2] += q; y[3] += a
        out = [{"broker": k[0], "symbol": k[1], "buy_q": v[0], "buy_a": v[1], "sell_q": v[2], "sell_a": v[3]} for k, v in agg.items() if k[0] > 0]
        if not out:
            print("broker numbers are hidden by NEPSE until the market has closed; run close again after 15:00")
        for i in range(0, len(out), 400):
            send("floor", d=day, first=(i == 0), last=(i + 400 >= len(out)), force=bool(os.environ.get("NP_FORCE")), rows=out[i:i + 400])
        stored = len(out)
        print("floorsheet rows:", len(out))
    except Budget:
        raise
    except Exception as e:
        print("floorsheet failed:", e)
    print(send("close", d=day))
    return stored


def history_rows(symbol, start, end):
    d = nepse.getCompanyPriceVolumeHistory(symbol, start, end) or {}
    if isinstance(d, dict):
        pages = int(num(d.get("totalPages")) or 1)
        if pages > 1:
            print("  ! %s %s..%s has %d pages; only the first was read" % (symbol, start, end, pages))
        return d.get("content") or []
    return d


def cmd_history():
    """One-time: about six years of daily prices for every listed company. Takes an hour or more; it can be stopped and run again."""
    today = now_np().date()
    years = int(os.environ.get("NP_YEARS", "6"))
    chunks = []  # NEPSE returns about one year per request, so ask one year at a time
    for k in range(years, 0, -1):
        chunks.append(((today - dt.timedelta(days=365 * k)).strftime("%Y-%m-%d"), (today - dt.timedelta(days=365 * (k - 1) + (1 if k > 1 else 0))).strftime("%Y-%m-%d")))
    push_symbols()
    first = True
    have = {}
    try:
        have = requests.get(API + "/api/np/have", timeout=30).json().get("bars", {})
    except Exception as e:
        print("could not read what is already stored:", e)
    sec = sectors()
    try:  # busiest stocks first, so the analysis and picks work after the first day; funds, debentures and preference shares last
        turn = {r["symbol"]: r["t"] for r in today_rows()}
    except Exception:
        turn = {}
    def rank(sym):
        sector = str(sec[sym][1]).lower()
        late = any(w in sector for w in ("debenture", "mutual", "preference", "promoter", "fund"))
        return (late, -turn.get(sym, 0), sym)
    for s in sorted(sec, key=rank):
        if have.get(s, 0) >= 200 and not os.environ.get("NP_FORCE"):
            continue  # already stored; the free database allows only about 100,000 row writes a day
        if AUTO and market_open():
            return "paused"  # tomorrow's session is starting; history continues after the close
        if "/" in s:
            continue  # debenture series with a slash in the code cannot be addressed
        try:
            data = []
            for a, b in chunks:
                got = history_rows(s, a, b)
                if first:
                    print("  window %s..%s -> %d rows" % (a, b, len(got)))
                data += got
                time.sleep(0.2)
            first = False
            seen, bars = set(), []
            for r in sorted(data, key=lambda r: str(pick(r, "businessDate", default=""))):
                d = str(pick(r, "businessDate", default=""))[:10]
                c = num(pick(r, "closePrice", "lastTradedPrice"))
                if not d or d in seen or c <= 0:
                    continue
                seen.add(d)
                prev = bars[-1]["c"] if bars else 0
                nepse_prev = num(pick(r, "previousDayClosePrice", "previousClose", "previousDayClose"))  # NEPSE's own (bonus/right adjusted) previous close, when the row has it
                bars.append({"d": d, "o": prev or c, "h": num(pick(r, "highPrice")) or c, "l": num(pick(r, "lowPrice")) or c, "c": c, "prev": nepse_prev or prev,
                             "v": num(pick(r, "totalTradedQuantity")), "t": num(pick(r, "totalTradedValue")), "trades": num(pick(r, "totalTrades"))})
            for i in range(0, len(bars), 800):
                send("history", symbol=s, bars=bars[i:i + 800])
            print(s, len(bars))
        except Budget:
            print("Today's write budget is used up. Nothing is lost: it continues with the stocks not yet stored after 05:45 Nepal time.")
            return "budget"
        except Exception as e:
            print(s, "failed:", repr(e))
        time.sleep(0.5)
    return "done"


LONG_URL = "https://raw.githubusercontent.com/Aabishkar2/nepse-data/main/data/company-wise/%s.csv"


def parse_company_page(html):
    """Company figures from a public company page: earnings per share (with the quarter), P/E, book value, P/B, market value, 52-week range, latest dividend and bonus."""
    import re, html as H
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\\1>", " ", html)
    txt = H.unescape(re.sub(r"(?s)<[^>]+>", " ", h)).replace("\xa0", " ")
    txt = re.sub(r"\s+", " ", txt)
    N = r"(-?[\d,]+(?:\.\d+)?)"
    def f(m, i=1):
        return float(m.group(i).replace(",", "")) if m else None
    out = {}
    m = re.search(r"\bEPS\s*" + N + r"\s*\(\s*FY\s*:\s*([\d\-/]+)\s*,?\s*Q\s*:\s*(\d)\s*\)", txt)
    if m: out["eps"], out["fy"], out["q"] = f(m), m.group(2), int(m.group(3))
    else:
        m = re.search(r"\bEPS\s*" + N, txt)
        if m: out["eps"] = f(m)
    for key, pat in (("pe", r"P/E Ratio\s*"), ("bv", r"Book Value\s*"), ("pbv", r"\bPBV\s*"), ("mcap", r"Market Capitalization\s*"), ("shares", r"Shares Outstanding\s*")):
        m = re.search(pat + N, txt)
        if m: out[key] = f(m)
    m = re.search(r"52 Weeks High - Low\s*" + N + r"\s*-\s*" + N, txt)
    if m: out["hi52"], out["lo52"] = f(m, 1), f(m, 2)
    m = re.search(r"% Dividend\s*" + N + r"\s*\(\s*FY\s*:\s*([\d\-/]+)\s*\)", txt)
    if m: out["div_pct"], out["div_fy"] = f(m), m.group(2)
    m = re.search(r"% Bonus\s*" + N + r"\s*\(\s*FY\s*:\s*([\d\-/]+)\s*\)", txt)
    if m: out["bonus_pct"], out["bonus_fy"] = f(m), m.group(2)
    return out


def cmd_fundamentals():
    """EPS, P/E, book value, P/B, dividend and bonus for every listed company, read politely (one page about every second) from the public
    company pages on merolagani.com. Run it daily; `python feed.py fundamentals NABIL NICA` only prints what it reads, for a check."""
    only = [a.upper() for a in sys.argv[2:]]
    sec = sectors()
    symbols = only or sorted(s for s in sec if "/" not in s and not any(w in str(sec[s][1]).lower() for w in ("debenture", "preference", "promoter")))
    batch, got, miss = [], 0, []
    for i, s in enumerate(symbols):
        try:
            r = requests.get("https://merolagani.com/CompanyDetail.aspx?symbol=" + s, headers={"User-Agent": "Mozilla/5.0 (compatible; SanketFeed/1.0)"}, timeout=25)
            d = parse_company_page(r.text) if r.status_code == 200 else {}
        except Exception as e:
            d = {}
            print("  ! %s: %r" % (s, e))
        if d.get("eps") is not None or d.get("bv") is not None:
            d["symbol"] = s; batch.append(d); got += 1
            if only or got <= 3:
                print("  %s -> %s" % (s, {k: v for k, v in d.items() if k != "symbol"}))
        else:
            miss.append(s)
        if len(batch) >= 40 and not only:
            send("fund", rows=batch); batch = []
        time.sleep(1.0)
    if batch and not only:
        send("fund", rows=batch)
    print("fundamentals: %d read, %d without figures%s" % (got, len(miss), (" (e.g. " + ", ".join(miss[:8]) + ")") if miss else ""))


def cmd_longhistory():
    """Ten years (NP_LONG_YEARS) of month-by-month lowest, highest and closing prices for every stock, from the public dataset
    github.com/Aabishkar2/nepse-data (one CSV per company, MIT licence). It feeds the pattern finder; only 120 small rows per stock are stored."""
    import csv, io
    years = int(os.environ.get("NP_LONG_YEARS", "10"))
    first_ym = "%04d-%02d" % (now_np().year - years, now_np().month)
    have = {}
    try:
        have = requests.get(API + "/api/np/have", timeout=30).json().get("months", {})
    except Exception as e:
        print("could not read what is already stored:", e)
    sec = sectors()
    try:
        turn = {r["symbol"]: r["t"] for r in today_rows()}
    except Exception:
        turn = {}
    order = sorted(sec, key=lambda s: (any(w in str(sec[s][1]).lower() for w in ("debenture", "mutual", "preference", "promoter", "fund")), -turn.get(s, 0), s))
    done = 0
    for s in order:
        if "/" in s or (have.get(s, 0) >= years * 12 * 0.6 and not os.environ.get("NP_FORCE")):
            continue
        try:
            r = requests.get(LONG_URL % s, timeout=40)
            if r.status_code != 200:
                continue
            months = {}
            recs = []
            for row in csv.DictReader(io.StringIO(r.text)):
                d = str(row.get("published_date") or row.get("date") or "")[:10]
                c, hi, lo = num(row.get("close")), num(row.get("high")), num(row.get("low"))
                if len(d) == 10 and c > 0:
                    pcv = row.get("per_change")
                    recs.append([d, c, hi or c, lo or c, num(pcv) if str(pcv or "").strip() not in ("", "None", "null") else None])
            recs.sort()
            # Bonus and right shares: NEPSE's percentage change is measured from an adjusted previous close, so when it does not match yesterday's close
            # the difference is a corporate action; every earlier price is scaled by it (the newest prices stay as they are).
            k, events = 1.0, 0
            raw = [x[1] for x in recs]  # unscaled closes: the check below must not see the scaling from later events
            for i in range(len(recs) - 1, 0, -1):
                c, pc_ = raw[i], recs[i][4]
                if pc_ is not None and abs(pc_) < 100:  # 0% on the day of a bonus is exactly the case to catch
                    implied_prev = c / (1 + pc_ / 100.0)
                    ratio = implied_prev / raw[i - 1]
                    if abs(ratio - 1) > 0.02 and 0.3 < ratio < 3:
                        k *= ratio; events += 1
                if k != 1.0:
                    recs[i - 1][1] *= k; recs[i - 1][2] *= k; recs[i - 1][3] *= k
            if events:
                print("  %s: %d bonus/right adjustments" % (s, events))
            for d, c, hi, lo, _pc in recs:
                ym = d[:7]
                if len(ym) != 7 or ym < first_ym:
                    continue
                m = months.setdefault(ym, {"symbol": s, "ym": ym, "lo": 0, "hi": 0, "c": c, "_d": ""})
                hi, lo = hi or c, lo or c
                m["hi"] = max(m["hi"], hi); m["lo"] = lo if not m["lo"] else min(m["lo"], lo)
                if d >= m["_d"]:
                    m["_d"], m["c"] = d, c
            rows = [{k: v for k, v in m.items() if k != "_d"} for m in sorted(months.values(), key=lambda m: m["ym"])]
            for i in range(0, len(rows), 400):
                send("monthly", rows=rows[i:i + 400])
            done += 1
            print(s, len(rows), "months")
        except Budget:
            print("Today's write budget is used up; the rest continues tomorrow.")
            return "budget"
        except Exception as e:
            print(s, "failed:", repr(e))
        time.sleep(0.2)
    print("Long history finished for", done, "stocks this run.")
    return "done"


def cmd_auto():
    """Runs by itself, every day, with nothing to press: live prices while the market is open (Sunday to Thursday 11:00-15:00), then the daily close
    (retried every 5 minutes until NEPSE releases the broker numbers), then the price history until the day's free budget is used."""
    global AUTO
    AUTO = True
    closed_day = hist_day = None
    close_tries = 0
    print("Auto mode started. Leave this window open (or let the scheduled task run it).")
    while True:
        try:
            n = now_np()
            trading = n.weekday() in (6, 0, 1, 2, 3)
            if market_open():
                cmd_live(until_close=True)
                continue
            if trading and n.time() > dt.time(15, 0) and closed_day != n.date() and n.time() < dt.time(18, 30):
                try:
                    got = cmd_close()
                except Budget:
                    print("budget used up before the close could be saved; it will be saved tomorrow only if you run `close` again after 05:45")
                    got = -1
                close_tries += 1
                if got != 0 or close_tries >= 12:  # brokers stored, or an hour of tries
                    closed_day = n.date(); close_tries = 0
                    if os.environ.get("NP_EXIT_AFTER_CLOSE") == "1":
                        print("Close saved; finished for today.")
                        return
                else:
                    print("broker numbers not released yet; trying again in 5 minutes")
                    time.sleep(300)
                continue
            if hist_day != n.date() and not market_open() and os.environ.get("NP_NO_HISTORY") != "1":
                print("Loading price history...")
                r = cmd_history()
                if r in ("done", "budget"):
                    hist_day = n.date()
                continue
        except Exception as e:
            print("auto: error:", repr(e), "- continuing in a minute")
        time.sleep(60)


if __name__ == "__main__":
    {"probe": cmd_probe, "live": cmd_live, "close": cmd_close, "history": cmd_history, "fresh": cmd_fresh, "floor": cmd_floor, "auto": cmd_auto, "longhistory": cmd_longhistory, "fundamentals": cmd_fundamentals}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
