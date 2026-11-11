#!/usr/bin/env python3
"""探测带单员「当前持仓」浮盈亏：position-history 中未平仓的记录。"""
import json, urllib.request, sys

BAPI = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"


def post(path, body):
    r = urllib.request.Request(BAPI + path, data=json.dumps(body).encode(), method="POST")
    r.add_header("User-Agent", UA)
    r.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(r, timeout=25))


def get(url):
    r = urllib.request.Request(url)
    r.add_header("User-Agent", UA)
    return json.load(urllib.request.urlopen(r, timeout=25))


pids = sys.argv[1].split(",")
for pid in pids:
    try:
        det = get(f"{BAPI}/detail?portfolioId={pid}")["data"]
        print(f"== {det.get('nickname')} ({pid}) margin={det.get('marginBalance')}")
        for k, v in det.items():
            if any(x in k.lower() for x in ("realiz", "unreal", "pnl", "balance", "aum")):
                print(f"   {k} = {v}")
    except Exception as e:
        print("detail fail", e)
    try:
        d = post("/position-history", {"portfolioId": pid, "pageNumber": 1, "pageSize": 50})
        shown = 0
        for p in (d.get("list") or []):
            if p.get("status") != "All Closed" or not p.get("closed"):
                if shown == 0:
                    print("   [OPEN raw]", json.dumps(p, ensure_ascii=False)[:1200])
                shown += 1
                print(f"   [OPEN] {p.get('symbol')} {p.get('side')} lev={p.get('leverage')} "
                      f"roi={p.get('roi')} pnl={p.get('closingPnl')} status={p.get('status')} "
                      f"opened={p.get('opened')}")
        print(f"   open_count_page1={shown}")
    except Exception as e:
        print("pos fail", e)
    print()
