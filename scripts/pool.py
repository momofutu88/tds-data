#!/usr/bin/env python3
"""全市场候选池重建：回答"TDS 每小时从全市场挑 1 个，它挑的是哪个"

数据源：data.binance.vision（AWS 托管，免 Key，美国节点可访问）
  - 标的清单：S3 list-objects
  - 1h K 线：monthly klines 包（完整月份）

产出：
  data/candidate_pool.csv   每条 TDS 开仓 → 该标的在当时的全市场排名
  data/market_snapshot.csv  每个开仓时刻的全市场横截面统计
  data/pool_meta.json       覆盖率、缺失标的等元信息
"""
import csv
import gzip
import io
import json
import os
import sys
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

S3 = "https://data.binance.vision.s3.amazonaws.com"
BASE = "https://data.binance.vision"
KL = "data/futures/um/monthly/klines"
POS_CSV = "data/all_positions.csv"
TDS = "4902752005274121217"
MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10"]
WORKERS = 16
UA = {"User-Agent": "Mozilla/5.0 research"}


def http(url, timeout=60, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
    raise last


def list_symbols():
    """S3 list-objects 拿全部永续标的名录"""
    url = f"{S3}?delimiter=/&prefix={KL}/"
    raw = http(url, 90).decode("utf-8", "replace")
    out = []
    marker = "<Prefix>"
    for chunk in raw.split(marker)[1:]:
        p = chunk.split("</Prefix>")[0]
        tail = p.rstrip("/").split("/")[-1]
        if tail:
            out.append(tail)
    if not out:
        raise RuntimeError("listing empty")
    return sorted(set(out))


def parse_zip(blob):
    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            txt = io.TextIOWrapper(f, encoding="utf-8")
            for line in txt:
                parts = line.rstrip("\n").split(",")
                if len(parts) < 7:
                    continue
                try:
                    ot = int(parts[0])
                    cl = float(parts[4])
                except ValueError:
                    continue                      # 表头行
                rows.append((ot, cl))
    return rows


def fetch_one(sym, month):
    url = f"{BASE}/{KL}/{sym}/1h/{sym}-1h-{month}.zip"
    try:
        return sym, month, parse_zip(http(url, 60))
    except Exception:
        return sym, month, None


def load_positions():
    if not os.path.exists(POS_CSV):
        raise RuntimeError("missing " + POS_CSV)
    rows = []
    with open(POS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if "TDS" not in (r.get("portfolio") or ""):
                continue
            if r.get("side") != "Short":
                continue
            try:
                t = int(float(r["opened_ms"]))
                pnl = float(r["pnl"] or 0)
            except Exception:
                continue
            rows.append({"symbol": r["symbol"], "opened_ms": t, "pnl": pnl,
                         "opened": r.get("opened", "")})
    return rows


def main():
    print("== 1. 取标的名录")
    try:
        syms = list_symbols()
        print(f"   listing ok: {len(syms)} symbols, sample={syms[:5]}")
    except Exception as e:
        print(f"   listing FAILED ({type(e).__name__}), fallback to positions+h3", file=sys.stderr)
        syms = None

    pos = load_positions()
    print(f"== 2. TDS 空单 {len(pos)} 条")

    if syms is None:
        cand = {p["symbol"] for p in pos}
        if os.path.exists("data/h3_features.csv"):
            with open("data/h3_features.csv", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    cand.add(r["symbol"])
        syms = sorted(cand)
        print(f"   fallback symbols: {len(syms)}")

    syms = [s for s in syms if s.endswith("USDT") and "_" not in s]
    print(f"== 3. USDT 永续 {len(syms)} 个，抓 {MONTHS}")

    kl = defaultdict(list)
    failed = []
    tasks = [(s, m) for s in syms for m in MONTHS]
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, s, m): (s, m) for s, m in tasks}
        for fu in as_completed(futs):
            s, m, rows = fu.result()
            done += 1
            if rows is None:
                failed.append(f"{s}/{m}")
            else:
                kl[s].extend(rows)
            if done % 200 == 0:
                print(f"   {done}/{len(tasks)} ok={len(kl)} failed={len(failed)}")

    for s in kl:
        kl[s].sort()
    print(f"== 4. K线 ok={len(kl)} failed={len(failed)}")
    print(f"   failed sample: {failed[:10]}")

    # 每小时的横截面 1h 涨幅
    hourly = defaultdict(dict)
    for s, bars in kl.items():
        for i in range(1, len(bars)):
            prev, cur = bars[i - 1][1], bars[i][1]
            if prev:
                hourly[bars[i][0]][s] = (cur - prev) / prev * 100

    idx = {s: {b[0]: j for j, b in enumerate(bars)} for s, bars in kl.items()}

    def ret(s, open_ms, hours):
        bars = kl.get(s)
        if not bars:
            return None
        h0 = open_ms - open_ms % 3600000
        m = idx[s]
        if h0 not in m:
            return None
        j = m[h0] - hours
        if j < 0:
            return None
        base = bars[j][1]
        return (bars[m[h0]][1] - base) / base * 100 if base else None

    out = []
    snaps = []
    for p in sorted(pos, key=lambda x: x["opened_ms"]):
        h0 = p["opened_ms"] - p["opened_ms"] % 3600000
        cross = hourly.get(h0) or {}
        if len(cross) < 20:
            continue
        s = p["symbol"]
        if s not in cross:
            continue
        r1 = cross[s]
        vals = sorted(cross.values())
        n = len(vals)
        rank_from_top = n - sum(1 for v in vals if v <= r1) + 1      # 1 = 最强
        pct = (n - rank_from_top + 1) / n * 100

        def q(x):
            return round(vals[min(n - 1, max(0, int(n * x)))], 3)

        r4 = ret(s, p["opened_ms"], 4)
        r24 = ret(s, p["opened_ms"], 24)
        out.append({
            "opened": p["opened"], "symbol": s, "pnl": round(p["pnl"], 2),
            "ret_1h": round(r1, 4),
            "n_candidates": n,
            "rank_from_top": rank_from_top,
            "rank_pct": round(pct, 1),
            "market_p10": q(0.10), "market_p50": q(0.50), "market_p90": q(0.90),
            "ret_4h": (round(r4, 4) if r4 is not None else ""),
            "ret_24h": (round(r24, 4) if r24 is not None else ""),
        })
        snaps.append({"h0": h0, "n": n, "p10": q(0.10), "p50": q(0.50), "p90": q(0.90),
                      "max": round(vals[-1], 3), "min": round(vals[0], 3)})

    os.makedirs("data", exist_ok=True)
    with open("data/candidate_pool.csv", "w", newline="", encoding="utf-8-sig") as f:
        if out:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            w.writeheader()
            w.writerows(out)
    with open("data/market_snapshot.csv", "w", newline="", encoding="utf-8-sig") as f:
        if snaps:
            w = csv.DictWriter(f, fieldnames=list(snaps[0].keys()))
            w.writeheader()
            w.writerows(snaps)

    # 结论统计
    print(f"\n== 5. 候选池重建：{len(out)} 条可算 / {len(pos)} 条")
    if out:
        rk = sorted(x["rank_from_top"] for x in out)
        import statistics
        print(f"   平均候选数 {statistics.mean(x['n_candidates'] for x in out):.0f}")
        print(f"   选中标的排名（1=最强）：中位 {statistics.median(rk):.0f} | P25 {rk[len(rk)//4]} | P75 {rk[len(rk)*3//4]}")
        top1 = sum(1 for x in rk if x == 1)
        top5 = sum(1 for x in rk if x <= 5)
        top10pct = sum(1 for x in out if x["rank_pct"] >= 90)
        print(f"   排第 1: {top1} ({top1/len(out)*100:.1f}%) | 前 5: {top5} ({top5/len(out)*100:.1f}%) | 前 10%: {top10pct} ({top10pct/len(out)*100:.1f}%)")
        print(f"   随机基线：第1应 {100/statistics.mean(x['n_candidates'] for x in out):.2f}%，前10%应 10%")

    meta = {"symbols_listed": len(syms), "symbols_with_kline": len(kl),
            "failed": len(failed), "months": MONTHS,
            "positions_total": len(pos), "pool_rows": len(out),
            "failed_sample": failed[:30]}
    with open("data/pool_meta.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print("== 6. done", json.dumps({k: v for k, v in meta.items() if k != "failed_sample"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
