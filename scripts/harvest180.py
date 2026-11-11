#!/usr/bin/env python3
"""对 180 天+ 候选带单员抓全量仓位历史并计算稳定指标。"""
import csv, json, os, time, urllib.request
from datetime import datetime, timezone, timedelta
from collections import defaultdict

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
PH = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history"
CST = timezone(timedelta(hours=8))


def post_json(url, body, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
            req.add_header("User-Agent", UA)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception:
            time.sleep(1 + i)
    return None


def fetch_positions(pid, max_pages=60):
    rows, seen = [], set()
    for page in range(1, max_pages + 1):
        j = post_json(PH, {"portfolioId": pid, "pageNumber": page, "pageSize": 20})
        if not j or not j.get("success"):
            break
        lst = (j.get("data") or {}).get("list") or []
        if not lst:
            break
        new = 0
        for x in lst:
            key = x.get("id") or f'{x.get("symbol")}_{x.get("opened")}'
            if key in seen:
                continue
            seen.add(key)
            rows.append(x); new += 1
        if new == 0 or len(lst) < 20:
            break
        time.sleep(0.18)
    return rows


def norm(rows):
    out = []
    for r in rows:
        try:
            out.append({
                "positionId": r.get("positionId", ""),
                "symbol": r.get("symbol", ""),
                "side": r.get("side") or ("Short" if str(r.get("positionType", "")).lower() in ("short", "2") else "Long"),
                "pnl": r.get("closingPnl", ""),
                "ret_pct": r.get("roi") if r.get("roi") is not None else "",
                "leverage": r.get("leverage", ""),
                "max_qty": r.get("maxOpenInterest", ""),
                "entry": r.get("avgCost") or "",
                "opened_ms": r.get("opened") or "",
                "closed_ms": r.get("closed") or "",
                "status": r.get("status", ""),
            })
        except Exception:
            continue
    return out


def f(x, d=None):
    try: return float(x)
    except: return d


def analyze(pid, nick, rows):
    rs = [r for r in rows if r.get("closed_ms") and f(r["pnl"]) is not None]
    rs.sort(key=lambda r: int(float(r["closed_ms"])))
    if not rs:
        return None
    pn = [f(r["pnl"], 0) for r in rs]
    total = sum(pn); n = len(rs)
    win = sum(1 for p in pn if p > 0)
    gains = sum(p for p in pn if p > 0); losses = -sum(p for p in pn if p < 0)
    pf = gains / losses if losses else 999
    cum = 0; peak = 0; mdd = 0
    for p in pn:
        cum += p; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    months = defaultdict(lambda: [0, 0.0])
    for r in rs:
        m = datetime.fromtimestamp(int(float(r["closed_ms"])) / 1000, CST).strftime("%Y-%m")
        months[m][0] += 1; months[m][1] += f(r["pnl"], 0)
    months = {k: v for k, v in sorted(months.items()) if k >= "2026-05"}
    neg_months = sum(1 for v in months.values() if v[1] < 0)
    span_days = (int(float(rs[-1]["closed_ms"])) - int(float(rs[0]["closed_ms"]))) / 86400000
    return {
        "pid": pid, "nickname": nick, "n": n,
        "total_pnl": round(total, 1), "win_rate": round(win / n * 100, 1),
        "profit_factor": round(pf, 2), "avg_pnl": round(total / n, 1),
        "max_loss": round(min(pn), 1),
        "realized_mdd": round(mdd, 1),
        "mdd_vs_total": round(mdd / abs(total) * 100, 1) if total else None,
        "neg_months_5m": neg_months, "months": {k: [v[0], round(v[1], 1)] for k, v in months.items()},
        "span_days": round(span_days), "first": rs[0]["closed_ms"], "last": rs[-1]["closed_ms"],
    }


def main():
    import glob
    os.makedirs("data", exist_ok=True)
    cands = []
    for fp in glob.glob("data/leads_180d.csv"):
        for r in csv.DictReader(open(fp, encoding="utf-8-sig")):
            cands.append((r["leadPortfolioId"], r["nickname"]))
    print(f"candidates: {len(cands)}")
    results = []
    for pid, nick in cands:
        rows = fetch_positions(pid)
        rs = norm(rows)
        safe_nick = nick.replace("/", "_")[:20]
        with open(f"data/h180_positions_{pid}.csv", "w", newline="", encoding="utf-8-sig") as fo:
            if rs:
                w = csv.DictWriter(fo, fieldnames=[k for k in rs[0].keys() if k != "raw"])
                w.writeheader()
                for r in rs:
                    w.writerow({k: v for k, v in r.items() if k != "raw"})
            else:
                fo.write("empty")
        a = analyze(pid, nick, rs)
        if a:
            results.append(a)
            print(f"{nick[:18]:20s} n={a['n']:5d} pnl={a['total_pnl']:+10.0f} win={a['win_rate']:5.1f}% "
                  f"PF={a['profit_factor']:5.2f} mdd={a['realized_mdd']:8.0f} neg_m={a['neg_months_5m']}")
        else:
            print(f"{nick[:18]:20s} NO DATA ({len(rows)} raw rows)")
        time.sleep(0.3)
    with open("data/h180_analysis.json", "w", encoding="utf-8") as fo:
        json.dump(results, fo, ensure_ascii=False, indent=1)
    print("saved h180_analysis.json")


if __name__ == "__main__":
    main()
