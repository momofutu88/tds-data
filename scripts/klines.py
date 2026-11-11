#!/usr/bin/env python3
"""H3 特征：从 data.binance.vision 官方 futures/um 月度 K 线包抓 1h K 线，
计算每条持仓开仓时刻的涨幅分位特征。

背景：GitHub 美国 runner 访问 fapi.binance.com 返回 451（地域封锁），
但 data.binance.vision（AWS 托管）全通，故改用官方数据仓库。
"""
import csv, glob, gzip, io, json, os, time, urllib.request, zipfile
from datetime import datetime, timedelta, timezone
from collections import defaultdict

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
BASE_D = "https://data.binance.vision/data/futures/um/daily/klines"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
CST = timezone(timedelta(hours=8))
START_MONTH = "2026-03"
NOW = datetime.now(CST)
# 当月月度包尚未发布，改用 daily 包逐日补；月初几天上月包也可能未出
CUR_MONTH = NOW.strftime("%Y-%m")
PREV_MONTH = (NOW.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
NEED_DAILY = {CUR_MONTH} | ({PREV_MONTH} if NOW.day <= 5 else set())


def month_list(start=START_MONTH, end=None):
    out, d = [], datetime.strptime(start + "-01", "%Y-%m-%d")
    end = end or CUR_MONTH
    while d.strftime("%Y-%m") <= end:
        out.append(d.strftime("%Y-%m"))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


MONTHS = month_list()


def _get_zip(url):
    for _ in range(2):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            zf = zipfile.ZipFile(io.BytesIO(raw))
            return zf.read(zf.namelist()[0]).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            time.sleep(1)
    return None


def _fetch_daily(sym, month):
    """逐日补 daily 包，只补到 UTC 昨天（当天包未生成）。"""
    parts = []
    y, m = map(int, month.split("-"))
    d = datetime(y, m, 1)
    today = datetime.now(timezone.utc).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
    while d.strftime("%Y-%m") == month and d < today:
        t = _get_zip(f"{BASE_D}/{sym}/1h/{sym}-1h-{d.strftime('%Y-%m-%d')}.zip")
        if t:
            parts.append(t)
        time.sleep(0.03)
        d += timedelta(days=1)
    return "\n".join(parts) if parts else None


def fetch_zip(sym, month):
    """先取月度包；当月/上月未发布时自动回落 daily 逐日。"""
    txt = _get_zip(f"{BASE}/{sym}/1h/{sym}-1h-{month}.zip")
    if txt is not None:
        return txt
    if month in NEED_DAILY:
        return _fetch_daily(sym, month)
    return None


def parse(txt):
    bars = []
    for line in txt.splitlines():
        p = line.strip().split(",")
        if len(p) < 6:
            continue
        try:
            t = int(float(p[0]))
        except ValueError:
            continue  # header
        if t < 10 ** 12:  # 秒 -> 毫秒
            t *= 1000
        try:
            bars.append((t, float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
        except ValueError:
            continue
    return bars


def main():
    os.makedirs("data", exist_ok=True)
    files = sorted(glob.glob("data/positions_*.csv"))
    positions, syms = [], set()
    for f in files:
        pid = f.split("positions_")[1].split(".")[0]
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            if not r.get("symbol") or not r.get("opened_ms"):
                continue
            r["_pid"], r["_open"], r["_sym"] = pid, int(float(r["opened_ms"])), r["symbol"].upper()
            positions.append(r)
            syms.add(r["_sym"])
    print(f"positions={len(positions)} symbols={len(syms)}")

    kl, miss = {}, []
    for i, s in enumerate(sorted(syms), 1):
        bars = []
        got = False
        for m in MONTHS:
            t = fetch_zip(s, m)
            if t:
                bars.extend(parse(t)); got = True
            time.sleep(0.05)
        if not got:
            miss.append(s); continue
        bars.sort()
        kl[s] = bars
        if i % 25 == 0:
            print(f"  {i}/{len(syms)} {s} bars={len(bars)}")
    print(f"klines ok={len(kl)} missing={len(miss)} {miss[:15]}")

    idx = {s: {b[0]: i for i, b in enumerate(bars)} for s, bars in kl.items()}
    hourly = defaultdict(dict)
    for s, bars in kl.items():
        for i in range(1, len(bars)):
            prev, cur = bars[i - 1][4], bars[i][4]
            if prev:
                hourly[bars[i][0]][s] = (cur - prev) / prev * 100

    def ret(s, open_ms, hours):
        bars = kl.get(s)
        if not bars:
            return ""
        h0 = open_ms - open_ms % 3600000
        m = idx[s]
        if h0 not in m:
            return ""
        i0 = m[h0]
        j = i0 - hours
        if j < 0:
            return ""
        base = bars[j][4]
        return round((bars[i0][4] - base) / base * 100, 4) if base else ""

    out = []
    for r in positions:
        h0 = r["_open"] - r["_open"] % 3600000
        r1, r4, r24 = ret(r["_sym"], r["_open"], 1), ret(r["_sym"], r["_open"], 4), ret(r["_sym"], r["_open"], 24)
        rank = ""
        if isinstance(r1, float) and h0 in hourly and len(hourly[h0]) > 10:
            vals = sorted(hourly[h0].values())
            rank = round(sum(1 for v in vals if v < r1) / len(vals) * 100, 1)
        amp = ""
        bars = kl.get(r["_sym"])
        if bars and h0 in idx[r["_sym"]]:
            i0 = idx[r["_sym"]][h0]
            seg = bars[max(0, i0 - 24):i0 + 1]
            if seg and seg[0][4]:
                amp = round((max(b[2] for b in seg) - min(b[3] for b in seg)) / seg[0][4] * 100, 2)
        out.append({"portfolioId": r["_pid"], "symbol": r["_sym"], "side": r.get("side"),
                    "opened": r.get("opened"), "pnl": r.get("pnl"), "ret_pct": r.get("ret_pct"),
                    "duration_h": r.get("duration_h"), "ret_1h_pct": r1, "ret_4h_pct": r4,
                    "ret_24h_pct": r24, "ret_1h_rank_pct": rank, "amp_24h_pct": amp,
                    "has_kline": r["_sym"] in kl})

    with open("data/h3_features.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
    with gzip.open("data/klines_1h_all.csv.gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "open_ms", "open", "high", "low", "close", "volume"])
        for s, bars in kl.items():
            for b in bars:
                w.writerow([s] + list(b))

    # 当月增量分片：下游每日拼接，避免 29MB 全量走 Contents API
    cur_start = int(datetime.strptime(CUR_MONTH + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    n_cur = 0
    with gzip.open(f"data/klines_{CUR_MONTH}.csv.gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "open_ms", "open", "high", "low", "close", "volume"])
        for s, bars in kl.items():
            for b in bars:
                if b[0] >= cur_start:
                    w.writerow([s] + list(b)); n_cur += 1
    print(f"features rows={len(out)} cur_month={CUR_MONTH} bars={n_cur}")

    # TDS 结论表
    def bucket(v):
        if v == "" or v is None: return "NA"
        v = float(v)
        for hi, lab in ((-10, "<-10%"), (-5, "-10~-5%"), (-2, "-5~-2%"), (0, "-2~0%"),
                        (2, "0~2%"), (5, "2~5%"), (10, "5~10%")):
            if v < hi: return lab
        return ">10%"

    agg = defaultdict(lambda: [0, 0.0, 0])
    for o in out:
        if o["portfolioId"] != "4902752005274121217" or o["side"] != "Short":
            continue
        b = bucket(o["ret_1h_pct"])
        agg[b][0] += 1; agg[b][1] += float(o["pnl"] or 0)
        agg[b][2] += 1 if float(o["pnl"] or 0) > 0 else 0
    print("\nTDS 空单：开仓前1h涨幅 -> 笔数/盈亏/胜率")
    for b in ("<-10%", "-10~-5%", "-5~-2%", "-2~0%", "0~2%", "2~5%", "5~10%", ">10%", "NA"):
        if b in agg:
            n, p, w = agg[b]
            print(f"  {b:9s} n={n:4d} pnl={p:+10.1f} win={w/n*100:5.1f}%")

    # 分位桶
    agg2 = defaultdict(lambda: [0, 0.0, 0])
    for o in out:
        if o["portfolioId"] != "4902752005274121217" or o["side"] != "Short":
            continue
        r = o["ret_1h_rank_pct"]
        if r == "":
            agg2["NA"][0] += 1; continue
        k = f"{int(r // 20) * 20}-{int(r // 20) * 20 + 20}%"
        agg2[k][0] += 1; agg2[k][1] += float(o["pnl"] or 0)
        agg2[k][2] += 1 if float(o["pnl"] or 0) > 0 else 0
    print("\nTDS 空单：开仓时横截面涨幅分位 -> 笔数/盈亏/胜率")
    for k in sorted(agg2):
        n, p, w = agg2[k]
        print(f"  {k:8s} n={n:4d} pnl={p:+10.1f} win={(w/n*100 if n else 0):5.1f}%")


if __name__ == "__main__":
    main()
