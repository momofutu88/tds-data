#!/usr/bin/env python3
"""TDS 带单员数据采集器（在 GitHub Actions 上运行，直连币安公开接口）

用法:
  python3 scripts/collect.py --portfolio-ids 4902752005274121217 --out data
无需任何账号、API Key、登录态；接口为币安网页端公开接口，POST + JSON body。
"""
import argparse, csv, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

BAPI = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CST = timezone(timedelta(hours=8))


def post(path, body, retries=3):
    data = json.dumps(body).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(BAPI + path, data=data, method="POST")
            req.add_header("User-Agent", UA)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.load(r)
            if j.get("success") or j.get("code") == "000000":
                return j.get("data")
            raise RuntimeError(f"API error: {j.get('code')} {j.get('message')}")
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def get_detail(pid):
    url = f"{BAPI}/detail?portfolioId={pid}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r).get("data") or {}


def fetch_paged(path, pid, page_size=100, cap=100000):
    out, page, total = [], 1, None
    while True:
        d = post(path, {"portfolioId": str(pid), "pageNumber": page, "pageSize": page_size})
        if not d:
            break
        total = d.get("total", total)
        lst = d.get("list") or []
        out.extend(lst)
        print(f"  {path} page {page}: +{len(lst)} (累计 {len(out)}/{total})")
        if len(lst) < page_size or len(out) >= (total or 0) or len(out) >= cap:
            break
        page += 1
        time.sleep(0.4)
    return out, total


def ms2str(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio-ids", required=True)
    ap.add_argument("--out", default="data")
    ap.add_argument("--skip-trades", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    for pid in [x.strip() for x in a.portfolio_ids.split(",") if x.strip()]:
        print(f"=== portfolioId={pid}")
        detail = get_detail(pid)
        print("  nickname:", detail.get("nickname"), "| status:", detail.get("status"))
        with open(f"{a.out}/detail_{pid}.json", "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=1)

        pos, total = fetch_paged("/position-history", pid)
        rows = []
        for p in pos:
            opened, closed = p.get("opened"), p.get("closed")
            dur_h = round((closed - opened) / 3600000, 2) if opened and closed else ""
            entry, exit_ = p.get("avgCost"), p.get("avgClosePrice")
            ret = ""
            if entry and exit_:
                ret = round((entry - exit_) / entry * 100, 3) if p.get("side") == "Short" \
                    else round((exit_ - entry) / entry * 100, 3)
            rows.append({
                "positionId": p.get("positionId"), "symbol": p.get("symbol"),
                "side": p.get("side"), "leverage": p.get("leverage"),
                "pnl": p.get("closingPnl"), "roi": p.get("roi"), "ret_pct": ret,
                "duration_h": dur_h, "entry": entry, "exit": exit_,
                "max_qty": p.get("maxOpenInterest"), "closed_qty": p.get("closedVolume"),
                "opened": ms2str(opened), "closed_at": ms2str(closed),
                "opened_ms": opened, "closed_ms": closed,
                "status": p.get("status"), "type": p.get("type"),
            })
        rows.sort(key=lambda r: r["opened_ms"] or 0)
        f1 = f"{a.out}/positions_{pid}.csv"
        write_csv(f1, rows, list(rows[0].keys()) if rows else ["positionId"])
        tot = sum(float(r["pnl"] or 0) for r in rows)
        print(f"  仓位: {len(rows)}/{total} 条, 累计盈亏 {tot:+.2f} U, 区间 {rows[0]['opened']} ~ {rows[-1]['closed_at']}")

        if not a.skip_trades:
            trades, ttotal = fetch_paged("/trade-history", pid, page_size=200, cap=20000)
            tr = [{
                "time": ms2str(t.get("time")), "time_ms": t.get("time"),
                "symbol": t.get("symbol"), "side": t.get("side"),
                "price": t.get("price"), "qty": t.get("qty"),
                "quantity": t.get("quantity"), "realized_profit": t.get("realizedProfit"),
                "fee": t.get("fee"), "position_side": t.get("positionSide"),
            } for t in trades]
            tr.sort(key=lambda r: r["time_ms"] or 0)
            write_csv(f"{a.out}/trades_{pid}.csv", tr, list(tr[0].keys()) if tr else ["time"])
            print(f"  成交: {len(tr)}/{ttotal} 条, 区间 {tr[0]['time']} ~ {tr[-1]['time']}" if tr else "  成交: 0")

    print("DONE")


if __name__ == "__main__":
    main()
