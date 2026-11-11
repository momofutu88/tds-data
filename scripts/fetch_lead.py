#!/usr/bin/env python3
"""通用带单员采集器：可指定 portfolioId、分页上限、限速，逐页落盘防限流中断。

用法:
  python3 scripts/fetch_lead.py --ids 5140488422016171264,5152209743214391553 --cap 6000 --delay 1.6 --out data
"""
import argparse, csv, json, os, time, urllib.request
from datetime import datetime, timezone, timedelta

BAPI = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CST = timezone(timedelta(hours=8))


def post(path, body, delay=0.0, max_retry=6):
    data = json.dumps(body).encode()
    for i in range(max_retry):
        try:
            req = urllib.request.Request(BAPI + path, data=data, method="POST")
            req.add_header("User-Agent", UA)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
            if j.get("success") or j.get("code") == "000000":
                time.sleep(delay)
                return j.get("data")
            msg = f"{j.get('code')} {j.get('message')}"
            if "90801003" in msg or "Too many" in msg:
                wait = 20 * (i + 1)
                print(f"    rate limited, sleep {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError("API error: " + msg)
        except Exception as e:
            if i == max_retry - 1:
                raise
            print(f"    retry {i+1}: {e}")
            time.sleep(5 * (i + 1))


def get_detail(pid):
    url = f"{BAPI}/detail?portfolioId={pid}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r).get("data") or {}


def ms2str(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, CST).strftime("%Y-%m-%d %H:%M")


FIELDS = ["positionId", "symbol", "side", "leverage", "pnl", "roi", "ret_pct", "duration_h",
          "entry", "exit", "max_qty", "closed_qty", "opened", "closed_at", "opened_ms",
          "closed_ms", "status", "type"]


def to_row(p):
    opened, closed = p.get("opened"), p.get("closed")
    dur = round((closed - opened) / 3600000, 2) if opened and closed else ""
    entry, exit_ = p.get("avgCost"), p.get("avgClosePrice")
    ret = ""
    if entry and exit_:
        ret = round((entry - exit_) / entry * 100, 3) if p.get("side") == "Short" \
            else round((exit_ - entry) / entry * 100, 3)
    return {"positionId": p.get("positionId"), "symbol": p.get("symbol"), "side": p.get("side"),
            "leverage": p.get("leverage"), "pnl": p.get("closingPnl"), "roi": p.get("roi"),
            "ret_pct": ret, "duration_h": dur, "entry": entry, "exit": exit_,
            "max_qty": p.get("maxOpenInterest"), "closed_qty": p.get("closedVolume"),
            "opened": ms2str(opened), "closed_at": ms2str(closed), "opened_ms": opened,
            "closed_ms": closed, "status": p.get("status"), "type": p.get("type")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    ap.add_argument("--cap", type=int, default=6000)
    ap.add_argument("--delay", type=float, default=1.6)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    for pid in [x.strip() for x in a.ids.split(",") if x.strip()]:
        print(f"== {pid}")
        try:
            det = get_detail(pid)
            json.dump(det, open(f"{a.out}/detail_{pid}.json", "w"), ensure_ascii=False, indent=1)
            print(f"  {det.get('nickname')} | margin={det.get('marginBalance')} aum={det.get('aumAmount')}")
        except Exception as e:
            print("  detail fail:", e)

        rows, page, total = [], 1, None
        while True:
            d = post("/position-history", {"portfolioId": pid, "pageNumber": page, "pageSize": 100},
                     delay=a.delay)
            if not d:
                break
            total = d.get("total", total)
            lst = d.get("list") or []
            rows.extend(to_row(p) for p in lst)
            if page % 5 == 0 or len(lst) < 100:
                print(f"  page {page}: +{len(lst)} 累计 {len(rows)}/{total}")
            if len(lst) < 100 or len(rows) >= min(total or 0, a.cap):
                break
            page += 1

        if not rows:
            print("  empty"); continue
        rows.sort(key=lambda r: r["opened_ms"] or 0)
        with open(f"{a.out}/positions_{pid}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        pnl = sum(float(r["pnl"] or 0) for r in rows)
        print(f"  写入 {len(rows)} 条, 区间 {rows[0]['opened']} ~ {rows[-1]['closed_at'] or rows[-1]['opened']}, 盈亏 {pnl:+.0f}U")

        try:
            trades, tp, ttotal = [], 1, None
            while tp <= 40:
                d = post("/trade-history", {"portfolioId": pid, "pageNumber": tp, "pageSize": 200},
                         delay=a.delay)
                if not d:
                    break
                ttotal = d.get("total", ttotal)
                lst = d.get("list") or []
                trades.extend({"time": ms2str(t.get("time")), "time_ms": t.get("time"),
                               "symbol": t.get("symbol"), "side": t.get("side"),
                               "price": t.get("price"), "qty": t.get("qty"),
                               "realized_profit": t.get("realizedProfit"), "fee": t.get("fee")}
                              for t in lst)
                if len(lst) < 200:
                    break
                tp += 1
            if trades:
                trades.sort(key=lambda r: r["time_ms"] or 0)
                with open(f"{a.out}/trades_{pid}.csv", "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                    w.writeheader(); w.writerows(trades)
                print(f"  成交 {len(trades)}/{ttotal}")
        except Exception as e:
            print("  trades skipped:", e)

    print("DONE")


if __name__ == "__main__":
    main()
