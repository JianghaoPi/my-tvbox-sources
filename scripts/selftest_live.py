#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live.py 离线自测：本地 mock HTTP 服务器跑通全管线，断言 12 项。

覆盖路径：m3u/txt 解析、频道名规范化合并（CCTV-1 与 CCTV-1综合 归并、URL 全局去重）、
测速过滤（404/超时判死，存活保留）、txt 的 type 字样同形字替换与逗号转义、
三格式产物、live_status/live_shield、README LIVE 段回写+徽章改写、aggregate.json lives 注入、
HTML 挑战页上游拒绝。

用法：python scripts/selftest_live.py
"""
import io
import json
import re
import socket
import sys
import threading
import time
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class MockHandler(BaseHTTPRequestHandler):
    routes = {}

    def _serve(self, body, status=200, ctype="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/deadsleep":
            time.sleep(3)
            self._serve(b"slow")
            return
        route = getattr(self.server, "routes", {}).get(path)
        if route is None:
            self._serve(b"not found", status=404)
            return
        body, status, ctype = route
        self._serve(body, status, ctype)

    def log_message(self, *a):  # 静默访问日志
        pass


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main() -> int:
    srv, port = serve()
    base = f"http://127.0.0.1:{port}"
    alive = lambda n: f"{base}/alive{n}".encode()  # noqa: E731

    srv.routes = {
        "/alive1": (alive(1), 200, "video/mp2t"),
        "/alive2": (alive(2), 200, "video/mp2t"),
        "/alive3": (alive(3), 200, "video/mp2t"),
        "/alive4": (alive(4), 200, "video/mp2t"),
        "/alive5": (alive(5), 200, "video/mp2t"),
        "/alive6": (alive(6), 200, "video/mp2t"),
        "/dead404": (b"gone", 404, "text/plain"),
        "/html": (b"<html>challenge page</html>", 200, "text/html"),
        "/a.m3u": (
            '#EXTM3U x-tvg-url="http://epg.test/e.xml"\n'
            '#EXTINF:-1 tvg-id="cctv1.cn" tvg-logo="http://logo/1.png" group-title="央视",CCTV-1\n'
            f"{base}/alive1\n"
            '#EXTINF:-1 group-title="卫视",湖南卫视 HD\n'
            f"{base}/dead404\n"
            '#EXTINF:-1 group-title="央视",CCTV-5+\n'
            f"{base}/alive2\n"
            '#EXTINF:-1 tvg-id="lotus.mo" user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
            ' (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36" group-title="港澳台",Lotus Macau HD\n'
            f"{base}/alive6\n".encode(), 200, "application/vnd.apple.mpegurl"),
        "/b.txt": (
            "央视,#genre#\n"
            f"CCTV-1综合,{base}/alive3\n"
            "港澳台,#genre#\n"
            f"凤凰中文,{base}/alive1#{base}/alive4\n"
            f"TVB翡翠,{base}/deadsleep\n"
            "Test,#genre#\n"
            f"Prototype,{base}/alive5\n".encode(), 200, "text/plain"),
        "/c.m3u": (b"<html>blocked</html>", 200, "text/html"),
    }

    tmp = Path(tempfile.mkdtemp(prefix="tvbox_live_selftest_"))
    out_dir = tmp / "output"
    out_dir.mkdir()
    readme = tmp / "README.md"
    readme.write_text(
        "# 测试仓库\n\n"
        "![源健康](https://old.test/shield.json) ![直播源](https://old.test/live_shield.json)\n\n"
        "<!-- LIVE-START -->\n旧内容\n<!-- LIVE-END -->\n\n尾部\n", encoding="utf-8")
    aggregate = tmp / "aggregate.json"
    aggregate.write_text(json.dumps(
        {"sites": [{"key": "demo", "name": "Demo", "type": 0, "api": "http://x"}]},
        ensure_ascii=False), encoding="utf-8")
    cfg = tmp / "lives.json"
    cfg.write_text(json.dumps({
        "epg": "http://epg.test/e.xml",
        "probe": {"workers": 8, "timeout": 1.2, "conn_retries": 0, "max_urls_per_channel": 6},
        "sources": [
            {"id": "a", "name": "假源A", "urls": [f"{base}/a.m3u"]},
            {"id": "b", "name": "假源B", "urls": [f"{base}/b.txt"]},
            {"id": "c", "name": "假源C", "urls": [f"{base}/c.m3u"]},
        ]}, ensure_ascii=False), encoding="utf-8")

    print("== live.py 全管线自测 ==")
    rc = live.main(cfg_path=cfg, out_dir=out_dir, readme_path=readme,
                   aggregate_path=aggregate, delivery="http://base.test/output")
    check("main 返回 0", rc == 0, f"rc={rc}")

    m3u = (out_dir / "live.m3u").read_text(encoding="utf-8")
    txt = (out_dir / "live.txt").read_text(encoding="utf-8")
    data = json.loads((out_dir / "live.json").read_text(encoding="utf-8"))
    status = json.loads((out_dir / "live_status.json").read_text(encoding="utf-8"))

    check("m3u 头含 EPG", m3u.startswith('#EXTM3U x-tvg-url="http://epg.test/e.xml"'), m3u[:60])
    check("m3u 含存活地址", all(f"/alive{n}" in m3u for n in (1, 2, 3, 4, 5)))
    check("m3u 剔除死链", "dead404" not in m3u and "deadsleep" not in m3u)
    check("m3u 分组与原名保留", 'group-title="央视"' in m3u and "CCTV-1综合" not in m3u
          and "CCTV-1" in m3u and "Prototype" in m3u and "湖南卫视" not in m3u)
    cctv1_line = next((l for l in txt.splitlines() if l.startswith("CCTV-1,")), "")
    check("txt 同频道多备线合并", "alive1" in cctv1_line and "alive3" in cctv1_line
          and cctv1_line.count("#") >= 1, cctv1_line)
    check("txt 分组行", "央视,#genre#" in txt and "港澳台,#genre#" in txt)
    check("txt 名称转义（逗号/type）", "Prototy\u0440e" in txt and "Prototype" not in txt)
    gmap = {g["group"]: {c["name"]: len(c["urls"]) for c in g["channels"]} for g in data}
    check("json 结构与计数", gmap.get("央视", {}).get("CCTV-1") == 2
          and gmap.get("央视", {}).get("CCTV-5+") == 1
          and gmap.get("港澳台", {}).get("凤凰中文") == 1, json.dumps(gmap, ensure_ascii=False))
    s = status["summary"]
    check("status 汇总", s["upstream_total"] == 3 and s["upstream_ok"] == 2
          and s["channels_out"] == 5 and s["urls_out"] == 6,
          json.dumps(s, ensure_ascii=False))
    check("EXTINF 属性值含逗号不污染频道名", "Lotus Macau HD" in txt and "like Gecko" not in txt
          and "Lotus Macau HD" in m3u)
    src_c = next(x for x in status["sources"] if x["id"] == "c")
    check("HTML 上游被拒", not src_c["ok"] and "HTML" in (src_c["error"] or ""),
          json.dumps(src_c, ensure_ascii=False))
    rm = readme.read_text(encoding="utf-8")
    live_seg = re.search(r"<!-- LIVE-START -->(.*?)<!-- LIVE-END -->", rm, re.S).group(1)
    check("README LIVE 段回写", "旧内容" not in live_seg and "| 假源A | ✅ |" in live_seg
          and "| 假源C | ❌ |" in live_seg and "上游 2/3 可用" in live_seg, live_seg[:200])
    check("README 徽章改写", "url=http://base.test/output/live_shield.json" in rm
          and "https://old.test/live_shield.json" not in rm)
    agg = json.loads(aggregate.read_text(encoding="utf-8"))
    lives = agg.get("lives") or []
    check("aggregate 注入 lives", len(lives) == 1 and lives[0]["url"] == "http://base.test/output/live.txt"
          and lives[0]["epg"] == "http://epg.test/e.xml" and len(agg["sites"]) == 1,
          json.dumps(lives, ensure_ascii=False))

    srv.shutdown()

    # ---- 第二轮：假源 B 整体失效 → 断言 S3.1 历史派生字段（新端口，mock 内容同步重建）
    srv2, port2 = serve()
    base2 = f"http://127.0.0.1:{port2}"
    srv2.routes = {p: (body.replace(base.encode(), base2.encode()) if isinstance(body, bytes) else body,
                       status, ctype)
                   for p, (body, status, ctype) in srv.routes.items()}
    srv2.routes["/b.txt"] = (b"gone", 404, "text/plain")
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(base, base2), encoding="utf-8")
    print("== live.py 第二轮（假源B失效）S3.1 字段自测 ==")
    rc2 = live.main(cfg_path=cfg, out_dir=out_dir, readme_path=readme,
                    aggregate_path=aggregate, delivery="http://base.test/output")
    check("第二轮 main 返回 0", rc2 == 0)
    st2 = json.loads((out_dir / "live_status.json").read_text(encoding="utf-8"))
    src_a = next(x for x in st2["sources"] if x["id"] == "a")
    src_b = next(x for x in st2["sources"] if x["id"] == "b")
    check("第二轮 假源A 连续失败 0 + 最近成功时间", src_a["ok"] and src_a["consecutive_failures"] == 0
          and bool(src_a["last_success_at"]) and len(src_a["urls_trend"]) == 2)
    check("第二轮 假源B 连续失败 1", not src_b["ok"] and src_b["consecutive_failures"] == 1
          and bool(src_b["last_failure_at"]))

    print(f"\n{'全部通过 ✅' if not FAILURES else '失败: ' + '；'.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
