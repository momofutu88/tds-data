#!/usr/bin/env python3
"""提现纪律核对器 · 双模式

  --mode review   每周三/日 20:00 全量核对（含未到档明细）
  --mode instant  每日扫描，只在「首次跨档」或「临近档位 90%」时推送（状态存 state/）

数据口径：带单员 position-history 中 opened >= 跟单起始时间的已平仓盈亏
× 跟单比例 × (1-分润)，推算理论净盈亏。实测系统性偏低约 20~25%（分批加仓导致），
所以真实到档以 App「净盈亏」为准；本脚本负责「提醒你去核对」，不是账本。
"""
import argparse, json, os, smtplib, urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header

BAPI = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CST = timezone(timedelta(hours=8))
NTFY = os.environ.get("NTFY_TOPIC") or "wb-tds-gc-7f3a9c1e"
STATE_PATH = os.environ.get("STATE_PATH", "state/withdraw_state.json")

# ---- 账户配置：改仓后手动更新 ----
# 法兰西 2026-11-11 00:35 止损触发出局（净盈亏 -171.31，回收 127.9U）——纪律：一票否决，不再跟
# 2026-11-11 01:30 起执行新原则：纪律一律由平台止盈/止损执行，推送只做记账复盘，不做执行依据
# tp = 平台止盈线（净盈亏占 M 的比例）。止盈触发后平台自动平仓+停止跟单，需手动重跟并重设止盈
ACCOUNTS = [
    dict(name="爽翻天",   pid="5170016259600335617", M=500.0000, start="2026-11-10 11:47", stop=250, fee=0.13, group="V", tp=0.20),
    dict(name="TDS",      pid="4902752005274121217", M=199.1917, start="2026-11-07 17:25", stop=155, fee=0.10, group="C", tp=2.00),
    dict(name="1000万u",  pid="5008273081378616064", M=301.9559, start="2026-11-10 11:39", stop=225, fee=0.10, group="C", tp=2.00),
    dict(name="Sky",      pid="4532994172262753536", M=297.9688, start="2026-11-05 12:29", stop=225, fee=0.10, group="C", tp=2.00),
    dict(name="甜蜜蜜",    pid="5141621486582775041", M=150.0000, start="2026-11-11 13:06", stop=100, fee=0.10, group="V", tp=0.20),
]

VIOLENT = [(0.25, .20), (0.50, .50), (1.00, 1.00), (1.50, 1.40), (2.00, 1.80)]
STOP_PCT = 0.40      # 止损 = 提后权益 × 0.6
NEAR_RATIO = 0.90    # 距下一档 90% 触发预档提醒（补偿推算低估）


def get_json(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r).get("data") or {}


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BAPI + path, data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    return j.get("data") or {}


def leader_balance(pid):
    d = get_json(f"{BAPI}/detail?portfolioId={pid}")
    return float(d.get("marginBalance") or 0), d.get("nickname", "")


