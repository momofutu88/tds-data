#!/usr/bin/env python3
"""带单员仓位风险画像：单笔风险、回撤、集中度、跟单适配度。"""
import csv, json, sys, statistics
from collections import Counter, defaultdict
from datetime import datetime

CST_FMT = "%Y-%m-%d %H:%M"


def load_pid(pid, data="data"):
    raw = json.load(open(f"{data}/detail_{pid}.json", encoding="utf-8"))
    det = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    try:
        rows = list(csv.DictReader(open(f"{data}/positions_{pid}.csv", encoding="utf-8-sig")))
    except FileNotFoundError:
        rows = []
    return det, rows


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def analyze(pid, data="data"):
    det, rows = load_pid(pid, data)
    name = det.get("nickname", pid)
    margin = f(det.get("marginBalance"))
    aum = f(det.get("aumAmount"))
    copy_aum = f(det.get("copyAum"))
    copier_pnl = f(det.get("copierPnl"))
    out = {"name": name, "pid": pid, "margin": margin, "aum": aum, "copyAum": copy_aum,
           "copierPnl": copier_pnl, "cur": det.get("currentCopyCount"), "max": det.get("maxCopyCount"),
           "totalCopy": det.get("totalCopyCount"), "fee": det.get("profitSharingRate"),
           "lock": det.get("lockPeriod"), "sharpe": det.get("sharpRatio"),
           "tags": det.get("tag"), "desc": det.get("description"),
           "fixedMin": det.get("fixedRadioMinCopyUsd") or det.get("fixedRadioMinCopyUsdStr"),
           "n_pos": len(rows)}

    closed = [r for r in rows if r["pnl"] not in ("", None) and r["closed_ms"]]
    if not closed:
        out["note"] = "无已平仓数据"
        return out

    pnls = [f(r["pnl"]) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    out.update({
        "n_closed": len(closed),
        "sum_pnl": round(sum(pnls), 1),
        "avg_pnl": round(statistics.mean(pnls), 1),
        "median_pnl": round(statistics.median(pnls), 1),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "max_win": round(max(pnls), 1),
        "max_loss": round(min(pnls), 1),
        "avg_win": round(statistics.mean(wins), 1) if wins else 0,
        "avg_loss": round(statistics.mean(losses), 1) if losses else 0,
    })
    out["pf"] = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None
    # 单笔最大亏 / 带单员当前自有余额 —— 爆仓/回撤代理
    out["max_loss_vs_margin"] = round(abs(min(pnls)) / margin * 100, 1) if margin else None
    out["max_loss_vs_aum"] = round(abs(min(pnls)) / aum * 100, 2) if aum else None
    # 名义仓位 / 余额
    notional = [f(r["max_qty"]) for r in closed if f(r["max_qty"]) > 0]
    if notional:
        out["avg_notional"] = round(statistics.mean(notional))
        out["max_notional"] = round(max(notional))
        out["notional_x_margin"] = round(max(notional) / margin, 1) if margin else None

    # 累积曲线回撤（按平仓时间）
    closed_sorted = sorted(closed, key=lambda r: int(r["closed_ms"]))
    cum, peak, mdd, peak_val = 0.0, 0.0, 0.0, 0.0
    for r in closed_sorted:
        cum += f(r["pnl"])
        peak = max(peak, cum)
        dd = cum - peak
        if dd < mdd:
            mdd = dd
    out["cum_pnl"] = round(cum, 1)
    out["mdd_abs"] = round(mdd, 1)
    out["mdd_vs_margin"] = round(abs(mdd) / margin * 100, 1) if margin else None
    out["mdd_vs_profit"] = round(abs(mdd) / cum * 100, 1) if cum > 0 else None

    # 连亏
    streak = worst = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        worst = max(worst, streak)
    out["max_loss_streak"] = worst

    # 集中度 / 方向 / 杠杆
    by_sym = Counter()
    for r in closed:
        by_sym[r["symbol"]] += abs(f(r["pnl"]))
    tot = sum(by_sym.values()) or 1
    top = by_sym.most_common(3)
    out["top_symbols"] = [(s, round(v / tot * 100, 1)) for s, v in top]
    out["n_symbols"] = len(by_sym)
    sides = Counter(r["side"] for r in closed)
    out["side_share"] = {k: round(v / len(closed) * 100, 1) for k, v in sides.items()}
    levs = [f(r["leverage"]) for r in closed if f(r["leverage"]) > 0]
    if levs:
        out["lev_median"] = statistics.median(levs)
        out["lev_max"] = max(levs)
    h1 = sum(1 for r in closed if f(r["duration_h"], 999) <= 1)
    out["share_under_1h"] = round(h1 / len(closed) * 100, 1)

    # 时间分段
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90)):
        cut = int(datetime.now().timestamp() * 1000) - days * 86400_000
        seg = [r for r in closed if int(r["closed_ms"]) >= cut]
        if seg:
            sp = [f(r["pnl"]) for r in seg]
            out[f"{label}_pnl"] = round(sum(sp), 1)
            out[f"{label}_n"] = len(seg)
            out[f"{label}_wr"] = round(len([x for x in sp if x > 0]) / len(sp) * 100, 1)
    # 首次/最后交易日
    if closed_sorted:
        out["first_trade"] = closed_sorted[0]["opened"]
        out["last_trade"] = closed_sorted[-1]["closed_at"]
    return out


if __name__ == "__main__":
    data = sys.argv[1] if len(sys.argv) > 1 else "data"
    pids = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    res = [analyze(p, data) for p in pids]
    print(json.dumps(res, ensure_ascii=False, indent=1))
