#!/usr/bin/env python3
"""探测币安带单员列表公开接口，找到可用的榜单端点。"""
import json, urllib.request, urllib.error, time

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
BASE = "https://www.binance.com/bapi"
P = BASE + "/futures/v1/friendly/future/copy-trade/lead-portfolio"
PC = BASE + "/composite/v1/friendly/future/copy-trade/lead-portfolio"

POST_BODIES = {
    "page20": {"pageNumber": 1, "pageSize": 20},
    "page20_roi180": {"pageNumber": 1, "pageSize": 20, "orderBy": "roi", "orderDirection": "desc", "timeRange": "180D"},
    "sort_roi": {"pageNumber": 1, "pageSize": 20, "sortType": "ROI", "timeRange": "MAX"},
    "roi_desc": {"pageNumber": 1, "pageSize": 20, "roiSortType": "ROI", "timeRange": "180D"},
    "empty": {},
}
GETS = [
    P + "/rank",
    P + "/list?pageNumber=1&pageSize=20",
    P + "/query-list?pageNumber=1&pageSize=20",
    P + "/leaderboard?pageNumber=1&pageSize=20",
    P + "/list-portfolio?pageNumber=1&pageSize=20",
    PC + "/query-list",
    PC + "/list?pageNumber=1&pageSize=20",
    "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/portfolio/rank-list?pageNumber=1&pageSize=20",
]

def try_req(method, url, body=None):
    try:
        if method == "POST":
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
        else:
            req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "ignore")
        try:
            j = json.loads(txt)
            code = j.get("code"); ok = j.get("success")
            d = j.get("data")
            if isinstance(d, dict):
                keys = list(d.keys())[:12]
                inner = None
                for k in ("list", "result", "records", "data", "portfolioList"):
                    if isinstance(d.get(k), list):
                        inner = d[k]; break
                n = len(inner) if inner is not None else "?"
                first = ""
                if inner:
                    f0 = inner[0]
                    first = json.dumps({k: f0.get(k) for k in list(f0.keys())[:20]}, ensure_ascii=False)[:400]
                elif isinstance(d.get("portfolioId"), str):
                    first = json.dumps({k: d.get(k) for k in list(d.keys())[:20]}, ensure_ascii=False)[:400]
                return f"OK code={code} success={ok} keys={keys} n={n} first={first}"
            elif isinstance(d, list):
                if d:
                    f0 = d[0]
                    return f"OK code={code} success={ok} n={len(d)} first={json.dumps({k: f0.get(k) for k in list(f0.keys())[:20]}, ensure_ascii=False)[:400]}"
                return f"OK code={code} success={ok} n=0"
            return f"OK code={code} success={ok} data_type={type(d).__name__} head={txt[:200]}"
        except json.JSONDecodeError:
            return f"HTML len={len(txt)} head={txt[:120]}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}"

out = []
for path in ["/query-list", "/list", "/search", "/rank-list", "/get-lead-portfolio-list"]:
    for name, body in POST_BODIES.items():
        r = try_req("POST", P + path, body)
        line = f"POST {path} [{name}] -> {r}"
        print(line); out.append(line); time.sleep(0.4)
for u in GETS:
    r = try_req("GET", u)
    line = f"GET {u.replace(BASE,'')} -> {r}"
    print(line); out.append(line); time.sleep(0.4)

with open("data/discovery.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nsaved data/discovery.txt")
