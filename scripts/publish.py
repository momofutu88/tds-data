#!/usr/bin/env python3
"""把采集结果发布到公开只读数据仓（供另一会话/龙虾直接 curl 拉取）。
在 GitHub Actions 中运行，使用 PUB_TOKEN 调 Contents API，无需 git。
"""
import base64, csv, glob, json, os, sys, time, urllib.request

TARGET = "momofutu88/quant-ref-data"


def api(path, token, method="GET", body=None):
    url = f"https://api.github.com/repos/{TARGET}/contents/{path}"
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body else None)
    req.add_header("Authorization", "token " + token)
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def put(path, data: bytes, token, msg):
    try:
        sha = api(path, token)["sha"]
    except Exception:
        sha = None
    body = {"message": msg, "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    for i in range(3):
        try:
            return api(path, token, "PUT", body)
        except Exception as e:
            if i == 2:
                raise
            time.sleep(2)


def main():
    token = os.environ.get("PUB_TOKEN")
    if not token:
        print("no PUB_TOKEN, skip publish"); return
    ns = {"4902752005274121217": "TDS长期量化基金", "5170016259600335617": "爽翻天",
          "5263968339740503553": "天辰资本", "5236187913165742336": "法兰西基金",
          "4532994172262753536": "Sky株式会社"}
    rows_all, fields = [], None
    for f in sorted(glob.glob("data/positions_*.csv")):
        pid = f.split("positions_")[1].split(".")[0]
        rows = list(csv.DictReader(open(f, encoding="utf-8-sig")))
        if not rows:
            continue
        if fields is None:
            fields = ["portfolio"] + list(rows[0].keys())
        for r in rows:
            r["portfolio"] = ns.get(pid, pid)
            rows_all.append(r)
    if not rows_all:
        print("no rows"); return
    rows_all.sort(key=lambda r: float(r.get("opened_ms") or 0))
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader(); w.writerows(rows_all)
    put("data/all_positions.csv", buf.getvalue().encode("utf-8-sig"), token,
        f"data: {len(rows_all)} rows {time.strftime('%Y-%m-%d %H:%M')}")
    print(f"published all_positions.csv rows={len(rows_all)}")

    if os.path.exists("data/profiles.json"):
        put("data/profiles.json", open("data/profiles.json", "rb").read(), token, "data: profiles")
        print("published profiles.json")
    if os.path.exists("data/h3_features.csv"):
        put("data/h3_features.csv", open("data/h3_features.csv", "rb").read(), token, "data: h3 features")
        print("published h3_features.csv")

    # 当月 K 线增量分片（全量 29MB 不走 Contents API）
    for f in sorted(glob.glob("data/klines_*.csv.gz")):
        name = os.path.basename(f)
        if name == "klines_1h_all.csv.gz":
            continue
        blob = open(f, "rb").read()
        if len(blob) < 1024:
            print("skip tiny", name, len(blob)); continue
        put(f"data/{name}", blob, token, f"data: {name}")
        print("published", name, len(blob))

    # 清掉历史上的空壳全量文件，避免下游拿到 84 字节假数据
    try:
        cur = api("data/klines_1h_all.csv.gz", token)
        if cur.get("size", 0) < 1024:
            api("data/klines_1h_all.csv.gz", token, "DELETE",
                {"message": "data: drop empty klines dump", "sha": cur["sha"]})
            print("deleted empty klines_1h_all.csv.gz")
    except Exception as e:
        print("klines cleanup skip", type(e).__name__)

    # 清 jsDelivr 缓存，保证外部立刻拿到新版本
    for f in ("data/all_positions.csv", "data/profiles.json", "data/h3_features.csv"):
        try:
            urllib.request.urlopen(
                f"https://purge.jsdelivr.net/gh/{TARGET}@main/{f}", timeout=20).read()
            print("purged", f)
        except Exception as e:
            print("purge skip", f, type(e).__name__)


if __name__ == "__main__":
    main()