def pnl_since(pid, start_str):
    """跟单开始后「新开仓」的累计平仓盈亏。"""
    start_ms = int(datetime.strptime(start_str, "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp() * 1000)
    total, page = 0.0, 1
    while page <= 30:
        d = post("/position-history", {"portfolioId": pid, "pageNumber": page, "pageSize": 100})
        lst = d.get("list") or []
        if not lst:
            break
        hit_old = False
        for p in lst:
            if (p.get("opened") or 0) < start_ms:
                hit_old = True
                continue
            if not p.get("closed"):
                continue
            total += float(p.get("closingPnl") or 0)
        if hit_old or len(lst) < 100 or page * 100 >= (d.get("total") or 0):
            break
        page += 1
    return total


def violent_target(tp, M):
    if tp <= 0:
        return 0.0, None
    r = tp / M
    tgt, hit = 0.0, None
    for mult, cut in VIOLENT:
        if r >= mult:
            tgt, hit = cut * M, mult
    if r > 2.0:
        extra = int((r - 2.0) / 0.5)
        tgt = (1.80 + 0.5 * extra) * M
        hit = 2.0 + 0.5 * extra
    return min(tgt, tp), hit


def compound_target(tp, M):
    if tp <= 0 or tp < 2.0 * M:
        return 0.0, None
    return min(tp - M, tp), 2.0


def next_line(tp, M, group):
    r = tp / M
    if group == "V":
        for mult, cut in VIOLENT:
            if r < mult:
                return mult, mult * M
        return 2.0 + 0.5 * (int((r - 2.0) / 0.5) + 1), (2.0 + 0.5 * (int((r - 2.0) / 0.5) + 1)) * M
    return 2.0, 2.0 * M


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def fmt_block(a, tp, eq, hit, tgt, nxt_r, nxt_amt, stop_new, act):
    grp = "暴力组" if a["group"] == "V" else "复利组"
    out = [f"■ {a['name']}（{grp}）  净利 {tp:+.1f}U / 净跟单 {a['M']:.1f}U = {tp/a['M']*100:.1f}%",
           f"   仓位：净跟单 {a['M']:.1f}U | 当前权益 {eq:.1f}U"]
    if tgt > 0:
        out += [f"   到档：{hit*100:.0f}% 档 → 提 {tgt:.1f}U",
                f"   提后：权益 {eq - tgt:.1f}U（本金 {a['M']:.1f}U 已全回）" if hit >= 1.0
                else f"   提后：权益 {eq - tgt:.1f}U",
                f"   止损：{a['stop']} → {stop_new}（{act}）"]
    else:
        out += [f"   下一档：{nxt_r*100:.0f}%（{nxt_amt:.1f}U），差 {nxt_amt - tp:.1f}U",
                f"   止损：{a['stop']}（不动）"]
    return "\n".join(out)


def build(mode="review", state=None):
    state = state if state is not None else {}
    lines, alerts, near, hits, near_map = [], [], [], {}, {}
    today = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    lines.append(f"【提现纪律{'到档扫描' if mode == 'instant' else '核对'} · {today}】")
    lines.append("推算值系统性偏低 20~25%，到档以 App「净盈亏」为准\n")

    for a in ACCOUNTS:
        try:
            bal, _ = leader_balance(a["pid"])
            pnl = pnl_since(a["pid"], a["start"])
        except Exception as e:
            if mode == "review":
                lines.append(f"■ {a['name']}：数据拉取失败（{e}）\n")
            continue
        if bal <= 0:
            continue
        k = a["M"] / bal * (1 - a["fee"])
        tp = pnl * k
        eq = a["M"] + tp
        tgt, hit = (violent_target if a["group"] == "V" else compound_target)(tp, a["M"])
        nxt_r, nxt_amt = next_line(tp, a["M"], a["group"])
        stop_new = max(a["stop"], round((eq - tgt) * (1 - STOP_PCT))) if tgt > 0 else a["stop"]
        act = "改" if stop_new != a["stop"] else "不动"
        hits[a["name"]] = hit or 0

        prev = float(state.get(a["name"], {}).get("hit") or 0)
        if mode == "instant":
            if hit and hit > prev:
                alerts.append(fmt_block(a, tp, eq, hit, tgt, nxt_r, nxt_amt, stop_new, act))
                lines.append(fmt_block(a, tp, eq, hit, tgt, nxt_r, nxt_amt, stop_new, act) + "\n")
            elif not hit and nxt_amt > 0 and tp >= nxt_amt * NEAR_RATIO:
                prev_near = float(state.get(a["name"], {}).get("near") or 0)
                if prev_near < nxt_r:
                    near.append(fmt_block(a, tp, eq, 0, 0, nxt_r, nxt_amt, a["stop"], "不动"))
                    near_map[a["name"]] = nxt_r
                    lines.append(fmt_block(a, tp, eq, 0, 0, nxt_r, nxt_amt, a["stop"], "不动") + "\n")
        else:
            lines.append(fmt_block(a, tp, eq, hit, tgt, nxt_r, nxt_amt, stop_new, act) + "\n")
            if hit:
                alerts.append(f"{a['name']} 已到 {hit*100:.0f}% 档，提 {tgt:.0f}U，止损 {a['stop']}→{stop_new}")

    if alerts:
        head = ["⚠️ 已到档，需要操作："] + [f"   · {x.splitlines()[0]}" for x in alerts] + [""]
        lines = lines[:2] + head + lines[2:]
    return "\n".join(lines), alerts, near, hits, near_map


def push_ntfy(title, body, tags="moneybag"):
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{NTFY}", data=body.encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Tags", tags)
        urllib.request.urlopen(req, timeout=20)
        print("ntfy ok")
    except Exception as e:
        print("ntfy fail:", e)


def send_mail(subject, body):
    user, pwd, to = (os.environ.get(k) for k in ("MAIL_USER", "MAIL_PASS", "MAIL_TO"))
    if not (user and pwd and to):
        print("mail skipped: no MAIL_USER/MAIL_PASS/MAIL_TO")
        return
    host = os.environ.get("MAIL_HOST", "smtp.qq.com")
    port = int(os.environ.get("MAIL_PORT", "465"))
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    try:
        s = smtplib.SMTP_SSL(host, port, timeout=25)
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
        s.quit()
        print("mail ok ->", to)
    except Exception as e:
        print("mail fail:", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["review", "instant"], default="review")
    ap.add_argument("--force", action="store_true", help="instant 模式下即使无新档也推送")
    args = ap.parse_args()

    st = load_state()
    body, alerts, near, hits, near_map = build(args.mode, st)
    print(body)

    if args.mode == "instant":
        if not alerts and not near and not args.force:
            print("no new tier -> silent")
            # 仍然记录已达档位，避免下次重复
            for a in ACCOUNTS:
                st.setdefault(a["name"], {})["hit"] = max(float(st.get(a["name"], {}).get("hit") or 0), hits.get(a["name"], 0))
            save_state(st)
            raise SystemExit(0)

        if alerts:
            title = "WITHDRAW ALERT"
            subj = f"[跟单·到档] {alerts[0].splitlines()[0].replace('■ ', '').split('（')[0]} —— 请核对后提现"
        else:
            title = "WITHDRAW NEAR"
            subj = "[跟单·预档] 距下一档 10% 以内，提前准备"
        if os.environ.get("DRY") != "1":
            push_ntfy(title, body[:3500], "rotating_light" if alerts else "warning")
            send_mail(subj, body)
    else:
        title = "WITHDRAW ALERT" if alerts else "WITHDRAW CHECK"
        if os.environ.get("DRY") != "1":
            push_ntfy(title, body[:3500])
            send_mail(f"[跟单纪律] {'有到档' if alerts else '周核对'} {datetime.now(CST).strftime('%m-%d')}", body)

    # 更新状态：已达档位 + 预档档位
    for a in ACCOUNTS:
        e = st.setdefault(a["name"], {})
        e["hit"] = max(float(e.get("hit") or 0), hits.get(a["name"], 0))
        if a["name"] in near_map:
            e["near"] = max(float(e.get("near") or 0), near_map[a["name"]])
        e["checked"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    save_state(st)
