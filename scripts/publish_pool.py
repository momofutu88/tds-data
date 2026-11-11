#!/usr/bin/env python3
"""把候选池产物发布到公开仓 quant-ref-data，供外部直接取用"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

TARGET = "momofutu88/quant-ref-data"
TOKEN = os.environ.get("PUB_TOKEN", "")
API = f"https://api.github.com/repos/{TARGET}/contents"
FILES = [
    ("data/candidate_pool.csv", "data/candidate_pool.csv"),
    ("data/market_snapshot.csv", "data/market_snapshot.csv"),
    ("data/pool_meta.json", "data/pool_meta.json"),
]


def get_sha(path):
    try:
        req = urllib.request.Request(f"{API}/{path}")
        req.add_header("Authorization", "token " + TOKEN)
        return json.load(urllib.request.urlopen(req, timeout=40))["sha"]
    except Exception:
        return None


def put(path, blob, msg):
    sha = get_sha(path)
    body = {"message": msg, "content": base64.b64encode(blob).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(f"{API}/{path}",
                                data=json.dumps(body).encode(), method="PUT")
    req.add_header("Authorization", "token " + TOKEN)
    return json.load(urllib.request.urlopen(req, timeout=90))["commit"]["sha"][:8]


def main():
    if not TOKEN:
        print("ERROR: PUB_TOKEN missing", file=sys.stderr)
        return 1
    ok = 0
    for src, dst in FILES:
        if not os.path.exists(src):
            print("skip missing", src)
            continue
        try:
            print(dst, "->", put(dst, open(src, "rb").read(), f"pool: {dst}"))
            ok += 1
        except Exception as e:
            print("failed", dst, type(e).__name__, e, file=sys.stderr)
    for f in ("data/candidate_pool.csv", "data/pool_meta.json"):
        try:
            urllib.request.urlopen(
                f"https://purge.jsdelivr.net/gh/{TARGET}@main/{f}", timeout=20).read()
        except Exception:
            pass
    print(f"published {ok}/{len(FILES)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
