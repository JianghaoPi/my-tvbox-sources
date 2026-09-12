#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update.py S3.1 状态历史自测：本地 mock 三个运行周期，断言历史派生字段。

场景：good 源始终 200；flaky 源前两轮 404（确定性失败不重试）、第三轮恢复 200。
断言：consecutive_failures 累计与清零、last_success_at/last_failure_at、延迟趋势长度、
update_shield 相对时间、README 表格与徽章回写、历史窗口文件结构。

用法：python scripts/selftest_status.py
"""
import json
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class MockHandler(BaseHTTPRequestHandler):
    state = {"flaky_404_left": 2}

    def do_GET(self):
        if self.path == "/good.json":
            body = json.dumps({"sites": [{"key": "g", "name": "G", "type": 0, "api": "http://g.example/api"}]}).encode()
            self.send_response(200)
        elif self.path == "/flaky.json":
            if self.state["flaky_404_left"] > 0:
                self.state["flaky_404_left"] -= 1
                body = b"gone"
                self.send_response(404)
            else:
                body = json.dumps({"sites": [{"key": "f", "name": "F", "type": 0, "api": "http://f.example/api"}]}).encode()
                self.send_response(200)
        else:
            body, _ = b"nope", self.send_response(404)
            self.wfile.write(body)
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def source(sid, status):
    return next(s for s in status["sources"] if s["id"] == sid)


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    tmp = Path(tempfile.mkdtemp(prefix="tvbox_status_selftest_"))
    out = tmp / "output"
    out.mkdir()
    update.OUTPUT_DIR = out
    update.HISTORY_FILE = out / "status_history.json"
    update.CONFIG_FILE = tmp / "sources.json"
    update.CONFIG_FILE.write_text(json.dumps({
        "repo": "test/repo",
        "sources": [
            {"id": "good", "name": "好源", "urls": [f"{base}/good.json"]},
            {"id": "flaky", "name": "波动源", "urls": [f"{base}/flaky.json"]},
        ]}, ensure_ascii=False), encoding="utf-8")
    update.README_FILE = tmp / "README.md"
    update.README_FILE.write_text(
        "# 测试\n\n![源健康](https://old/shield.json) ![更新](https://old/update_shield.json)\n\n"
        "<!-- STATUS-START -->\n旧\n<!-- STATUS-END -->\n", encoding="utf-8")

    print("== update.py S3.1 状态历史自测 ==")

    # 运行 1：flaky 404
    update.main()
    st = json.loads((out / "status.json").read_text(encoding="utf-8"))
    g, f = source("good", st), source("flaky", st)
    check("运行1 good ok+首次成功时间", g["ok"] and bool(g["last_success_at"]) and g["consecutive_failures"] == 0)
    check("运行1 good 趋势长度 1", len(g["latency_trend_ms"]) == 1)
    check("运行1 flaky 连续失败 1", not f["ok"] and f["consecutive_failures"] == 1
          and bool(f["last_failure_at"]) and f["last_success_at"] is None)
    check("运行1 徽章=首次运行", json.loads((out / "update_shield.json").read_text(encoding="utf-8"))["message"] == "首次运行")
    check("运行1 历史窗口 1 条", st["summary"]["runs_tracked"] == 1)

    # 运行 2：flaky 再 404
    update.main()
    st = json.loads((out / "status.json").read_text(encoding="utf-8"))
    g, f = source("good", st), source("flaky", st)
    check("运行2 flaky 连续失败 2", f["consecutive_failures"] == 2)
    check("运行2 good 趋势长度 2", len(g["latency_trend_ms"]) == 2)
    check("运行2 徽章=相对时间", "刚刚" in json.loads((out / "update_shield.json").read_text(encoding="utf-8"))["message"]
          or "分钟前" in json.loads((out / "update_shield.json").read_text(encoding="utf-8"))["message"])
    check("运行2 历史 runs=2", st["summary"]["runs_tracked"] == 2)

    # 运行 3：flaky 恢复
    update.main()
    st = json.loads((out / "status.json").read_text(encoding="utf-8"))
    g, f = source("good", st), source("flaky", st)
    check("运行3 flaky 恢复：连续失败清零", f["ok"] and f["consecutive_failures"] == 0
          and bool(f["last_success_at"]))
    check("运行3 flaky 保留上次失败时间", bool(f["last_failure_at"]) and f["last_failure_at"] <= f["last_success_at"])
    hist = json.loads(update.HISTORY_FILE.read_text(encoding="utf-8"))
    check("历史文件结构", len(hist["runs"]) == 3
          and set(hist["runs"][0]["sources"]) == {"good", "flaky"})

    rm = update.README_FILE.read_text(encoding="utf-8")
    seg = re.search(r"<!-- STATUS-START -->(.*?)<!-- STATUS-END -->", rm, re.S).group(1)
    check("README 表格与徽章", "旧" not in seg and "| 好源 | ✅ |" in seg and "| 波动源 | ✅ |" in seg
          and "![更新](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/test/repo/main/output/update_shield.json)" in rm
          and "![源健康](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/test/repo/main/output/shield.json)" in rm)

    srv.shutdown()
    print(f"\n{'全部通过 ✅' if not FAILURES else '失败: ' + '；'.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
