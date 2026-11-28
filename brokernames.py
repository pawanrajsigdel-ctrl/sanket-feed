"""Writes data/brokers.json: {broker number: broker company name}, so Sanket can show names instead of numbers. Tries what the NEPSE library offers; prints what it found."""
import json, os, sys
from nepse import Nepse

n = Nepse()
n.setTLSVerification(False)
names = {}
print("methods with 'rok' or 'ember':", [m for m in dir(n) if "rok" in m or "ember" in m])
for m in [x for x in dir(n) if ("rokerList" in x or "getBrokers" in x or "getMember" in x or "rokerDetail" in x)]:
    try:
        r = getattr(n, m)()
        print(m, type(r).__name__, (r[:1] if isinstance(r, list) else str(r)[:200]))
        if isinstance(r, list):
            for x in r:
                if not isinstance(x, dict):
                    continue
                no = x.get("memberCode") or x.get("brokerCode") or x.get("memberId") or x.get("id")
                nm = x.get("memberName") or x.get("brokerName") or x.get("name") or x.get("fullName")
                try:
                    no = int(str(no).strip())
                except Exception:
                    continue
                if nm and 0 < no < 1000:
                    names[no] = str(nm).strip()
    except Exception as e:
        print(m, "failed:", repr(e)[:120])
if not names:
    try:  # floorsheet trades name their brokers too
        t = (n.getFloorSheet() or [])[:3]
        print("floorsheet sample keys:", list(t[0].keys()) if t else "none")
        for x in n.getFloorSheet() or []:
            for k in ("buyer", "seller"):
                no, nm = x.get(k + "MemberId") or x.get(k + "BrokerId"), x.get(k + "MemberName") or x.get(k + "BrokerName")
                try:
                    if nm and no:
                        names[int(no)] = str(nm).strip()
                except Exception:
                    pass
    except Exception as e:
        print("floorsheet failed:", repr(e)[:120])
print("brokers named:", len(names))
if names:
    os.makedirs("data", exist_ok=True)
    json.dump({str(k): v for k, v in sorted(names.items())}, open("data/brokers.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
