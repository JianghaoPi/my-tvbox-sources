#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TVBox 源仓库自动聚合管线（S1）。

流程：读 config/sources.json → 逐源抓取（多地址回退 + 宽容解析 + 智能重试）
     → 缓存兜底 → 合并去重 → 写 output/ 五类产物 → 回写 README 状态表。

设计要点（见《桌面端设计方案.md》6.3.1）：
  1. 多地址回退：每源 urls 依序尝试，首个成功即用（对抗上游域名失效）
  2. 中文域名 punycode：饭太硬.com → xn--xxx.com，否则 DNS 解析失败
  3. 宽容 JSON：去 BOM / 剔注释 / strict=False / 截取首尾大括号
  4. 智能重试：瞬态错误（超时/连接/5xx/空响应）退避重试；确定性失败（4xx/非JSON/结构不对）直接跳下一个地址
  5. 缓存兜底：本次全挂 → 沿用上次成功的 output/<id>.json
  6. 产物多样性：单仓聚合.json / 多仓订阅.json / <id>.json / status.json / shield.json
  7. 状态可视化：README 状态表自动回写 + shields.io 徽章
  8. 防死循环：脚本只写 output/ 与 README.md（触发路径是 config/ scripts/，互不重叠）

本地调试：python3 scripts/update.py
"""
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, quote

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "sources.json"
OUTPUT_DIR = ROOT / "output"
README_FILE = ROOT / "README.md"

FETCH_TIMEOUT = 15          # 单请求超时（秒）
TRANSIENT_RETRIES = 2       # 瞬态错误重试次数（退避 2s、4s）
MAX_SUB_SOURCES = 8         # 上游若为多仓订阅格式，最多展开抓取的子源数
CST = timezone(timedelta(hours=8))

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "MTV-TB/2.0 (Android TV)",
    "com.catvod.toyscat/3.8",
]

import random
random.seed()  # 各次运行 UA 分布不同，避免被上游按 UA 规律识别


def log(msg: str):
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 工具函数

def punycode_url(url: str) -> str:
    """中文域名转 punycode（仅处理 host），非 ASCII 路径做百分号编码。"""
    p = urlsplit(url)
    host = p.hostname or ""
    try:
        port = f":{p.port}" if p.port else ""
    except ValueError:
        port = ""
    host_pc = ".".join(
        lab if lab.isascii() else "xn--" + lab.encode("punycode").decode("ascii")
        for lab in host.split(".")
    )
    netloc = (host_pc + port).lower()
    path = quote(p.path, safe="/%:@&=+$,~*!()'-._;")
    return urlunsplit((p.scheme or "http", netloc, path, p.query, ""))


def lenient_json(text: str):
    """宽容 JSON 解析：去 BOM、剔注释、strict=False、截取首尾大括号后重试。"""
    t = text.lstrip("\ufeff\r\n\t ").strip()
    attempts = [t]
    # 剔 // 行注释与 /* */ 块注释（仅在直接解析失败后才尝试，避免误伤字符串内容）
    no_line = re.sub(r"(?m)^\s*//.*$", "", t)
    no_block = re.sub(r"/\*.*?\*/", "", no_line, flags=re.S)
    attempts.append(no_block)
    # 截取首个 { 到最后一个 }（兼容前后夹杂日志文本的返回）
    for s in list(attempts):
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            attempts.append(s[i:j + 1])
    last_err = None
    for s in attempts:
        try:
            return json.loads(s, strict=False)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ValueError(f"JSON 解析失败: {last_err}")


def is_tvbox_config(obj) -> bool:
    """结构校验：必须是含非空 sites（点播配置）或非空 urls（多仓订阅）的 dict。"""
    if not isinstance(obj, dict):
        return False
    sites, urls = obj.get("sites"), obj.get("urls")
    return (isinstance(sites, list) and len(sites) > 0) or \
           (isinstance(urls, list) and len(urls) > 0)


def looks_like_html(text: str) -> bool:
    # 只剥 BOM 与空白：HTML/XML 响应必然以 '<' 开头（DOCTYPE/html/head/xml），一律视为垃圾页
    head = text.lstrip("\ufeff \t\r\n")
    return head.startswith("<")


# ---------------------------------------------------------------- 抓取

def fetch_single(session: requests.Session, url: str):
    """抓取单个地址。返回 (obj 或 None, 错误说明 或 None)。"""
    pu = punycode_url(url)
    last_err = "unknown"
    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            r = session.get(pu, timeout=FETCH_TIMEOUT, allow_redirects=True,
                            headers={"User-Agent": random.choice(UA_POOL)})
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f"{type(e).__name__}"
            time.sleep(2 * (attempt + 1))
            continue
        except Exception as e:  # 其它异常按确定性处理，重试无意义
            return None, f"{type(e).__name__}: {e}"
        if r.status_code >= 500:  # 服务端错误 → 瞬态，退避重试
            last_err = f"HTTP {r.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:  # 4xx → 确定性失败，直接跳下一个地址
            return None, f"HTTP {r.status_code}"
        text = r.text or ""
        if not text.strip():
            last_err = "空响应"
            time.sleep(2 * (attempt + 1))
            continue
        if looks_like_html(text):  # 挑战页/防爬页/域名停放页，重试无意义
            return None, f"返回 HTML 页（挑战页或失效页, {len(text)}B）"
        try:
            obj = lenient_json(text)
        except ValueError as e:
            return None, str(e) + f"（首部: {text[:60]!r}）"
        if not is_tvbox_config(obj):
            return None, "结构不符：缺非空 sites/urls"
        return obj, None
    return None, last_err


def fetch_source(session: requests.Session, source: dict):
    """逐地址回退抓取一个上游源。返回 (used_url, obj, error_summary)。"""
    errors = []
    for url in source.get("urls", []):
        obj, err = fetch_single(session, url)
        if obj is not None:
            return url, obj, None
        errors.append(f"{url.split('://')[-1][:50]}: {err}")
        log(f"    × {errors[-1]}")
    return None, None, "; ".join(errors)


def expand_subscription(session: requests.Session, obj: dict):
    """上游为多仓订阅格式（{"urls":[{name,url}]}）时，展开抓取其子源并返回站点素材。"""
    subs = [s for s in obj.get("urls", []) if isinstance(s, dict) and s.get("url")]
    materials = []
    for sub in subs[:MAX_SUB_SOURCES]:
        sub_obj, err = None, None
        sub_url = punycode_url(sub["url"])
        # 子源失败只记一次，不回退重试整套（子源通常只有一个地址）
        try:
            r = session.get(sub_url, timeout=FETCH_TIMEOUT, allow_redirects=True,
                            headers={"User-Agent": random.choice(UA_POOL)})
            if r.status_code < 400 and not looks_like_html(r.text or ""):
                parsed = lenient_json(r.text or "")
                if isinstance(parsed, dict) and isinstance(parsed.get("sites"), list):
                    sub_obj = parsed
            elif r.status_code >= 400:
                err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
        if sub_obj:
            materials.append(sub_obj)
        else:
            log(f"    · 子源 {sub.get('name','?')} 跳过（{err or '格式不符'}）")
    return materials


# ---------------------------------------------------------------- 合并

def site_identity(site: dict):
    """站点去重键：key+api 组合。"""
    return (str(site.get("key", "")), str(site.get("api", "")))


def merge_configs(configs: list):
    """把多份配置合并为单仓聚合：sites 按 (key,api) 去重、key 冲突加后缀；
    lives/parses 按 name+url 去重；flags 并集；spider 取出现最多的。"""
    seen_ident, seen_key = set(), Counter()
    merged_sites = []
    for cfg in configs:
        for site in cfg.get("sites", []):
            if not isinstance(site, dict):
                continue
            ident = site_identity(site)
            if ident in seen_ident:
                continue
            seen_ident.add(ident)
            key = str(site.get("key", f"site_{len(merged_sites)}"))
            if seen_key[key]:  # key 冲突 → 加数字后缀（如 抖音_2）
                n = seen_key[key] + 1
                while f"{key}_{n}" in seen_key:
                    n += 1
                site = {**site, "key": f"{key}_{n}"}
                key = site["key"]
            seen_key[key] += 1
            merged_sites.append(site)

    def dedup_list(field, idfunc):
        out, seen = [], set()
        for cfg in configs:
            for item in cfg.get(field, []) or []:
                if not isinstance(item, dict):
                    continue
                k = idfunc(item)
                if k in seen:
                    continue
                seen.add(k)
                out.append(item)
        return out

    lives = dedup_list("lives", lambda x: (str(x.get("name", "")), str(x.get("url", ""))))
    parses = dedup_list("parses", lambda x: (str(x.get("name", "")), str(x.get("url", ""))))
    flags = sorted({f for cfg in configs for f in (cfg.get("flags") or []) if isinstance(f, str)})
    spiders = Counter(str(cfg["spider"]) for cfg in configs if cfg.get("spider"))
    agg = {"sites": merged_sites}
    if spiders:
        agg["spider"] = spiders.most_common(1)[0][0]
    if lives:
        agg["lives"] = lives
    if parses:
        agg["parses"] = parses
    if flags:
        agg["flags"] = flags
    agg["ads"] = dedup_list("ads", lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
    return {k: v for k, v in agg.items() if v}


# ---------------------------------------------------------------- 产物

def count_sites(obj) -> int:
    if not isinstance(obj, dict):
        return 0
    return len(obj.get("sites") or [])


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def build_readme_table(status_list: list, repo: str, ok_n: int, total: int) -> str:
    lines = [
        f"> 自动更新于 {datetime.now(CST):%Y-%m-%d %H:%M}（{ok_n}/{total} 源可用）。"
        f" 手动触发：Actions → update → Run workflow。",
        "",
        "| 源 | 状态 | 生效地址 | 站点 | 错误 |",
        "|---|---|---|---|---|",
    ]
    for s in status_list:
        addr = s["used_url"] or "-"
        if len(addr) > 60:
            addr = addr[:57] + "..."
        err = (s.get("error") or "-").replace("|", "\\|")
        if len(err) > 80:
            err = err[:77] + "..."
        state = "✅" if s["ok"] else ("📦 缓存兜底" if s.get("cached") else "❌")
        lines.append(f"| {s['name']} | {state} | {addr} | {s.get('sites', 0)} | {err} |")
    return "\n".join(lines)


def rewrite_readme(table_md: str):
    """回写 README 中 STATUS-START/END 标记之间的状态表。"""
    if not README_FILE.exists():
        return
    text = README_FILE.read_text(encoding="utf-8")
    pattern = re.compile(r"(<!-- STATUS-START -->).*?(<!-- STATUS-END -->)", re.S)
    if not pattern.search(text):
        log("README 缺少 STATUS 标记，跳过状态表回写")
        return
    README_FILE.write_text(
        pattern.sub(lambda m: m.group(1) + "\n" + table_md + "\n" + m.group(2), text),
        encoding="utf-8")


def rewrite_badge(base: str):
    """README 顶部徽章指向本平台的 shield.json。"""
    if not README_FILE.exists():
        return
    text = README_FILE.read_text(encoding="utf-8")
    new = f"![源健康](https://img.shields.io/endpoint?url={base}/shield.json)"
    out = re.sub(r"!\[源健康\]\([^)]*\)", new, text, count=1)
    if out != text:
        README_FILE.write_text(out, encoding="utf-8")


def resolve_repo(cfg: dict) -> str:
    """仓库 slug：优先 CI 环境变量，其次 git remote，最后配置文件占位。"""
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    if os.environ.get("CNB") == "true":
        # CNB 内置变量（docs.cnb.cool 默认环境变量）：格式 group_slug/repo_name
        if os.environ.get("CNB_REPO_SLUG"):
            return os.environ["CNB_REPO_SLUG"]
        import subprocess
        try:
            url = subprocess.run(["git", "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            m = re.search(r"cnb\.cool[:/](.+?)(\.git)?$", url)
            if m:
                return m.group(1)
        except Exception:
            pass
    return cfg.get("repo") or "YOUR_GITHUB/my-tvbox-sources"


def delivery_base(repo: str) -> str:
    """本平台产物的匿名 raw 访问前缀（多仓订阅.json 内各源地址的基座）。"""
    if os.environ.get("CNB") == "true":
        return f"https://cnb.cool/{repo}/-/git/raw/main/output"
    return f"https://raw.githubusercontent.com/{repo}/main/output"


# ---------------------------------------------------------------- 主流程

def main() -> int:
    cfg = lenient_json(CONFIG_FILE.read_text(encoding="utf-8"))
    sources = cfg.get("sources", [])
    repo = resolve_repo(cfg)
    OUTPUT_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    log(f"开始聚合：{len(sources)} 个上游源（repo={repo}）")

    status_list, agg_configs = [], []
    for source in sources:
        sid, name = source["id"], source["name"]
        log(f"▶ {name} ({sid})")
        t0 = time.time()
        used_url, obj, err = fetch_source(session, source)
        cached = False
        if obj is not None:
            # 本次成功 → 落盘为该源的最新缓存
            write_json(OUTPUT_DIR / f"{sid}.json", obj)
        else:
            # 本次全挂 → 缓存兜底：沿用上次成功的 <id>.json
            cache_file = OUTPUT_DIR / f"{sid}.json"
            if cache_file.exists():
                try:
                    obj = lenient_json(cache_file.read_text(encoding="utf-8"))
                    cached = True
                    log(f"    📦 沿用缓存（{count_sites(obj)} 站点）")
                except Exception:
                    obj = None
        latency = int((time.time() - t0) * 1000)

        # 多仓订阅格式 → 展开子源取素材（缓存命中的订阅格式同样展开）
        site_objs = []
        if obj is not None:
            if isinstance(obj.get("urls"), list) and not obj.get("sites"):
                log("    多仓订阅格式 → 展开子源")
                site_objs = expand_subscription(session, obj)
            else:
                site_objs = [obj]
            agg_configs.extend(site_objs)
            total_sites = sum(count_sites(o) for o in site_objs)
        else:
            total_sites = 0

        status_list.append({
            "id": sid, "name": name,
            "ok": used_url is not None,
            "cached": cached,
            "used_url": used_url,
            "error": err,
            "sites": total_sites,
            "latency_ms": latency,
        })
        log(f"    结果: ok={used_url is not None} cached={cached} sites={total_sites} {latency}ms")

    # ---- 产物 1+2: 单仓聚合 / 多仓订阅
    usable = [s for s in status_list if s["ok"] or s["cached"]]
    if agg_configs:
        write_json(OUTPUT_DIR / "单仓聚合.json", merge_configs(agg_configs))
    sub_urls = [{
        "name": s["name"],
        "url": f"{delivery_base(repo)}/{s['id']}.json",
    } for s in usable]
    write_json(OUTPUT_DIR / "多仓订阅.json", {"urls": sub_urls})

    # ---- 产物 4: status.json（机器可读，App/README 消费）
    ok_n = sum(1 for s in status_list if s["ok"])
    write_json(OUTPUT_DIR / "status.json", {
        "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "repo": repo,
        "summary": {"total": len(status_list), "ok": ok_n,
                    "cached": sum(1 for s in status_list if s["cached"])},
        "sources": status_list,
    })

    # ---- 产物 5: shield.json（README 徽章，shields.io endpoint 格式）
    ratio = ok_n / len(status_list) if status_list else 0
    color = "brightgreen" if ratio >= 0.9 else "yellowgreen" if ratio >= 0.5 else \
            "orange" if ratio >= 0.3 else "red"
    write_json(OUTPUT_DIR / "shield.json", {
        "schemaVersion": 1, "label": "源健康",
        "message": f"{ok_n}/{len(status_list)} ok", "color": color,
    })

    # ---- 产物 7: README 状态表回写 + 徽章指向本平台
    rewrite_readme(build_readme_table(status_list, repo, ok_n, len(status_list)))
    rewrite_badge(delivery_base(repo))

    log(f"完成：{ok_n} 在线 / {sum(1 for s in status_list if s['cached'])} 缓存兜底 / "
        f"{sum(1 for s in status_list if not s['ok'] and not s['cached'])} 不可用；"
        f"聚合站点 {sum(count_sites(o) for o in agg_configs)}（去重前）")
    if ok_n == 0 and not any(s["cached"] for s in status_list):
        log("警告：所有源均不可用且无缓存，聚合结果不可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
