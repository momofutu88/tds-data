#!/usr/bin/env python3
"""通过 GitHub Git Data API 一次性提交多个文件（绕开 git push 的网络中断）。"""
import base64, json, os, sys, urllib.request

import os
TOKEN = os.environ.get("GH_TOKEN", "")
REPO = "momofutu88/tds-data"
API = "https://api.github.com"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Authorization", f"token {TOKEN}")
    r.add_header("User-Agent", "tds-bot")
    r.add_header("Accept", "application/vnd.github+json")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.load(resp)


def main():
    files = sys.argv[1:]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ref = call("GET", f"/repos/{REPO}/git/ref/heads/main")
    head = ref["object"]["sha"]
    base = call("GET", f"/repos/{REPO}/git/commits/{head}")["tree"]["sha"]
    print("head", head[:8], "tree", base[:8])

    tree = []
    for f in files:
        p = os.path.join(root, f)
        content = base64.b64encode(open(p, "rb").read()).decode()
        blob = call("POST", f"/repos/{REPO}/git/blobs", {"content": content, "encoding": "base64"})
        tree.append({"path": f, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print("blob", f, blob["sha"][:8])

    new_tree = call("POST", f"/repos/{REPO}/git/trees", {"base_tree": base, "tree": tree})
    commit = call("POST", f"/repos/{REPO}/git/commits",
                  {"message": "feat: 到档即时推送 + follower 探测", "tree": new_tree["sha"], "parents": [head]})
    call("PATCH", f"/repos/{REPO}/git/refs/heads/main", {"sha": commit["sha"]})
    print("COMMIT", commit["sha"])


if __name__ == "__main__":
    main()
