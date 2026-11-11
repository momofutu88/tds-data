#!/usr/bin/env python3
"""1000万u之路 实时信号轮询器：私有仓 Actions 每 5 分钟跑一次。
1) 拉 position-history，取 closed==null 的当前持仓
2) 与上次状态 diff → 新开仓 / 新平仓
3) 新开仓立即推 ntfy（引擎实时消费）
4) 信号 JSON 发布到公开仓 data/mil_signal.json（CDN 供拉取）
5) 检出延迟指标：position-history 的 updateTime vs 平仓时间
"""
import base64, json, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from collect import BAPI, UA, post  # noqa: E402

MIL_PID = "5008273081378616064"
PRIV = "momofutu88/tds-data"
PUB = "momofutu88/quant-ref-data"
STATE = "data/mil_signal_state.json"
SIG_PUB = "data/mil_signal.json"
NTFY_TOPIC = "wb-tds-gc-7f3a9c1e"
CST = timezone(timedelta(hours=8))


def gh_api(repo, path, token, method="GET", body=None):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body else None)
    req.add_header("Authorization", "token " + token)
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def gh_get_raw(repo, path, token):
    url = f"https://raw.githubusercontent.com/{repo}/main/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "token " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception:
        return None


def gh_put(repo, path, data: bytes, token, msg):
    try:
        sha = gh_api(repo, path, token)["sha"]
    except Exception:
        sha = None
    body = {"message": msg, "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    for i in range(3):
        try:
            return gh_api(repo, path, token, "PUT", body)
        except Exception:
            if i == 2:
                raise
            time.sleep(2)


def ntfy(title, text):
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                 data=text.encode(), method="POST")
    req.add_header("Title", title.encode("ascii", "ignore").decode() or "mil-signal")
    req.add_header("Tags", "mil_signal")
    try:
        urllib.request.urlopen(req, timeout=20).read()
        print("ntfy pushed:", title)
    except Exception as e:
        print("ntfy fail:", type(e).__name__, e)


def main():
    token = os.environ.get("PUB_TOKEN")
    if not token:
        print("no PUB_TOKEN"); return

    d = post("/position-history",
             {"portfolioId": MIL_PID, "pageNumber": 1, "pageSize": 50})
    lst = d.get("list") or []
    open_pos = []
    for r in lst:
        if not r.get("closed"):
            open_pos.append({
                "symbol": r.get("symbol"), "side": r.get("side"),
                "opened_ms": r.get("opened"),
                "entry": r.get("avgCost"), "max_qty": r.get("maxOpenInterest"),
                "closed_qty": r.get("closedVolume"),
                "leverage": r.get("leverage"),
            })
    now_ms = int(time.time() * 1000)

    # 最近平仓记录（检出延迟观测）
    t = post("/trade-history",
             {"portfolioId": MIL_PID, "pageNumber": 1, "pageSize": 10})
    recent = (t.get("list") or []) if t else []

    prev = gh_get_raw(PRIV, STATE, token) or {}
    seen_key = lambda p: f"{p['symbol']}|{p['side']}|{p['opened_ms']}"
    prev_keys = {seen_key(p) for p in prev.get("open", [])}
    cur_keys = {seen_key(p) for p in open_pos}
    new_open = [p for p in open_pos if seen_key(p) not in prev_keys]
    new_close = [p for p in prev.get("open", []) if seen_key(p) not in cur_keys]

    sig = {
        "fetched_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "fetched_ms": now_ms, "pid": MIL_PID, "open": open_pos,
        "new_open": new_open, "new_close": new_close,
        "recent_trades": [{"symbol": r.get("symbol"), "side": r.get("side"),
                           "close_ms": r.get("closed"),
                           "pnl": r.get("closingPnl")} for r in recent[:10]],
    }

    # 新开仓：秒级 ntfy 推送（引擎消费入口）
    for p in new_open:
        lag_min = (now_ms - (p["opened_ms"] or now_ms)) / 60000.0
        ntfy("MIL OPEN %s %s" % (p["symbol"], p["side"]),
             "%s %s 开于%s 检出延迟%.1f分钟 entry=%s qty=%s lev=%s" % (
                 p["symbol"], p["side"],
                 datetime.fromtimestamp((p["opened_ms"] or now_ms) / 1000, CST)
                 .strftime("%m-%d %H:%M"), lag_min,
                 p["entry"], p["max_qty"], p["leverage"]))

    # 平仓检出延迟观测（trade-history 的 closeTime vs 我们此刻才发现）
    for r in sig["recent_trades"][:5]:
        if r["close_ms"]:
            lag = (now_ms - int(r["close_ms"])) / 60000.0
            print("close lag: %s %.1f min" % (r["symbol"], lag))

    gh_put(PRIV, STATE, json.dumps({"open": open_pos, "ts": now_ms},
                                   ensure_ascii=False).encode(), token,
           "state: mil signal")
    # 有变化才发公开仓（降低 commit 频率）；心跳每 12 次(1小时)发一次
    changed = new_open or new_close
    heartbeat = (now_ms // 3600000) != (prev.get("ts", 0) // 3600000)
    if changed or heartbeat or not prev:
        gh_put(PUB, SIG_PUB, json.dumps(sig, ensure_ascii=False).encode(), token,
               "mil signal %s" % ("CHANGED" if changed else "heartbeat"))
        try:
            urllib.request.urlopen(
                f"https://purge.jsdelivr.net/gh/{PUB}@main/{SIG_PUB}",
                timeout=20).read()
        except Exception:
            pass
    print("open=%d new_open=%d new_close=%d changed=%s" %
          (len(open_pos), len(new_open), len(new_close), bool(changed)))


if __name__ == "__main__":
    main()
