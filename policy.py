"""Rates and rules: reads the public pages that move the market and keeps a dated copy in data/policy/ (free, permanent, read by the site and by Ask Sanket).
Each source becomes one file: its tables, its headlines (text, link, date when one sits next to it) and a short text snapshot.
Run:  python policy.py            all sources
      python policy.py nrb_home   one source, printed
Sources that fail are skipped and reported; nothing here can stop the feed.
"""
import datetime as dt, gzip, html as H, json, os, re, sys, time
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data", "policy")
UA = {"User-Agent": "Mozilla/5.0 (compatible; SanketFeed/1.0)"}

SOURCES = {
    # name: (title, url, group)
    "nrb_home": ("NRB: policy rates, interbank and short-term rates", "https://www.nrb.org.np/", "rates"),
    "nrb_policy_rates": ("NRB policy rates", "https://www.nrb.org.np/category/policy-rates/", "rates"),
    "nrb_short_term": ("NRB short term interest rates", "https://www.nrb.org.np/category/short-term-interest-rates/", "rates"),
    "ss_short_term": ("Short-term interest rates (ShareSansar)", "https://www.sharesansar.com/short-term-interest-rates", "rates"),
    "ss_bfi_rates": ("Bank and finance interest rates (ShareSansar)", "https://www.sharesansar.com/bfis-interest-rate", "rates"),
    "nrb_notices": ("NRB notices", "https://www.nrb.org.np/category/notices/", "rules"),
    "nrb_press": ("NRB press releases", "https://www.nrb.org.np/category/press-release/", "rules"),
    "nrb_monetary": ("NRB monetary policy", "https://www.nrb.org.np/category/monetary-policy/", "rules"),
    "nrb_circulars": ("NRB circulars and directives to banks", "https://www.nrb.org.np/category/bfi-policy/", "rules"),
    "sebon_home": ("SEBON (securities regulator)", "https://www.sebon.gov.np/", "rules"),
    "sebon_notices": ("SEBON notices and circulars", "https://www.sebon.gov.np/notices", "rules"),
    "sebon_acts": ("Securities laws, rules and directives", "https://www.sebon.gov.np/acts-rules-guidelines", "rules"),
    "ss_news": ("Market news (ShareSansar)", "https://www.sharesansar.com/category/latest", "news"),
    "ss_interest_news": ("Bank interest rate notices (ShareSansar)", "https://www.sharesansar.com/category/interest-rates", "news"),
    "ss_economy": ("Economy news (ShareSansar)", "https://www.sharesansar.com/category/economy", "news"),
}
DATE = re.compile(r"(\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8},? \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]{2,8},? \d{4}|[A-Z][a-z]{2} \d{1,2}, \d{4})")


def clean(s):
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"(?s)<[^>]+>", " ", s))).strip()


def parse(page, base):
    page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page)
    tables = []
    for tb in re.findall(r"(?is)<table\b.*?</table>", page):
        rows = []
        for tr in re.findall(r"(?is)<tr\b.*?</tr>", tb):
            cells = [clean(c) for c in re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", tr)]
            if any(cells):
                rows.append(cells[:14])
        if len(rows) >= 2:
            tables.append(rows[:80])
    links, seen = [], set()
    for m in re.finditer(r"(?is)<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", page):
        t = clean(m.group(2))
        if len(t) < 18 or len(t) > 220 or t.lower() in seen or re.match(r"(?i)(home|login|about|contact|read more|privacy|terms)", t):
            continue
        seen.add(t.lower())
        u = m.group(1)
        u = u if u.startswith("http") else (base.rstrip("/") + "/" + u.lstrip("/")) if not u.startswith("/") else re.match(r"https?://[^/]+", base).group(0) + u
        tail = clean(page[m.end(): m.end() + 260])
        d = DATE.search(tail) or DATE.search(clean(page[max(0, m.start() - 160): m.start()]))
        links.append({"t": t, "u": u, "d": d.group(1) if d else ""})
    text = clean(page)
    return tables, links[:120], text[:3500]


def run(name):
    title, url, group = SOURCES[name]
    try:
        r = requests.get(url, headers=UA, timeout=40)
    except Exception as e:
        return name, "network %r" % e
    if r.status_code != 200:
        return name, "HTTP %s" % r.status_code
    tables, links, text = parse(r.text, url)
    body = {"name": name, "title": title, "url": url, "group": group, "fetched": dt.datetime.utcnow().isoformat() + "Z", "tables": tables, "links": links, "text": text}
    if not (tables or links):
        return name, "page read (%d chars) but no tables or headlines found" % len(r.text)
    os.makedirs(OUT, exist_ok=True)
    with gzip.open(os.path.join(OUT, name + ".json.gz"), "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(body, f, separators=(",", ":"), ensure_ascii=False)
    return name, "%d tables, %d headlines" % (len(tables), len(links))


def main():
    names = [a for a in sys.argv[1:] if a in SOURCES] or list(SOURCES)
    ok = []
    for n in names:
        res = run(n)
        print("%-18s %s" % res)
        if "tables" in res[1] or "headlines" in res[1]:
            ok.append(n)
        time.sleep(1.2)
    json.dump({"updated": dt.datetime.utcnow().isoformat() + "Z", "sources": [dict(name=n, title=SOURCES[n][0], url=SOURCES[n][1], group=SOURCES[n][2]) for n in ok]}, open(os.path.join(OUT, "index.json"), "w"), separators=(",", ":"))
    print("policy: %d of %d sources saved" % (len(ok), len(names)))


if __name__ == "__main__":
    main()
