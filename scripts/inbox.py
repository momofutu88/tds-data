#!/usr/bin/env python3
"""总工↔核验方 回执上行通道

核验方往 ntfy topic POST 一段文本，本脚本轮询拉取、去重、永久归档到公开仓。
ntfy 服务端只缓存 12h，归档后永久可读（raw.githubusercontent / jsDelivr）。

产物：
  inbox/messages.json  —— 结构化消息列表（按 time 升序，按 id 去重）
  inbox/lobster.md     —— 人类可读的合并文档（最新在上）
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

TOPIC = "wb-tds-gc-7f3a9c1e"
REPO = "momofutu88/quant-ref-data"
TOKEN = os.environ.get("PUB_TOKEN", "")
API = f"https://api.github.com/repos/{REPO}/contents"


def fetch_ntfy():
    url = f"https://ntfy.sh/{TOPIC}/json?poll=1&since=all"
    raw = urllib.request.urlopen(url, timeout=60).read().decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") != "message":
            continue
        out.append({
            "id": d.get("id", ""),
            "time": int(d.get("time", 0)),
            "title": d.get("title", "") or "",
            "message": d.get("message", "") or "",
        })
    return out


def gh_get_raw(path):
    """返回 (bytes 内容, sha)；文件不存在返回 (None, None)。不解析 JSON。"""
    try:
        req = urllib.request.Request(f"{API}/{path}")
        req.add_header("Authorization", "token " + TOKEN)
        r = json.load(urllib.request.urlopen(req, timeout=40))
        return base64.b64decode(r["content"]), r["sha"]
    except Exception:
        return None, None


def gh_get_json(path, default):
    data, sha = gh_get_raw(path)
    if data is None:
        return default, sha
    try:
        return json.loads(data.decode("utf-8")), sha
    except Exception:
        return default, sha


def gh_put(path, data, message, sha=None):
    last = None
    for _ in range(3):
        body = {"message": message,
                "content": base64.b64encode(data).decode()}
        if sha:
            body["sha"] = sha
        req = urllib.request.Request(f"{API}/{path}",
                                    data=json.dumps(body).encode(),
                                    method="PUT")
        req.add_header("Authorization", "token " + TOKEN)
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))["commit"]["sha"][:8]
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (409, 422):   # sha 过期/缺失 → 重取后重试
                _, sha = gh_get_raw(path)
                continue
            raise
    raise last


def render_md(msgs):
    import datetime
    lines = ["# 核验方回执（自动归档）", "",
             "> 通道：ntfy.sh/%s ｜ 自动去重归档 ｜ 最新在上" % TOPIC,
             "> 由 GitHub Actions 定时拉取，ntfy 端 12h 过期不影响此处归档。", ""]
    for m in sorted(msgs, key=lambda x: x["time"], reverse=True):
        ts = datetime.datetime.utcfromtimestamp(m["time"] + 8 * 3600).strftime("%Y-%m-%d %H:%M")
        head = f"## {ts}"
        if m["title"]:
            head += f" · {m['title']}"
        lines.append(head)
        lines.append("")
        lines.append(m["message"])
        lines.append("")
    return "\n".join(lines)


def main():
    if not TOKEN:
        print("ERROR: PUB_TOKEN missing", file=sys.stderr)
        return 1

    try:
        live = fetch_ntfy()
    except Exception as e:
        print("ntfy fetch failed:", type(e).__name__, e, file=sys.stderr)
        return 1

    old, sha = gh_get_json("inbox/messages.json", [])
    old = old or []
    seen = {m["id"] for m in old}
    new = [m for m in live if m["id"] and m["id"] not in seen]

    if new:
        merged = sorted(old + new, key=lambda m: m["time"])
        c1 = gh_put("inbox/messages.json",
                    json.dumps(merged, ensure_ascii=False, indent=1).encode("utf-8"),
                    f"inbox: +{len(new)} messages", sha)
    else:
        merged = old
        c1 = "-"

    if not merged:
        print("nothing to archive")
        return 0

    # 每次都重渲染，保证 md 与 json 始终一致
    _, sha2 = gh_get_raw("inbox/lobster.md")
    c2 = gh_put("inbox/lobster.md",
                render_md(merged).encode("utf-8"),
                f"inbox: render {len(merged)} messages", sha2)

    print(f"archived +{len(new)} (total {len(merged)}) commits {c1} {c2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
