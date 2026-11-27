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
    return n.weekday() in (6, 0, 1, 2, 3, 4) and dt.time(10, 55) <= n.time() <= dt.time(15, 5)  # Sunday to Friday


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
    seen, full_at, flow_at, index_at, subs_at, busiest = {}, 0.0, 0.0, 0.0, 0.0, []
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
            if time.time() - index_at > 45:  # the NEPSE index moves every minute; the sector indices follow every 5 minutes
                index_at = time.time()
                every5 = time.time() - subs_at > 290
                if every5:
                    subs_at = time.time()
                push_index(now_np().strftime("%Y-%m-%d"), main_only=not every5)
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


def push_index(day, main_only=False):
    """The NEPSE index and the sector indices as they stand now (while the market is open) or at the close."""
    try:
        idx = []
        for r in (nepse.getNepseIndex() or []) + ([] if main_only else (nepse.getNepseSubIndices() or [])):
            name = str(pick(r, "index", "indexName", default="")).strip()
            if name.upper() in ("NEPSE INDEX", "NEPSE"):
                name = "NEPSE"
            idx.append({"name": name, "d": day, "value": num(pick(r, "currentValue", "close")), "chg": num(pick(r, "perChange", "percentageChange")), "turnover": 0})
        send("index", rows=[x for x in idx if x["name"] and x["value"] > 0])
    except Budget:
        raise
    except Exception as e:
        print("index failed:", e)


def cmd_close():
    day = now_np().strftime("%Y-%m-%d")
    push_symbols()
    rows = today_rows()
    send("prices", d=day, rows=rows)
    push_index(day)
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
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
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


def parse_events_page(html):
    """Event headlines from one day of the public events calendar: the linked headlines first; if the page has no such links, the lines that sit in front of a date."""
    import re, html as H
    out, seen = [], set()
    for m in re.finditer(r"(?is)<a\b[^>]*href=[\"']([^\"']*eventdetail/[^\"']+)[\"'][^>]*>(.*?)</a>", html):
        t = re.sub(r"\s+", " ", H.unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(2)))).strip()
        if len(t) >= 6 and t not in seen:
            seen.add(t); u = m.group(1); out.append({"title": t, "url": ("https://www.sharesansar.com" + u) if u.startswith("/") else u})
    if out:
        return out
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"\s+", " ", H.unescape(re.sub(r"(?s)<[^>]+>", " ", h)))
    i = txt.find("Financial Events")
    txt = txt[i + len("Financial Events"):] if i >= 0 else txt
    for m in re.finditer(r"([A-Z0-9][^|]{8,220}?)\s+(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday), [A-Z][a-z]+ \d{1,2}, \d{4}", txt):
        t = m.group(1).strip()
        if t not in seen and not t.lower().startswith(("home", "login")):
            seen.add(t); out.append({"title": t, "url": ""})
    return out


def cmd_events():
    """Calendar of AGM days, book-close dates, right shares, IPOs, auctions and listings, one page per day from the public events calendar.
    `python feed.py events` reads from 7 days ago to 75 days ahead (daily run); `events 400` reads 400 days back too (one-off, to fill in the past);
    add `dry` to only print what is read and store nothing."""
    args = [a for a in sys.argv[2:]]
    dry = "dry" in args
    nums = [int(a) for a in args if a.isdigit()]
    back = nums[0] if nums else 7
    ahead = nums[1] if len(nums) > 1 else 75
    today = now_np().date()
    batch, pages, total, empty = [], 0, 0, 0
    for off in range(-back, ahead + 1):
        d = (today + dt.timedelta(days=off)).isoformat()
        try:
            r = requests.get("https://www.sharesansar.com/events/" + d, headers={"User-Agent": "Mozilla/5.0 (compatible; SanketFeed/1.0)"}, timeout=25)
            ev = parse_events_page(r.text) if r.status_code == 200 else []
            if r.status_code != 200:
                print("  ! %s HTTP %s" % (d, r.status_code))
        except Exception as e:
            ev = []
            print("  ! %s: %r" % (d, e))
        pages += 1
        if not ev:
            empty += 1
        for e in ev:
            batch.append({"d": d, "title": e["title"], "url": e.get("url", "")}); total += 1
        if (dry and pages <= 3) or (not dry and pages == 1):
            print("  %s -> %d events%s" % (d, len(ev), "; e.g. " + " | ".join(x["title"][:70] for x in ev[:3]) if ev else ""))
        if len(batch) >= 150 and not dry:
            send("events", rows=batch); batch = []
        time.sleep(0.6)
    if batch and not dry:
        send("events", rows=batch)
    print("events: %d days read, %d events%s, %d days without events" % (pages, total, " (dry run, nothing stored)" if dry else " stored", empty))


