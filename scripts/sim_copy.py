#!/usr/bin/env python3
"""跟单模拟：按带单员历史仓位序列，复制到固定本金，叠加提现纪律 + 止损。

口径：r_i = pnl_i / margin(带单员当前余额)，跟单账户权益 × (1+r_i)。
分润仅对盈利笔扣 10%。止损线 = max(750, 提现后权益×0.6)。
提现纪律（暴力组）：25%→20%M，50%→50%M，100%→100%M，150%→140%M，200%→180%M。
"""
import csv, json, sys

VIOLENT = [(0.25, .20), (0.50, .50), (1.00, 1.00), (1.50, 1.40), (2.00, 1.80)]
CST = None


def load(pid, d="data"):
    raw = json.load(open(f"{d}/detail_{pid}.json", encoding="utf-8"))
    det = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    rows = list(csv.DictReader(open(f"{d}/positions_{pid}.csv", encoding="utf-8-sig")))
    closed = [r for r in rows if r.get("pnl") not in ("", None) and r.get("closed_ms")]
    closed.sort(key=lambda r: int(r["closed_ms"]))
    return det, closed


def target(profit, M):
    if profit <= 0:
        return 0.0, None
    ratio = profit / M
    tgt, hit = 0.0, None
    for m, cut in VIOLENT:
        if ratio >= m:
            tgt, hit = cut * M, m
    if ratio > 2.0:
        ex = int((ratio - 2.0) / 0.5)
        tgt, hit = (1.80 + 0.5 * ex) * M, 2.0 + 0.5 * ex
    return min(tgt, profit), hit


def sim(pid, M=1000, stop_pct=0.25, fee=0.10, d="data"):
    det, rows = load(pid, d)
    margin = float(det.get("marginBalance") or 0)
    if not margin:
        return {"name": det.get("nickname"), "err": "no margin"}
    eq, peak, withdrawn, wd_hits = M, M, 0.0, []
    stop_line = M * (1 - stop_pct)
    bust, mdd, worst_trade = False, 0.0, 0.0

    for r in rows:
        pnl = float(r["pnl"] or 0)
        rel = pnl / margin
        ret = rel * (1 - fee) if rel > 0 else rel
        eq *= (1 + ret)
        worst_trade = min(worst_trade, ret * 100)
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
        if eq <= stop_line:
            bust = True
            break
        profit = eq - M
        tgt, hit = target(profit, M)
        if tgt > withdrawn:
            eq -= (tgt - withdrawn)
            withdrawn = tgt
            wd_hits.append(round(hit * 100) if hit else 0)
            stop_line = max(M * (1 - stop_pct), eq * 0.6)

    return {"name": det.get("nickname"), "pid": pid, "M": M, "n": len(rows),
            "end_eq": round(eq, 1), "withdrawn": round(withdrawn, 1),
            "total": round(eq + withdrawn, 1),
            "ret_pct": round((eq + withdrawn - M) / M * 100, 1),
            "mdd_pct": round(mdd / peak * 100, 1) if peak else 0,
            "worst_trade_pct": round(worst_trade, 1),
            "busted": bust, "wd_hits": wd_hits,
            "margin": margin, "copierPnl": det.get("copierPnl"),
            "copyAum": det.get("copyAum")}


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "data"
    pids = sys.argv[2].split(",")
    M = float(sys.argv[3]) if len(sys.argv) > 3 else 1000
    out = [sim(p, M, d=d) for p in pids]
    print(json.dumps(out, ensure_ascii=False, indent=1))
