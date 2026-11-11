#!/usr/bin/env python3
"""扫描币安带单员榜单：抓 home-page/query-list 全量榜单 + detail，
筛出运行时间 >= 180 天的稳定带单员候选。"""
import csv, json, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
QA = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
DET = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail"
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


def get_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception:
            time.sleep(1 + i)
    return None


def scan_page(time_range, data_type, page):
    body = {"pageNumber": page, "pageSize": 50, "timeRange": time_range,
            "dataType": data_type, "favoriteOnly": False, "hideFull": False,
            "nickname": "", "order": "DESC", "apiKeyOnly": False}
    j = post_json(QA, body)
    if not j or not j.get("success"):
        return None
    d = j.get("data") or {}
    return d.get("list") or []


def scan_all(time_range="180D", data_type="ROI"):
    seen = {}
    page = 1
    while page <= 12:
        lst = scan_page(time_range, data_type, page)
        if lst is None:
            print(f"  [{time_range}/{data_type}] page {page} FAILED"); break
        if not lst:
            break
        for x in lst:
            pid = x.get("leadPortfolioId")
            if pid:
                seen.setdefault(pid, x)
        print(f"  [{time_range}/{data_type}] page {page}: +{len(lst)} (cum {len(seen)})")
        if len(lst) < 50:
            break
        page += 1
        time.sleep(0.3)
    return seen


def main():
    import os
    os.makedirs("data", exist_ok=True)
    leads = {}
    for tr, dt in [("180D", "ROI"), ("180D", "PNL"), ("90D", "ROI"), ("MAX", "ROI"), ("MAX", "PNL")]:
        print(f"scanning {tr}/{dt} ...")
        leads.update(scan_all(tr, dt))
        time.sleep(0.5)
    print(f"\ntotal unique leads: {len(leads)}")

    # 保存榜单原始信息
    with open("data/leads_raw.json", "w", encoding="utf-8") as f:
        json.dump(list(leads.values()), f, ensure_ascii=False)

    # 逐个查 detail，筛 180 天
    now_ms = time.time() * 1000
    rows, all_rows = [], []
    for i, (pid, x) in enumerate(leads.items(), 1):
        d = get_json(f"{DET}?portfolioId={pid}")
        if not d or not d.get("success"):
            time.sleep(0.15); continue
        det = d.get("data") or {}
        start = det.get("startTime") or det.get("launchTime") or 0
        try: start = int(start)
        except: start = 0
        days = (now_ms - start) / 86400000 if start else None
        row = {
            "leadPortfolioId": pid,
            "nickname": det.get("nickname") or x.get("nickname") or "",
            "start_time": datetime.fromtimestamp(start / 1000, CST).strftime("%Y-%m-%d") if start else "",
            "running_days": round(days) if days else "",
            "roi_180d": x.get("roi") or x.get("roiRate") or "",
            "pnl_180d": x.get("pnl") or "",
            "marginBalance": det.get("marginBalance") or "",
            "aumAmount": det.get("aumAmount") or "",
            "sharpRatio": det.get("sharpRatio") or "",
            "maxDrawdown": det.get("maxDrawdown") or det.get("mdd") or "",
            "currentCopyCount": det.get("currentCopyCount") or x.get("currentCopyCount") or "",
            "maxCopyCount": det.get("maxCopyCount") or x.get("maxCopyCount") or "",
            "riskLevel": det.get("riskLevel") or "",
            "style": det.get("style") or det.get("portfolioType") or "",
        }
        all_rows.append(row)
        if days and days >= 180:
            rows.append(row)
        if i % 50 == 0:
            print(f"  detail {i}/{len(leads)} (180d+: {len(rows)})")
        time.sleep(0.15)

    def dump(path, rs):
        if not rs:
            open(path, "w").write("none"); return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rs[0].keys()))
            w.writeheader(); w.writerows(rs)

    dump("data/leads_all.csv", all_rows)
    dump("data/leads_180d.csv", rows)
    print(f"\ndone: all={len(all_rows)} 180d+={len(rows)}")


if __name__ == "__main__":
    main()