FLOOR_KEEP = 120


def floor_index():
    """Rewrites data/floor/index.json from the day files present (newest FLOOR_KEEP kept) and returns the list of days."""
    days = sorted(x[:-8] for x in os.listdir("data/floor") if x.endswith(".json.gz") and len(x) == 18)
    for old in days[:-FLOOR_KEEP]:
        os.remove("data/floor/%s.json.gz" % old)
    days = days[-FLOOR_KEEP:]
    json.dump({"days": days, "updated": dt.datetime.utcnow().isoformat() + "Z"}, open("data/floor/index.json", "w"))
    return days


def hhmmss(v):
    t = str(v or "")
    t = t.split("T")[-1] if "T" in t else t.split(" ")[-1]
    return t[:8]


def floor_row(t, fallback=""):
    s = str(pick(t, "stockSymbol", "symbol", default=fallback) or fallback).upper()
    return [s, int(num(pick(t, "buyerMemberId", "buyerBroker"))), int(num(pick(t, "sellerMemberId", "sellerBroker"))), num(pick(t, "contractQuantity", "quantity")),
            num(pick(t, "contractRate", "rate")), num(pick(t, "contractAmount", "amount")), hhmmss(pick(t, "tradeTime", "contractTime", "businessDate", default="")), str(pick(t, "contractId", "id", default=""))]


OPEN_FLOOR = "https://raw.githubusercontent.com/rajeevpaudel/nepse-history/main/data/floorsheet/%s/%s.csv"
OPEN_HIST = "https://raw.githubusercontent.com/menaceXnadin/nepse-historical-market-data-csv/master/data/"


