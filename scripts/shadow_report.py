#!/usr/bin/env python3
"""影子复制周报：读 data/shadow_copy.jsonl，输出复制损耗汇总。

用法：python3 scripts/shadow_report.py [data/shadow_copy.jsonl]
"""
import json
import statistics
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
PATH = sys.argv[1] if len(sys.argv) > 1 else "data/shadow_copy.jsonl"


def med(xs):
    return round(statistics.median(xs), 3) if xs else None


def main():
    rows = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    if not rows:
        print("no data yet")
        return

    closed = [r for r in rows if r.get("closed")]
    open_now = [r for r in rows if not r.get("closed")]
    missed = [r for r in closed if r["first_seen_delay_s"] > (r["closed"] - r["opened"]) / 1000]
    # 已错过：我们首次看到时它已经平了——真实引擎根本跟不上的单

    print(f"样本窗口: {datetime.fromtimestamp(rows[0]['opened']/1000, CST):%m-%d %H:%M}"
          f" ~ {datetime.fromtimestamp(rows[-1]['opened']/1000, CST):%m-%d %H:%M} (开仓时间)")
    print(f"总仓位数 {len(rows)} | 已平 {len(closed)} | 未平 {len(open_now)} | 错过(开平在两次轮询之间) {len(missed)}")
    print(f"实际轮询延迟: 中位 {med([r['first_seen_delay_s'] for r in rows])}s "
          f"最大 {max(r['first_seen_delay_s'] for r in rows)}s")
    print()

    for label, key in [("延迟30s", "adverse_30s"), ("延迟60s", "adverse_60s"), ("延迟300s", "adverse_300s")]:
        adv = [abs(r[key]) for r in rows if r.get(key) is not None]
        print(f"入场价劣化 {label}: 中位 {med(adv)}% | 均值 "
              f"{round(sum(adv)/len(adv),3) if adv else '-'}% | 最大 {round(max(adv),3) if adv else '-'}%")
    print()

    lead_pnl = sum(float(r.get("closingPnl") or 0) for r in closed)
    print(f"带单员实际已平仓盈亏合计: {round(lead_pnl,2)}U ({len(closed)} 笔)")
    for label, key in [("延迟30s", "sim_pnl_30s"), ("延迟60s", "sim_pnl_60s"), ("延迟300s", "sim_pnl_300s")]:
        sim = [r[key] for r in closed if r.get(key) is not None]
        if sim:
            print(f"  复制模拟(跟到已平的{len(sim)}笔) {label}: 合计 {round(sum(sim),2)}% | "
                  f"中位单笔 {med(sim)}% | 胜率 {round(100*sum(1 for x in sim if x > 0)/len(sim),1)}%")
    print()
    # 方向与杠杆分布
    from collections import Counter
    sides = Counter(("多" if r["dir"] == 1 else "空") for r in rows)
    levs = Counter(r.get("leverage") for r in rows)
    print(f"方向分布: {dict(sides)} | 杠杆Top: {levs.most_common(5)}")
    hot = Counter(r["symbol"] for r in rows).most_common(5)
    print(f"高频标的: {hot}")


if __name__ == "__main__":
    main()
