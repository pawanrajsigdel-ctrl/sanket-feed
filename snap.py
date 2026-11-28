"""Saves the answers of the public Sanket pages as small files in this repository (data/snap/<name>.json), so the site reads them from GitHub and the database is not asked.
Runs after the close and by hand. A page is only replaced when the API answers successfully, so a blocked database never wipes a good saved copy."""
import json, os, re, sys, time, datetime as dt
import requests

API = os.environ.get("NP_API", "https://techtara-api.techtara.workers.dev").rstrip("/")
PATHS = ["/api/np/market", "/api/np/pressure", "/api/np/picks", "/api/np/fundamentals", "/api/np/today", "/api/np/rules", "/api/np/rotation", "/api/np/mfnav",
         "/api/np/leaders", "/api/np/ipo", "/api/np/heatmap", "/api/np/compounders", "/api/np/brokers?days=5", "/api/np/brokers?days=30", "/api/np/breadth", "/api/np/signals"]


def slug(p):
    return re.sub(r"[^a-zA-Z0-9]+", "_", re.sub(r"^/api/np/", "", p)).strip("_")


def main():
    os.makedirs("data/snap", exist_ok=True)
    ok = bad = 0
    for p in PATHS:
        body = None
        for attempt in range(2):
            try:
                r = requests.get(API + p, timeout=40)
                if r.status_code == 200:
                    j = r.json()
                    if j.get("success") and not j.get("empty"):
                        body = j
                        break
                print("  ! %s -> HTTP %s %s" % (p, r.status_code, r.text[:80].replace("\n", " ")))
            except Exception as e:
                print("  ! %s -> %r" % (p, e))
            time.sleep(3)
        if body is None:
            bad += 1
            continue
        body["_snap"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open("data/snap/%s.json" % slug(p), "w", encoding="utf-8") as f:
            json.dump(body, f, separators=(",", ":"), ensure_ascii=False)
        ok += 1
        time.sleep(1)
    print("saved %d pages, %d not available" % (ok, bad))
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