def cmd_floorback():
    """Fills in past days of the floorsheet archive from the open NEPSE floorsheet dataset (NEPSE itself only serves the current day).
    `python feed.py floorback 60` saves the 60 most recent missing trading days, newest first; days already saved are skipped. Run again to go further back."""
    import csv, gzip, io
    want = int((sys.argv[2:] or ["30"])[0])
    os.makedirs("data/floor", exist_ok=True)
    have = set(x[:-8] for x in os.listdir("data/floor") if x.endswith(".json.gz"))
    today = now_np().date()
    done = missing = 0
    for back in range(1, 800):
        d = today - dt.timedelta(days=back)
        ds = d.isoformat()
        if d.weekday() in (5,) or ds in have:
            continue
        if done >= want or missing >= 25:
            break
        try:
            r = requests.get(OPEN_FLOOR % (ds[:4], ds), timeout=90)
        except Exception as e:
            print("%s: %r" % (ds, e))
            missing += 1
            continue
        if r.status_code == 404:
            print("%s: not in the dataset (holiday or not added yet)" % ds)
            missing += 1
            continue
        if r.status_code != 200:
            print("%s: HTTP %s" % (ds, r.status_code))
            missing += 1
            continue
        missing = 0
        rows = []
        for t in csv.DictReader(io.StringIO(r.text)):
            try:
                rows.append([str(t["symbol"]).upper(), int(float(t["buyer"] or 0)), int(float(t["seller"] or 0)), float(t["quantity"]), float(t["rate"]), float(t["amount"]), "", str(t["transaction_no"])])
            except Exception:
                continue
        if len(rows) < 500:
            print("%s: only %d trades, skipped" % (ds, len(rows)))
            continue
        body = json.dumps({"d": ds, "cols": ["symbol", "buyer", "seller", "qty", "rate", "amount", "time", "id"], "rows": rows}, separators=(",", ":"))
        with gzip.open("data/floor/%s.json.gz" % ds, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write(body)
        have.add(ds)
        done += 1
        print("%s: %d trades saved (%d of %d)" % (ds, len(rows), done, want))
    kept = floor_index()
    print("floorback finished: %d days added, %d days in the archive" % (done, len(kept)))


def cmd_openhist():
    """One-off import of history from open datasets: every company's reported quarters (profit, EPS, net worth, ROE) since FY 073/074 and the sector index
    history (last 400 days). `python feed.py openhist` imports both; `openhist quarters` or `openhist index` imports one."""
    import csv, io
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    if only in ("", "quarters"):
        r = requests.get(OPEN_HIST + "quarterly_reports.csv", timeout=90)
        r.raise_for_status()
        rows = []
        for t in csv.DictReader(io.StringIO(r.text)):
            q = str(t.get("quarter", "")).lower().replace("q", "")
            if not t.get("symbol") or not q.isdigit():
                continue
            rows.append({"symbol": t["symbol"], "fy": t["fiscal_year"], "q": int(q), "eps": t.get("eps", ""), "net_profit": t.get("net_profit", ""), "net_worth": t.get("net_worth", ""), "roe": t.get("roe", ""),
                         "roa": t.get("roa", ""), "paidup": t.get("paidup_capital", ""), "assets": t.get("total_assets", ""), "deposit": t.get("deposit", "")})
        print("quarterly reports: %d rows read" % len(rows))
        for i in range(0, len(rows), 250):
            send("quarters", rows=rows[i:i + 250])
        print("quarterly reports stored")
    if only in ("", "index"):
        codes = ["NEPSE", "BANKING", "DEVBANK", "FINANCE", "HOTELS", "HYDROPOWER", "INVESTMENT", "LIFEINSU", "NONLIFEINSU", "MANUFACTURE", "MICROFINANCE", "MUTUAL", "OTHERS", "TRADING"]
        cutoff = (now_np().date() - dt.timedelta(days=400)).isoformat()
        for code in codes:
            folder = "index" if code == "NEPSE" else "subindices"
            r = requests.get("%s%s/adjusted/%s_adjusted.csv" % (OPEN_HIST, folder, code), timeout=60)
            if r.status_code != 200:
                print("  %s: HTTP %s" % (code, r.status_code))
                continue
            rows = []
            prev = None
            for t in csv.DictReader(io.StringIO(r.text)):
                d = str(t["time"])[:10]
                v = float(t["close"] or 0)
                if d >= cutoff and v > 0:
                    rows.append({"code": code, "d": d, "value": v, "chg": round((v / prev - 1) * 100, 2) if prev else 0, "turnover": float(t.get("volume") or 0) if code == "NEPSE" else 0})
                prev = v if v > 0 else prev
            for i in range(0, len(rows), 250):
                send("indexhist", rows=rows[i:i + 250])
            print("  %s: %d days sent" % (code, len(rows)))
    print("open history import finished")


def parse_mfnav(html):
    """Rows of the monthly mutual fund NAV table: [{symbol, name, label, nav, prev}]. The table header reads 'NAV as of <month>' then 'NAV as of <previous month>'."""
    import re, html as H
    out, label = [], ""
    m = re.search(r"NAV as of\s+([A-Za-z]+)", H.unescape(re.sub(r"(?s)<[^>]+>", " ", html)))
    if m:
        label = m.group(1)
    for tr in re.findall(r"(?is)<tr\b.*?</tr>", html):
        cells = [re.sub(r"\s+", " ", H.unescape(re.sub(r"(?s)<[^>]+>", " ", c))).strip() for c in re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", tr)]
        if len(cells) < 5:
            continue
        for i, c in enumerate(cells):
            if re.fullmatch(r"[A-Z0-9]{3,10}", c) and re.search(r"[A-Z]", c) and i + 2 < len(cells):
                try:
                    nav, prev = float(cells[i + 1].replace(",", "")), float(cells[i + 2].replace(",", ""))
                except ValueError:
                    break
                if nav > 0:
                    out.append({"symbol": c, "name": cells[i - 1] if i else "", "label": label, "nav": nav, "prev": prev})
                break
    return out


MFNAV_SEED = """
mangsir 2024-01-02; shrawan 2023-09-04; shrawan 2024-09-05; kartik 2024-12-02; mangsir 2025-01-01; poush 2025-01-30
kartik 2025-12-02; mangsir 2025-12-31; poush 2026-02-02; magh 2026-03-01; falgun 2026-03-31; chaitra 2026-05-03
baisakh 2026-06-01; jestha 2026-06-30; ashad 2026-08-03; shrawan 2026-09-02; ashwin 2026-11-03
""".replace("\n", ";")
MFNAV_SEED_URLS = {
    ("mangsir", "2025-01-01"): "mangsir-insights-mutual-fund-trends-in-nepal-sector-allocations-top-stocks-and-nav-movements-unveiled-2025-01-01",
    ("poush", "2025-01-30"): "overview-of-mutual-fund-trends-in-nepal-sector-wise-investments-top-stocks-and-nav-changes-in-poush-2025-01-30",
    ("ashad", "2026-08-03"): "how-did-the-mutual-funds-perform-in-ashad-a-complete-analysis-of-multiple-factors-2026-08-03",
}


def mfnav_seed_links():
    out = []
    for part in MFNAV_SEED.split(";"):
        part = part.strip()
        if not part:
            continue
        mon, day = part.split()
        slug = MFNAV_SEED_URLS.get((mon, day)) or "know-how-did-the-mutual-funds-perform-in-%s-a-complete-analysis-of-multiple-factors-%s" % (mon, day)
        out.append("https://www.sharesansar.com/newsdetail/" + slug)
    return out


def cmd_mfnav():
    """Monthly NAV per unit of every mutual fund, from the public monthly mutual fund analysis articles.
    `python feed.py mfnav` reads the newest 2 articles; `mfnav 12` the newest 12; `mfnav all` also reads every known monthly article back to 2023 (one-off); add `dry` to print only."""
    import re
    args = sys.argv[2:]
    dry = "dry" in args
    nums = [int(a) for a in args if a.isdigit()]
    want = 999 if "all" in args else (nums[0] if nums else 2)
    H = {"User-Agent": "Mozilla/5.0 (compatible; SanketFeed/1.0)"}
    links, seen = [], set()
    if "all" in args:
        links = mfnav_seed_links(); seen = set(links)
    for page in ("https://www.sharesansar.com/category/mutual-fund", "https://www.sharesansar.com/category/mutual-fund?page=2", "https://www.sharesansar.com/category/mutual-fund?page=3", "https://www.sharesansar.com/category/mutual-fund?page=4", "https://www.sharesansar.com/category/mutual-fund?page=5", "https://www.sharesansar.com/category/mutual-fund?page=6"):
        try:
            r = requests.get(page, headers=H, timeout=30)
        except Exception as e:
            print("list page failed:", repr(e))
            continue
        print(page, "->", r.status_code, len(r.text))
        for u in re.findall(r"""href=["']([^"']*newsdetail/[^"']+)["']""", r.text):
            if u not in seen and re.search(r"(perform|nav-changes|monthly-analysis|mutual-fund-trends|mutual-funds?-in|insights)", u) and re.search(r"\d{4}-\d{2}-\d{2}", u):
                seen.add(u); links.append(u if u.startswith("http") else "https://www.sharesansar.com" + u)
        if len(links) >= want * 2:
            break
    print("%d article links to try" % len(links))
    got = 0
    used = set()
    for u in sorted(links, key=lambda x: re.findall(r"\d{4}-\d{2}-\d{2}", x)[-1], reverse=True):
        if got >= want:
            break
        day = re.findall(r"\d{4}-\d{2}-\d{2}", u)[-1]
        try:
            r = requests.get(u, headers=H, timeout=40)
            rows = parse_mfnav(r.text)
        except Exception as e:
            print(day, "failed:", repr(e))
            continue
        if len(rows) < 10:
            print(day, "no NAV table found (%d rows) %s" % (len(rows), u))
            continue
        label = rows[0]["label"]
        if day in used:
            continue
        used.add(day)
        print("%s: %s, %d funds (%s)" % (day, label, len(rows), u))
        if not dry:
            for i in range(0, len(rows), 100):
                send("mfnav", rows=[dict(x, d=day) for x in rows[i:i + 100]])
        got += 1
    print("mfnav: %d months read" % got)


def flow_day(text):
    """One day of the open floorsheet (CSV text) boiled down per stock: trades, shares, turnover, high and low rate, and the 5 brokers who bought and sold most.
    Returns {symbol: [trades, qty, amount, hi, lo, [[broker, qty, avg_rate]...buyers], [[broker, qty, avg_rate]...sellers]]}."""
    import csv, io
    agg = {}
    for t in csv.DictReader(io.StringIO(text)):
        try:
            sy = str(t["symbol"]).upper(); q = float(t["quantity"]); r = float(t["rate"]); b = int(float(t["buyer"] or 0)); sl = int(float(t["seller"] or 0))
        except Exception:
            continue
        a = agg.setdefault(sy, [0, 0.0, 0.0, 0.0, 1e18, {}, {}])
        a[0] += 1; a[1] += q; a[2] += q * r; a[3] = max(a[3], r); a[4] = min(a[4], r)
        x = a[5].setdefault(b, [0.0, 0.0]); x[0] += q; x[1] += q * r
        y = a[6].setdefault(sl, [0.0, 0.0]); y[0] += q; y[1] += q * r
    out = {}
    for sy, a in agg.items():
        top = lambda m: [[k, int(v[0]), round(v[1] / v[0], 2)] for k, v in sorted(m.items(), key=lambda kv: -kv[1][0])[:5]]
        out[sy] = [a[0], int(a[1]), round(a[2]), a[3], a[4] if a[4] < 1e17 else 0, top(a[5]), top(a[6])]
    return out


def cmd_flowhist():
    """Whole history of who bought and sold each stock, from the open floorsheet dataset (2015 onwards), kept small: one file per stock and year in data/flow/<year>/
    with one line per trading day (trades, shares, turnover, high, low and the top 5 buying and selling brokers), in one folder per year.
    `python feed.py flowhist 2025` does one year; `flowhist 2023 2026` does 2023 to 2026. Days already saved are skipped, so it can be run again."""
    import gzip, re
    from concurrent.futures import ThreadPoolExecutor
    a = [int(x) for x in sys.argv[2:] if x.isdigit()]
    y0, y1 = (a[0], a[1] if len(a) > 1 else a[0]) if a else (now_np().year, now_np().year)
    cols = ["d", "trades", "qty", "amount", "hi", "lo", "buyers", "sellers"]
    # One folder per year (data/flow/<year>/<SYMBOL>.json.gz), so runs for different years never touch the same files.
    fname = lambda sy, yr: "data/flow/%d/%s.json.gz" % (yr, re.sub(r"[^A-Z0-9_.-]", "_", sy))
    def load(sy, yr):
        f = fname(sy, yr)
        if os.path.exists(f):
            try:
                return {r[0]: r for r in json.load(gzip.open(f, "rt", encoding="utf-8"))["rows"]}
            except Exception:
                pass
        return {}
    today = now_np().date()
    for year in range(y0, y1 + 1):
        os.makedirs("data/flow/%d" % year, exist_ok=True)
        done_f = "data/flow/%d/_days.json" % year
        done = set(json.load(open(done_f))) if os.path.exists(done_f) else set()
        days = []
        d = dt.date(year, 1, 1)
        while d.year == year and d <= today:
            if d.weekday() not in (5,) and d.isoformat() not in done:
                days.append(d.isoformat())
            d += dt.timedelta(days=1)
        print("%d: %d days to read" % (year, len(days)))
        fresh = {}
        def get(ds):
            for _ in range(3):
                try:
                    r = requests.get(OPEN_FLOOR % (ds[:4], ds), timeout=120)
                    if r.status_code == 404:
                        return ds, None
                    if r.status_code == 200:
                        return ds, flow_day(r.text)
                except Exception:
                    time.sleep(2)
            return ds, False
        with ThreadPoolExecutor(max_workers=6) as ex:
            for ds, res in ex.map(get, days):
                if res is False:
                    print("  %s: could not read, will retry next run" % ds)
                    continue
                done.add(ds)
                if res:
                    for sy, v in res.items():
                        fresh.setdefault(sy, {})[ds] = [ds] + v
        for sy, rows in fresh.items():
            cur = load(sy, year); cur.update(rows)
            with gzip.open(fname(sy, year), "wt", encoding="utf-8", compresslevel=9) as f:
                json.dump({"s": sy, "y": year, "cols": cols, "rows": [cur[k] for k in sorted(cur)]}, f, separators=(",", ":"))
        json.dump(sorted(done), open(done_f, "w"))
        print("%d finished: %d days read, %d stocks updated" % (year, len(days), len(fresh)))
    total = sum(len([x for x in os.listdir("data/flow/" + yd) if x.endswith(".gz")]) for yd in os.listdir("data/flow") if os.path.isdir("data/flow/" + yd))
    print("flowhist finished; %d stock-year files in data/flow" % total)


def cmd_pricehist():
    """All daily prices from the open datasets, kept in this repo as small files: data/prices/<SYMBOL>.json.gz (every stock, from the first day the dataset has,
    1995 for the oldest) and data/index/<CODE>.json.gz (NEPSE, float, sensitive and every sector index, from 1997).
    `python feed.py pricehist <folder of nepse-data> <folder of nepse-historical-market-data-csv>`; the workflow downloads both folders first."""
    import csv, gzip, re
    a = [x for x in sys.argv[2:] if not x.isdigit()]
    aab, men = (a + ["", ""])[:2]
    os.makedirs("data/prices", exist_ok=True); os.makedirs("data/index", exist_ok=True)
    safe = lambda sy: re.sub(r"[^A-Z0-9_.-]", "_", sy)
    num = lambda v: (None if str(v).strip().lower() in ("", "nan", "none") else float(v))
    n = rows_total = 0
    for f in sorted(os.listdir(os.path.join(aab, "data/company-wise"))) if aab else []:
        if not f.endswith(".csv"):
            continue
        sy = f[:-4].upper(); out = []; prev = None
        for t in csv.DictReader(open(os.path.join(aab, "data/company-wise", f), encoding="utf-8")):
            try:
                d = t["published_date"][:10]; o, h, l, c = (num(t[k]) for k in ("open", "high", "low", "close"))
            except Exception:
                continue
            if not d or c is None:
                continue
            chg = num(t.get("per_change"))
            if chg is None and prev:
                chg = round((c / prev - 1) * 100, 2)
            out.append([d, o, h, l, c, chg if chg is not None else 0, int(num(t.get("traded_quantity")) or 0), round(num(t.get("traded_amount")) or 0)])
            prev = c
        out.sort(key=lambda r: r[0])
        if out:
            with gzip.open("data/prices/%s.json.gz" % safe(sy), "wt", encoding="utf-8", compresslevel=9) as g:
                json.dump({"s": sy, "cols": ["d", "o", "h", "l", "c", "chg", "qty", "amt"], "rows": out}, g, separators=(",", ":"))
            n += 1; rows_total += len(out)
    print("prices: %d stocks, %d rows, first day %s" % (n, rows_total, min([json.load(gzip.open("data/prices/" + x, "rt"))["rows"][0][0] for x in os.listdir("data/prices")] or [""])))
    k = 0
    for folder in ("index", "subindices") if men else ():
        d0 = os.path.join(men, "data", folder, "adjusted")
        for f in sorted(os.listdir(d0)) if os.path.isdir(d0) else []:
            if not f.endswith("_adjusted.csv"):
                continue
            code = f[:-13].upper(); out = []
            for t in csv.DictReader(open(os.path.join(d0, f), encoding="utf-8")):
                try:
                    out.append([t["time"][:10], num(t["open"]), num(t["high"]), num(t["low"]), num(t["close"]), int(num(t.get("volume")) or 0)])
                except Exception:
                    continue
            out.sort(key=lambda r: r[0])
            if out:
                with gzip.open("data/index/%s.json.gz" % safe(code), "wt", encoding="utf-8", compresslevel=9) as g:
                    json.dump({"s": code, "cols": ["d", "o", "h", "l", "c", "vol"], "rows": out}, g, separators=(",", ":"))
                k += 1
                print("  index %s: %d days from %s" % (code, len(out), out[0][0]))
    print("price history finished: %d stocks, %d indices" % (n, k))


def find_key(o, names):
    """First value found under any of the given (lower-case) key names, searching nested dictionaries."""
    if isinstance(o, dict):
        for k, v in o.items():
            if str(k).lower() in names and v not in (None, ""):
                return v
        for v in o.values():
            r = find_key(v, names)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_key(v, names)
            if r is not None:
                return r
    return None


def cmd_shares():
    """Promoter and public shares of every company, from the exchange's own company details. `python feed.py shares NABIL` prints one company's raw fields to check."""
    only = [a.upper() for a in sys.argv[2:]]
    syms = only or sorted(s for s in sectors() if "/" not in s)
    rows, miss, shown = [], [], 0
    for s in syms:
        try:
            d = nepse.getCompanyDetails(s)
        except Exception:
            miss.append(s)
            continue
        if only or shown == 0:
            sec = d.get("security") if isinstance(d, dict) and isinstance(d.get("security"), dict) else d
            print("  %s fields: %s" % (s, sorted(sec.keys())[:60] if isinstance(sec, dict) else type(d).__name__))
            shown += 1
        p = num(find_key(d, {"promotershares"}))
        u = num(find_key(d, {"publicshares"}))
        li = num(find_key(d, {"stocklistedshares", "listedshares"}))
        if p > 0 or u > 0:
            rows.append({"symbol": s, "promoter": p, "public": u, "listed": li or p + u})
            if only or len(rows) <= 3:
                print("  %s -> promoter %s, public %s, listed %s" % (s, p, u, li))
        else:
            miss.append(s)
        time.sleep(0.15)
    if rows and not only:
        for i in range(0, len(rows), 150):
            send("shares", rows=rows[i:i + 150])
    print("shares: %d companies with a split%s, %d without (e.g. %s)" % (len(rows), " (check only, nothing stored)" if only else " stored", len(miss), ", ".join(miss[:6])))


def cmd_floorarchive():
    """Saves every trade of the day (symbol, buyer, seller, quantity, rate, amount, time) as one compressed file in this repository, under data/floor/.
    The files are free to keep and are read straight from GitHub by the Sanket floorsheet view, so nothing counts against the database's daily writes.
    The last 120 days are kept. Run it after 15:00 Nepal time, when the broker numbers have been released."""
    import gzip
    day = now_np().strftime("%Y-%m-%d")
    sheet = nepse.getFloorSheet() or []
    rows = []
    for t in sheet:
        r = floor_row(t)
        if r[0]:
            rows.append(r)
    if len(rows) < 1000 or not any(r[1] > 0 for r in rows):
        print("floorsheet not complete yet (%d trades, broker numbers %s); try again later" % (len(rows), "hidden" if not any(r[1] > 0 for r in rows) else "present"))
        sys.exit(1)
    os.makedirs("data/floor", exist_ok=True)
    body = json.dumps({"d": day, "cols": ["symbol", "buyer", "seller", "qty", "rate", "amount", "time", "id"], "rows": rows}, separators=(",", ":"))
    with gzip.open("data/floor/%s.json.gz" % day, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(body)
    kept = floor_index()
    print("floorsheet archived: %d trades for %s, %d days kept" % (len(rows), day, len(kept)))


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
    """Runs by itself, every day, with nothing to press: live prices while the market is open (Sunday to Friday 11:00-15:00), then the daily close
    (retried every 5 minutes until NEPSE releases the broker numbers), then the price history until the day's free budget is used."""
    global AUTO
    AUTO = True
    closed_day = hist_day = None
    close_tries = 0
    print("Auto mode started. Leave this window open (or let the scheduled task run it).")
    while True:
        try:
            n = now_np()
            trading = n.weekday() in (6, 0, 1, 2, 3, 4)
            if market_open():
                cmd_live(until_close=True)
                continue
            if trading and n.time() > dt.time(15, 0) and closed_day != n.date():  # any time after 15:00 the same day, so a late start still saves the close
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
            if os.environ.get("NP_EXIT_AFTER_CLOSE") == "1" and not market_open() and (closed_day == n.date() or not trading or n.time() < dt.time(10, 0) and False):
                print("Nothing left to do today; finished."); return
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
    {"probe": cmd_probe, "live": cmd_live, "close": cmd_close, "history": cmd_history, "fresh": cmd_fresh, "floor": cmd_floor, "auto": cmd_auto, "longhistory": cmd_longhistory, "fundamentals": cmd_fundamentals, "floorarchive": cmd_floorarchive, "events": cmd_events, "floorback": cmd_floorback, "openhist": cmd_openhist, "mfnav": cmd_mfnav, "flowhist": cmd_flowhist, "pricehist": cmd_pricehist, "shares": cmd_shares}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
