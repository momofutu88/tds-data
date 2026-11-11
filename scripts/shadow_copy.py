#!/usr/bin/env python3
"""影子复制引擎（只记录，不下单）。

目的：为 12 月「暴力聚合带单号」提供复制损耗数据。
方法：每 5 分钟轮询暴力源的 position-history，发现新仓位后拉 1m K 线，
      量化「延迟 30s / 60s / 300s / 实际轮询延迟」跟进时的入场价劣化与模拟盈亏。
输出：data/shadow_copy.jsonl —— 每行一个仓位快照记录
状态：state/shadow_state.json —— 已见仓位去重
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
BAPI = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
FAPI = "https://fapi.binance.com"
UA = "Mozilla/5.0"

# 暴力源清单：pid -> (名称, 复制起点)。开新源在这里加一行。
SOURCES = {
    "5170016259600335617": ("爽翻天", "2026-11-10 11:47"),
}

STATE_PATH = os.environ.get("SHADOW_STATE", "state/shadow_state.json")
DATA_PATH = os.environ.get("SHADOW_DATA", "data/shadow_copy.jsonl")
NTFY = os.environ.get("NTFY_TOPIC", "")

PRICE_KEYS = ["openingPrice", "avgPrice", "openPrice", "openingAvgPrice", "entryPrice"]
CLOSE_KEYS = ["closingPrice", "avgClosePrice", "closePrice", "markPrice"]


def get_json(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BAPI + path, data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("data") or {}


KLINE_URLS = [
    FAPI + "/fapi/v1/klines",                 # 正主（部分地区被 451 拒）
    "https://www.binance.com/fapi/v1/klines", # www 代理路径
    "https://www.binance.com/api/v3/klines",  # 现货价兜底（近似）
]


def klines(symbol, start_ms, end_ms):
    """1m K 线（跨度>6h 自动降为 5m，返回 interval 和 {openTime: close}）。"""
    span = end_ms - start_ms
    interval = "1m" if span <= 6 * 3600 * 1000 else "5m"
    step = 60000 if interval == "1m" else 300000
    qs = (f"?symbol={symbol}&interval={interval}"
          f"&startTime={max(0, start_ms - step)}&endTime={end_ms}&limit=1500")
    km = {}
    err = None
    for base in KLINE_URLS:
        try:
            raw = get_json(base + qs)
            if isinstance(raw, list) and raw:
                for k in raw:
                    km[int(k[0])] = float(k[4])  # close
                break
        except Exception as e:
            err = e
    if not km and err:
        print(f"   kline fail {symbol}: {err}")
    return interval, step, km


def price_at(km, step, t):
    """取覆盖时刻 t 的那根 K 线的收盘价。"""
    if not km:
        return None
    base = (t // step) * step
    if base in km:
        return km[base]
    near = [k for k in km if abs(k - base) <= step * 2]
    return km[min(near)] if near else None


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%m-%d %H:%M:%S") if ms else None


def notify(text):
    if not NTFY:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{NTFY}", data=text.encode(), method="POST")
        req.add_header("Title", "影子引擎")
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"   ntfy fail: {e}")


def main():
    state = {}
    if os.path.exists(STATE_PATH):
        state = json.load(open(STATE_PATH, encoding="utf-8"))
    seen = set(state.get("seen", []))

    now_ms = int(time.time() * 1000)
    new_rows = []

    for pid, (name, start_str) in SOURCES.items():
        start_ms = int(datetime.strptime(start_str, "%Y-%m-%d %H:%M")
                       .replace(tzinfo=CST).timestamp() * 1000)
        try:
            d = post("/position-history", {"portfolioId": pid, "pageNumber": 1, "pageSize": 50})
        except Exception as e:
            print(f"[{name}] history fail: {e}")
            continue
        lst = d.get("list") or []
        fresh = [p for p in lst
                 if str(p.get("id") or f"{p.get('symbol')}-{p.get('opened')}") not in seen
                 and (p.get("opened") or 0) >= start_ms]
        print(f"[{name}] page1={len(lst)} fresh={len(fresh)}")

        for p in fresh:
            oid = str(p.get("id") or f"{p.get('symbol')}-{p.get('opened')}")
            seen.add(oid)
            opened = p.get("opened") or 0
            closed = p.get("closed")
            sym = p.get("symbol", "")
            side_raw = str(p.get("side") or "")
            long_ = any(w in side_raw.lower() for w in ("long", "buy")) or "多" in side_raw
            direction = 1 if long_ else -1
            interval, step, km = klines(sym, opened, now_ms)

            def adv(delay_s):
                q = price_at(km, step, opened + delay_s * 1000)
                p0 = price_at(km, step, opened)
                if not q or not p0:
                    return None
                return round(direction * (q / p0 - 1) * 100, 4)

            exit_price = None
            for k in CLOSE_KEYS:
                if p.get(k):
                    exit_price = float(p[k]); break
            if exit_price is None and closed:
                exit_price = price_at(km, step, closed)
            p_open = price_at(km, step, opened)

            def sim_pnl(delay_s):
                q = price_at(km, step, opened + delay_s * 1000)
                if not q or not exit_price:
                    return None
                return round(direction * (exit_price / q - 1) * 100, 4)

            rec = dict(
                pid=pid, name=name, oid=oid, symbol=sym, side=side_raw, dir=direction,
                leverage=p.get("leverage"), opened=opened, opened_s=fmt_ts(opened),
                closed=closed, closed_s=fmt_ts(closed),
                status=p.get("status"), closingPnl=p.get("closingPnl"), roi=p.get("roi"),
                poll_ts=now_ms, first_seen_delay_s=round((now_ms - opened) / 1000),
                kline_interval=interval,
                p_open=p_open, exit_price=exit_price,
                adverse_30s=adv(30), adverse_60s=adv(60), adverse_300s=adv(300),
                sim_pnl_30s=sim_pnl(30), sim_pnl_60s=sim_pnl(60), sim_pnl_300s=sim_pnl(300),
            )
            for k in PRICE_KEYS:
                if p.get(k):
                    rec[f"src_{k}"] = p[k]
            new_rows.append(rec)
            tag = "CLOSED" if closed else "OPEN  "
            print(f"   {tag} {sym} {side_raw} {p.get('leverage')}x opened={rec['opened_s']} "
                  f"delay={rec['first_seen_delay_s']}s adv30={rec['adverse_30s']}% "
                  f"simPnL30={rec['sim_pnl_30s']}%")

    if not new_rows:
        print("no new positions")
        return

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    state["seen"] = list(seen)[-800:]
    state["last_new"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    ntfy_lines = [f"影子引擎 {len(new_rows)} 笔新仓:"]
    for r in new_rows:
        ntfy_lines.append(f"{r['symbol']} {r['side']} {r['leverage']}x "
                          f"延迟劣化30s={r['adverse_30s']}% simPnL30={r['sim_pnl_30s']}%")
    notify("\n".join(ntfy_lines))
    print(f"saved {len(new_rows)} rows -> {DATA_PATH}")


if __name__ == "__main__":
    main()
