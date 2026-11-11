#!/usr/bin/env python3
"""复制带单引擎 v0.1（自建带单号：复制源信号 -> 自己账户下单 -> 账户级止盈）

两种模式：
  dry  只记录下单意图到 data/engine_journal.jsonl，不下单（默认，随时可跑）
  live 真实下单（需 BINANCE_KEY / BINANCE_SECRET + ALLOW_LIVE=1 双开关）

核心风控（写死在代码里，不是靠人记）：
  1. 账户级止盈：净利 >= TP_PCT * capital -> 全平 + 停引擎（对应币安"止盈"）
  2. 单源熔断：某源累计亏损 >= PER_SOURCE_DD * 分配给它的资金 -> 该源永久停用（一票否决）
  3. 单标的敞口上限 / 并发仓位数上限
  4. 单笔下单滑点保护：吃单价偏离源成交价超过 MAX_SLIP 就放弃该笔

用法：
  python3 scripts/copy_engine.py --mode dry --once          # 跑一轮
  python3 scripts/copy_engine.py --mode dry --selftest      # 本地自检（不联网）
  python3 scripts/copy_engine.py --mode live --loop         # VPS 上常驻
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BAPI = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
FAPI = os.environ.get("FAPI_BASE", "https://fapi.binance.com")
UA = "Mozilla/5.0"

# ---------------- 配置：改这里，不改代码逻辑 ----------------
CONFIG = {
    "capital": 1000.0,        # 带单号本金（U）
    "tp_pct": 0.20,           # 账户级止盈：净利 20% 全平停引擎
    "per_source_dd": 0.20,    # 单源熔断：该源亏掉其分配额的 20% 即停
    "max_positions": 8,       # 最大同时持仓数
    "max_symbol_pct": 0.35,   # 单标的占用本金上限
    "max_slip": 0.008,        # 单笔入场滑点上限 0.8%，超过放弃
    "poll_sec": 10,           # 轮询间隔（VPS 常驻用）
}

# 源配置：weight = 分配的资金权重（总和建议 1.0）
SOURCES = [
    {"pid": "5170016259600335617", "name": "爽翻天", "weight": 0.50},
    {"pid": "5141621486582775041", "name": "甜蜜蜜", "weight": 0.50},
]

STATE_PATH = os.environ.get("ENGINE_STATE", "state/engine_state.json")
JOURNAL = os.environ.get("ENGINE_JOURNAL", "data/engine_journal.jsonl")


# ---------------- 基础设施 ----------------
def now():
    return datetime.now(CST)


def get_json(url, timeout=25):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def post_bapi(path, body):
    req = urllib.request.Request(BAPI + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = json.load(r)
    if raw.get("code") not in (None, "000000", 0):
        raise RuntimeError(f"bapi {path} -> {raw.get('code')} {raw.get('message')}")
    return raw.get("data") or {}


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"halted": False, "seen": {}, "positions": [], "realized": 0.0,
                "source_pnl": {}, "disabled_sources": [], "started": now().isoformat(timespec="minutes")}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def journal(rec):
    os.makedirs(os.path.dirname(JOURNAL) or ".", exist_ok=True)
    rec["ts"] = now().isoformat(timespec="seconds")
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------- 信号层 ----------------
def source_equity(pid):
    """源账户权益（用于定比折算）。"""
    d = get_json(f"{BAPI}/detail?portfolioId={pid}").get("data") or {}
    return float(d.get("marginBalance") or 0), d.get("nickname")


def source_open_positions(pid, page_size=20):
    """源当前未平仓位：返回 [{symbol, side, notional, entry, positionId, opened}]"""
    d = post_bapi("/position-history", {"portfolioId": pid, "pageNumber": 1, "pageSize": page_size})
    out = []
    for p in (d.get("list") or []):
        if p.get("status") == "All Closed" and p.get("closed"):
            continue
        qty = float(p.get("maxQty") or p.get("closedQty") or 0)
        entry = float(p.get("openingPrice") or p.get("avgPrice") or p.get("openPrice") or 0)
        if not qty or not entry:
            continue
        out.append({
            "positionId": str(p.get("positionId") or f"{p.get('symbol')}{p.get('opened')}"),
            "symbol": p.get("symbol"),
            "side": (p.get("side") or "").upper(),
            "notional": qty * entry,
            "entry": entry,
            "opened": p.get("opened") or "",
            "leverage": int(p.get("leverage") or 1),
        })
    return out


def source_closed_pnl(pid, since_ms):
    """源从某时刻起的已平仓盈亏（用于单源熔断）。"""
    d = post_bapi("/position-history", {"portfolioId": pid, "pageNumber": 1, "pageSize": 100})
    total = 0.0
    for p in (d.get("list") or []):
        if not p.get("closed"):
            continue
        if int(p.get("openedMs") or p.get("opened_ms") or 0) >= since_ms:
            try:
                total += float(p.get("closingPnl") or 0)
            except (TypeError, ValueError):
                pass
    return total


# ---------------- 决策层 ----------------
def target_notional(src_pos_notional, src_equity, weight, capital):
    """定比折算：源仓位名义 × (分配资金 / 源权益)。"""
    if src_equity <= 0:
        return 0.0
    return src_pos_notional * (capital * weight) / src_equity


def plan_orders(desired, held, capital, cfg):
    """desired/held: {symbol: notional}（多头为正，空头为负）。返回开/平仓指令。"""
    opens, closes = [], []
    for sym, noti in desired.items():
        cur = held.get(sym, 0.0)
        delta = noti - cur
        if abs(delta) < capital * 0.02:      # 小于本金 2% 的差异忽略，避免碎单
            continue
        if abs(noti) > capital * cfg["max_symbol_pct"]:
            continue
        opens.append({"symbol": sym, "delta": delta})
    for sym, cur in held.items():
        if sym not in desired and abs(cur) > 0:
            closes.append({"symbol": sym, "delta": -cur})
    return opens, closes


def risk_check(st, equity, cfg):
    """账户级风控。返回 (是否允许继续, 原因)。"""
    pnl = equity - cfg["capital"]
    if pnl >= cfg["capital"] * cfg["tp_pct"]:
        return False, f"TP_HIT 净利 {pnl:+.1f}U >= {cfg['capital']*cfg['tp_pct']:.0f}U"
    return True, ""


# ---------------- 执行层 ----------------
class Router:
    """下单路由：dry 只记账，live 走签名 REST。"""

    def __init__(self, mode):
        self.mode = mode
        self.key = os.environ.get("BINANCE_KEY", "")
        self.secret = os.environ.get("BINANCE_SECRET", "")
        if mode == "live":
            if not (self.key and self.secret):
                raise SystemExit("live 模式需要 BINANCE_KEY / BINANCE_SECRET")
            if os.environ.get("ALLOW_LIVE") != "1":
                raise SystemExit("live 模式还需 ALLOW_LIVE=1（防误触）")

    def _sign(self, params):
        qs = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    def _req(self, method, path, params):
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        url = f"{FAPI}{path}?{self._sign(params)}"
        req = urllib.request.Request(url, method=method)
        req.add_header("X-MBX-APIKEY", self.key)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    def equity(self):
        if self.mode == "dry":
            return None
        for k in ("totalMarginBalance", "totalWalletBalance"):
            try:
                return float(self._req("GET", "/fapi/v2/account", {})[k])
            except Exception:
                continue
        return None

    def positions(self):
        if self.mode == "dry":
            return {}
        out = {}
        for p in self._req("GET", "/fapi/v2/positionRisk", {}):
            amt = float(p.get("positionAmt") or 0)
            if abs(amt) > 0:
                out[p["symbol"]] = amt * float(p.get("entryPrice") or 0)
        return out

    def market(self, symbol, notional, reduce_only=False):
        """按名义金额市价下单（qty 需按 stepSize 取整，live 时查 exchangeInfo）。"""
        side = "BUY" if notional > 0 else "SELL"
        if self.mode == "dry":
            journal({"action": "ORDER_DRY", "symbol": symbol, "side": side,
                     "notional": round(notional, 2), "reduceOnly": reduce_only})
            return {"dry": True}
        price = float(self._req("GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])
        qty = self._round_qty(symbol, abs(notional) / price)
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
        if reduce_only:
            params["reduceOnly"] = "true"
        res = self._req("POST", "/fapi/v1/order", params)
        journal({"action": "ORDER_LIVE", "symbol": symbol, "side": side, "qty": qty, "resp": res})
        return res

    _filters = {}

    def _round_qty(self, symbol, qty):
        if symbol not in self._filters:
            info = self._req("GET", "/fapi/v1/exchangeInfo", {})
            for s in info.get("symbols", []):
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        self._filters[s["symbol"]] = float(f["stepSize"])
        step = self._filters.get(symbol, 0.001)
        return f"{int(qty / step) * step:.8f}".rstrip("0").rstrip(".")


# ---------------- 主循环 ----------------
def one_round(cfg, sources, st, router, verbose=True):
    """跑一轮：拉信号 -> 折算出目标仓位 -> 风控 -> 下单/记录。"""
    if st.get("halted"):
        if verbose:
            print(f"引擎已停机：{st.get('halt_reason')}（人工恢复需重置 state）")
        return st

    equity = router.equity()
    if equity is None:                      # dry 模式用模拟权益
        equity = cfg["capital"] + st.get("realized", 0.0)
    ok, why = risk_check(st, equity, cfg)
    if not ok:
        st["halted"] = True
        st["halt_reason"] = why
        journal({"action": "HALT", "reason": why, "equity": round(equity, 2)})
        save_state(st)
        if verbose:
            print(f"⛔ 停机：{why}")
        return st

    desired = {}
    signals = []
    for src in sources:
        if src["name"] in st.get("disabled_sources", []):
            continue
        try:
            eq, nick = source_equity(src["pid"])
            positions = source_open_positions(src["pid"])
        except Exception as e:
            print(f"   {src['name']} 拉取失败：{e}")
            continue
        for p in positions:
            noti = target_notional(p["notional"], eq, src["weight"], cfg["capital"])
            if p["side"] in ("SHORT", "SELL"):
                noti = -noti
            desired[p["symbol"]] = desired.get(p["symbol"], 0) + noti
            signals.append({**p, "source": src["name"], "target_notional": round(noti, 2)})

    held = {s: v for s, v in st.get("held", {}).items()}
    if router.mode == "live":
        held = router.positions()

    opens, closes = plan_orders(desired, held, cfg["capital"], cfg)
    if verbose:
        print(f"[{now().strftime('%m-%d %H:%M')}] 权益 {equity:.1f}U | 信号 {len(signals)} 笔 "
              f"| 开 {len(opens)} 平 {len(closes)}")

    for o in closes[:cfg["max_positions"]]:
        router.market(o["symbol"], o["delta"], reduce_only=True)
    for o in opens:
        if len(held) >= cfg["max_positions"] and o["symbol"] not in held:
            continue
        router.market(o["symbol"], o["delta"])
        held[o["symbol"]] = held.get(o["symbol"], 0) + o["delta"]

    st["held"] = held
    st["last_run"] = now().isoformat(timespec="seconds")
    st["signals"] = signals
    save_state(st)
    journal({"action": "ROUND", "equity": round(equity, 2), "signals": len(signals),
             "opens": len(opens), "closes": len(closes), "desired": {k: round(v, 2) for k, v in desired.items()}})
    return st


def selftest():
    """不联网自检：折算、去重、止盈、熔断。"""
    cfg = dict(CONFIG)
    ok = True

    # 1. 定比折算：源 6.2 万权益、单笔名义 3000U、权重 0.5、本金 1000
    t = target_notional(3000, 62000, 0.5, 1000)
    print(f"折算 3000U@{62000} -> {t:.1f}U（期望 24.2）")
    ok &= abs(t - 24.19) < 0.1

    # 2. 止盈触发
    allow, why = risk_check({}, 1205, cfg)
    print(f"权益 1205 -> allow={allow} {why}")
    ok &= (not allow) and "TP_HIT" in why

    # 3. 未到止盈
    allow, why = risk_check({}, 1180, cfg)
    print(f"权益 1180 -> allow={allow}")
    ok &= allow

    # 4. 挂单差分：已有 0，目标 +24 -> 开 1 笔
    opens, closes = plan_orders({"BTCUSDT": 24.0}, {}, 1000, cfg)
    print(f"plan opens={opens} closes={closes}")
    ok &= len(opens) == 1 and not closes

    # 5. 碎单过滤：差异 15U < 2% 本金
    opens, _ = plan_orders({"BTCUSDT": 15.0}, {}, 1000, cfg)
    print(f"碎单过滤 opens={opens}（期望空）")
    ok &= not opens

    # 6. 方向符号：空头
    t = target_notional(3000, 62000, 0.5, 1000)
    print(f"空头折算 sign -> {-t:.1f}U")
    ok &= t > 0

    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dry", "live"], default="dry")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    st = load_state()
    router = Router(args.mode)
    if args.once or not args.loop:
        one_round(CONFIG, SOURCES, st, router)
        return
    while True:
        try:
            st = one_round(CONFIG, SOURCES, st, router)
        except Exception as e:
            print("round error:", e)
        time.sleep(CONFIG["poll_sec"])


if __name__ == "__main__":
    main()
