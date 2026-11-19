#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TVBox 配置维护管线。

流程：读 config/sources.json → 逐源抓取（多地址回退 + 宽容解析 + 智能重试）
     → 格式解码 / 相对路径补全 / JAR 检查 → 缓存兜底 → 完整配置回退 + 多仓订阅。

设计要点（见《桌面端设计方案.md》6.3.1）：
  1. 多地址回退：每源 urls 依序尝试，首个成功即用（对抗上游域名失效）
  2. 中文域名 punycode：饭太硬.com → xn--xxx.com，否则 DNS 解析失败
  3. 配置格式：宽容 JSON / 图片 Base64 / 响应自带密钥的 2423 AES-CBC
  4. 智能重试：瞬态错误（超时/连接/5xx/空响应）退避重试；确定性失败（4xx/非JSON/结构不对）直接跳下一个地址
  5. 缓存兜底：本次全挂 → 沿用上次成功的 output/<id>.json
  6. 产物多样性：aggregate.json（单仓聚合）/ subscribe.json（多仓订阅）/ <id>.json / status.json / shield.json
  7. 状态可视化：README 状态表自动回写 + shields.io 徽章
  8. 防死循环：脚本只写 output/ 与 README.md（触发路径是 config/ scripts/，互不重叠）
  9. 内容治理：点播站点黑名单（config/sources.json 的 site_block_keywords，默认虎牙/斗鱼）
     剔除上游塞入的网络直播站；上游自带的无效 lives（相对路径/空地址/Xtream API）不进产物
 10. aggregate 选择一套完整上游（在线优先、再按 aggregate_prefer）；不混用不同 JAR 的站点

本地调试：python3 scripts/update.py
"""
import base64
import copy
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "sources.json"
OUTPUT_DIR = ROOT / "output"
README_FILE = ROOT / "README.md"
HISTORY_FILE = OUTPUT_DIR / "status_history.json"   # S3.1: 跨次运行状态历史（滚动窗口）

FETCH_TIMEOUT = 15          # 单请求超时（秒）
TRANSIENT_RETRIES = 2       # 瞬态错误重试次数（退避 2s、4s）
MAX_SUB_SOURCES = 8         # 上游若为多仓订阅格式，最多展开抓取的子源数
HISTORY_MAX_RUNS = 60       # 历史保留的运行次数（每日 2 次 ≈ 最近 30 天）
HISTORY_TREND = 10          # 延迟趋势取最近 N 次
CST = timezone(timedelta(hours=8))
DEFAULT_SITE_BLOCK = ("虎牙", "斗鱼")  # 站点黑名单默认关键词：上游常塞虎牙/斗鱼网络直播站进点播列表
CONFIG_UA = "okhttp/4.12.0"  # 配置接口常按播放器 UA 分流；浏览器 UA 会返回说明/挑战页

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "MTV-TB/2.0 (Android TV)",
    "com.catvod.toyscat/3.8",
]

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


def decode_body(r: requests.Response) -> str:
    """内容解码：不信 header 声明，先严格试 UTF-8，再试 GB18030，最后带错替换。
    上游常见"头声明 GBK、实际发 UTF-8"（或反之），直接 r.text 会出西里尔乱码。"""
    raw = r.content
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode(r.encoding or "utf-8", errors="replace")


def decode_config(raw: bytes):
    """按播放器协议解析 JSON、图片后附 Base64、2423 AES-CBC 配置。

    AES 密钥与 IV 均来自响应自身的公开封装；不执行上游脚本或 JAR。
    协议参考 FongMi/TV 的 catvod/utils/Decoder.java。
    """
    for _ in range(3):
        response = requests.Response()
        response._content = raw
        text = decode_body(response).lstrip("\ufeff\r\n\t ")
        if looks_like_html(text):
            raise ValueError(f"返回 HTML 页（说明页/挑战页/失效页, {len(raw)}B）")
        try:
            return lenient_json(text)
        except ValueError:
            pass
        if text.startswith("2423"):
            try:
                packed = bytes.fromhex(re.sub(r"\s+", "", text))
                key, encrypted = packed[2:].split(b"#$", 1)
                if not 0 < len(key) <= 16 or len(encrypted) <= 13:
                    raise ValueError("无效的密钥或载荷长度")
                iv = encrypted[-13:].lower().ljust(16, b"0")
                raw = unpad(AES.new(key.lower().ljust(16, b"0"), AES.MODE_CBC, iv)
                            .decrypt(encrypted[:-13]), AES.block_size)
            except (ValueError, TypeError) as exc:
                raise ValueError("2423 AES-CBC 配置解码失败") from exc
            continue
        match = re.search(rb"[A-Za-z0-9]{8}\*\*", raw)
        if match:
            try:
                raw = base64.b64decode(re.sub(rb"\s+", b"", raw[match.end():]), validate=True)
            except ValueError as exc:
                raise ValueError("图片/Base64 配置解码失败") from exc
            continue
        raise ValueError("配置不是有效 JSON 或支持的图片/Base64/AES-CBC 格式")
    raise ValueError("配置封装层数过多")


def normalize_config(obj: dict, base_url: str) -> dict:
    """快照移到本仓库前，按重定向后的上游地址补全资源相对路径。"""
    url_fields = {"spider", "jar", "api", "ext", "url", "wallpaper", "logo", "icon"}

    def visit(value, field=""):
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v, field) for v in value]
        if isinstance(value, str) and (value.startswith(("./", "../", "//")) or
                                      field in url_fields and value.startswith(("/", "?"))):
            return urljoin(base_url, value)
        return value

    return visit(obj)


def check_spider(session: requests.Session, obj: dict, headers=None):
    """只读 JAR 文件头，拦截配置 200 但爬虫已 403/404/返回 HTML 的情况。"""
    spider = obj.get("spider")
    if not isinstance(spider, str) or not spider:
        return None
    url = spider.split(";", 1)[0]
    if not url.startswith(("http://", "https://")):
        return "爬虫依赖不是可读取的 HTTP(S) 地址"
    try:
        with session.get(punycode_url(url), timeout=(10, 15), stream=True,
                         headers={"User-Agent": CONFIG_UA, **(headers or {})}) as response:
            if response.status_code >= 400:
                return f"爬虫依赖 HTTP {response.status_code}"
            # 压缩/分块响应的一次 raw.read(4) 可能不足 4 字节，累计解码后的数据再判断。
            prefix = b""
            for chunk in response.iter_content(chunk_size=512):
                prefix += chunk
                if len(prefix) >= 4:
                    break
            if not prefix.startswith(b"PK\x03\x04"):
                return "爬虫依赖未返回 ZIP/JAR 文件头"
    except requests.RequestException as exc:
        return f"爬虫依赖 {type(exc).__name__}"
    return None


# ---------------------------------------------------------------- 状态历史（S3.1）

def history_load(path: Path) -> dict:
    try:
        return lenient_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  首次运行/文件损坏都从空历史开始
        return {"runs": []}


def history_append(path: Path, states: dict) -> dict:
    """追加一次运行快照 {sid: {ok,cached,latency_ms}} 并裁剪窗口，返回更新后的历史。"""
    hist = history_load(path)
    hist.setdefault("runs", []).append({
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "sources": states,
    })
    hist["runs"] = hist["runs"][-HISTORY_MAX_RUNS:]
    write_json(path, hist)
    return hist


def history_derive(hist: dict, sid: str, field: str = "latency_ms") -> dict:
    """从历史派生单源健康字段：连续失败次数、最近成功/失败时间、数值趋势（field 可换，如 urls）。"""
    runs = hist.get("runs", [])
    entries = [r.get("sources", {}).get(sid) for r in runs]
    entries = [e for e in entries if isinstance(e, dict)]
    cf = 0
    for e in reversed(entries):
        if e.get("ok"):
            break
        cf += 1
    last_ok = next((r["ts"] for r in reversed(runs)
                    if isinstance(r.get("sources", {}).get(sid), dict) and r["sources"][sid].get("ok")), None)
    last_fail = next((r["ts"] for r in reversed(runs)
                      if isinstance(r.get("sources", {}).get(sid), dict) and not r["sources"][sid].get("ok")), None)
    trend = [e[field] for e in entries[-HISTORY_TREND:] if isinstance(e.get(field), int)]
    return {"consecutive_failures": cf, "last_success_at": last_ok,
            "last_failure_at": last_fail, "trend": trend}


def relative_hours(ts) -> str:
    """'x小时前' 供徽章/看板用；解析失败返回原文。"""
    if not ts:
        return "首次运行"
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        minutes = int((datetime.now(CST) - dt).total_seconds() // 60)
        if minutes < 1:
            return "刚刚"
        if minutes < 60:
            return f"{minutes}分钟前"
        if minutes < 60 * 48:
            return f"{minutes // 60}小时前"
        return f"{minutes // (60 * 24)}天前"
    except Exception:  # noqa: BLE001
        return ts


# ---------------------------------------------------------------- 抓取

def fetch_single(session: requests.Session, url: str, headers=None):
    """抓取单个地址。返回 (obj 或 None, 错误说明 或 None)。"""
    pu = punycode_url(url)
    last_err = "unknown"
    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            r = session.get(pu, timeout=FETCH_TIMEOUT, allow_redirects=True,
                            headers={"User-Agent": CONFIG_UA, **(headers or {})})
        except requests.exceptions.SSLError:
            return None, "SSLError（TLS 证书校验失败）"
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f"{type(e).__name__}"
            if attempt < TRANSIENT_RETRIES:
                time.sleep(2 * (attempt + 1))
            continue
        except Exception as e:  # 其它异常按确定性处理，重试无意义
            return None, f"{type(e).__name__}: {e}"
        if r.status_code >= 500:  # 服务端错误 → 瞬态，退避重试
            last_err = f"HTTP {r.status_code}"
            if attempt < TRANSIENT_RETRIES:
                time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:  # 4xx → 确定性失败，直接跳下一个地址
            return None, f"HTTP {r.status_code}"
        if not r.content.strip():
            last_err = "空响应"
            if attempt < TRANSIENT_RETRIES:
                time.sleep(2 * (attempt + 1))
            continue
        try:
            obj = decode_config(r.content)
        except ValueError as e:
            return None, str(e)
        if not is_tvbox_config(obj):
            return None, "结构不符：缺非空 sites/urls"
        return normalize_config(obj, r.url), None
    return None, last_err


def fetch_source(session: requests.Session, source: dict):
    """逐地址回退抓取一个上游源。返回 (used_url, obj, error_summary)。"""
    errors = []
    for url in source.get("urls", []):
        obj, err = fetch_single(session, url, source.get("headers"))
        if obj is not None:
            err = check_spider(session, obj, source.get("headers"))
            if not err:
                return url, obj, None
        errors.append(f"{url.split('://')[-1][:50]}: {err}")
        log(f"    × {errors[-1]}")
    return None, None, "; ".join(errors)


def expand_subscription(session: requests.Session, obj: dict, headers=None):
    """上游为多仓订阅格式（{"urls":[{name,url}]}）时，展开抓取其子源并返回站点素材。"""
    subs = [s for s in obj.get("urls", []) if isinstance(s, dict) and s.get("url")]
    materials = []
    for sub in subs[:MAX_SUB_SOURCES]:
        sub_obj, err = fetch_single(session, sub["url"], headers)
        if sub_obj and sub_obj.get("sites"):
            err = check_spider(session, sub_obj, headers)
        if sub_obj and sub_obj.get("sites") and not err:
            materials.append(sub_obj)
        else:
            log(f"    · 子源 {sub.get('name','?')} 跳过（{err or '格式不符'}）")
    return materials


# ---------------------------------------------------------------- 合并

def site_identity(site: dict):
    """站点去重键：key+api 组合。"""
    return (str(site.get("key", "")), str(site.get("api", "")))


def valid_live_entry(x: dict) -> bool:
    """直播条目结构可用性：文本直播管线（type 0）要求名称 + 绝对 http(s) 地址。
    上游点播配置自带的 lives 常见相对路径（./lives/x.txt，本仓库并不发布该文件）、
    空地址、Xtream player_api.php（JSON 用户接口，文本解析不出频道），一律剔除。"""
    url = str(x.get("url") or "").strip()
    if not str(x.get("name") or "").strip():
        return False
    if not url.lower().startswith(("http://", "https://")):
        return False
    return "player_api.php" not in url.lower()


def merge_configs(configs: list, block_keywords=()):
    """使用首个完整配置，保留其 JAR/rules/ads 等上下文；其余源由多仓切换。

    csp_* 类名可能相同但来自不同 JAR，不能把站点混合后共用一个 JAR。
    """
    primary = next((cfg for cfg in configs if cfg.get("sites")), {})
    seen_ident, seen_key = set(), set()
    merged_sites, blocked_sites = [], 0
    for site in primary.get("sites", []):
        if not isinstance(site, dict):
            continue
        if any(kw and kw in str(site.get("name", "")) for kw in block_keywords):
            blocked_sites += 1
            continue
        ident = site_identity(site)
        if ident in seen_ident:
            continue
        seen_ident.add(ident)
        site = copy.deepcopy(site)
        key = str(site.get("key", f"site_{len(merged_sites)}"))
        if key in seen_key:
            n = 2
            while f"{key}_{n}" in seen_key:
                n += 1
            site = {**site, "key": f"{key}_{n}"}
            key = site["key"]
        seen_key.add(key)
        merged_sites.append(site)
    if blocked_sites:
        log(f"    站点黑名单剔除 {blocked_sites} 个（关键词：{'、'.join(k for k in block_keywords if k)}）")

    all_lives = primary.get("lives") or []
    lives = [x for x in all_lives if isinstance(x, dict) and valid_live_entry(x)]
    dropped_lives = len(all_lives) - len(lives)
    if dropped_lives:
        log(f"    直播配置剔除 {dropped_lives} 条无效项（相对路径/空地址/Xtream API）")
    agg = copy.deepcopy(primary)
    agg["sites"] = merged_sites
    if lives:
        agg["lives"] = lives
    else:
        agg.pop("lives", None)
    return agg


# ---------------------------------------------------------------- 产物

def count_sites(obj) -> int:
    if not isinstance(obj, dict):
        return 0
    return len(obj.get("sites") or [])


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def build_readme_table(status_list: list, repo: str, ok_n: int, total: int) -> str:
    lines = [
        f"> 自动更新于 {datetime.now(CST):%Y-%m-%d %H:%M}（{ok_n}/{total} 源通过配置及 JAR 检查，未验证逐站播放）。"
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
    """README 顶部徽章指向本平台的 shield.json / update_shield.json。"""
    if not README_FILE.exists():
        return
    text = README_FILE.read_text(encoding="utf-8")
    new = f"![源健康](https://img.shields.io/endpoint?url={base}/shield.json)"
    out = re.sub(r"!\[源健康\]\([^)]*\)", new, text)  # 全部替换：防双平台 CI 交替改写产生的重复徽章
    upd = f"![更新](https://img.shields.io/endpoint?url={base}/update_shield.json)"
    out = re.sub(r"!\[更新\]\([^)]*\)", upd, out)
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
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    disabled = [{"id": s["id"], "name": s["name"], "reason": s.get("disabled_reason", "人工停用")}
                for s in cfg.get("sources", []) if not s.get("enabled", True)]
    repo = resolve_repo(cfg)
    OUTPUT_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    log(f"开始聚合：{len(sources)} 个上游源（repo={repo}）")

    status_list, materials = [], []
    for source in sources:
        sid, name = source["id"], source["name"]
        log(f"▶ {name} ({sid})")
        t0 = time.time()
        used_url, obj, err = fetch_source(session, source)
        cached = False
        site_objs = []
        if obj is not None:
            if obj.get("sites"):
                site_objs = [obj]
            else:
                site_objs = expand_subscription(session, obj, source.get("headers"))
            if not site_objs:
                used_url, obj, err = None, None, "订阅可读取，但没有通过检查的子源"
        if obj is not None:
            # 只有产生可用站点后才覆盖缓存，避免空订阅覆盖上次的配置
            write_json(OUTPUT_DIR / f"{sid}.json", obj)
        else:
            # 本次全挂 → 缓存兜底：沿用上次成功的 <id>.json
            cache_file = OUTPUT_DIR / f"{sid}.json"
            if cache_file.exists():
                try:
                    obj = lenient_json(cache_file.read_text(encoding="utf-8"))
                    if is_tvbox_config(obj):
                        site_objs = ([obj] if obj.get("sites") else
                                     expand_subscription(session, obj, source.get("headers")))
                    cached = bool(site_objs)
                    if cached:
                        log(f"    📦 沿用缓存（{sum(count_sites(o) for o in site_objs)} 站点）")
                except Exception:
                    obj = None
        latency = int((time.time() - t0) * 1000)
        materials.extend({"id": sid, "name": name, "cached": cached, "config": o} for o in site_objs)
        total_sites = sum(count_sites(o) for o in site_objs)

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

    # ---- S3.1: 状态历史 → 连续失败/最近成功/延迟趋势
    states = {s["id"]: {"ok": s["ok"], "cached": s["cached"], "latency_ms": s["latency_ms"]}
              for s in status_list}
    hist_prev = history_load(HISTORY_FILE)
    prev_run_ts = hist_prev["runs"][-1]["ts"] if hist_prev.get("runs") else None
    hist = history_append(HISTORY_FILE, states)
    for s in status_list:
        d = history_derive(hist, s["id"])
        s["consecutive_failures"] = d["consecutive_failures"]
        s["last_success_at"] = d["last_success_at"]
        s["last_failure_at"] = d["last_failure_at"]
        s["latency_trend_ms"] = d["trend"]

    # ---- 产物 1+2: 聚合配置 aggregate / 多仓订阅 subscribe（ASCII 文件名，避免中文路径编码歧义）
    usable = [s for s in status_list if s["ok"] or s["cached"]]
    aggregate_status = None
    if materials:
        preferred = {sid: i for i, sid in enumerate(cfg.get("aggregate_prefer", []))}
        materials.sort(key=lambda item: (item["cached"], preferred.get(item["id"], len(preferred))))
        selected = materials[0]
        block_kw = cfg.get("site_block_keywords") or list(DEFAULT_SITE_BLOCK)
        aggregate = merge_configs([selected["config"]], block_kw)
        if (OUTPUT_DIR / "live.txt").exists():
            # 单独刷新点播时也保留本仓库直播入口；live.py 随后会再次注入并更新 EPG。
            live_url = f"{delivery_base(repo)}/live.txt"
            entry = {"name": "自建直播·自动保活", "type": 0, "url": live_url}
            live_cfg_file = ROOT / "config" / "lives.json"
            if live_cfg_file.exists():
                epg = lenient_json(live_cfg_file.read_text(encoding="utf-8")).get("epg")
                if epg:
                    entry["epg"] = epg
            aggregate["lives"] = [entry] + [x for x in aggregate.get("lives", []) if x.get("url") != live_url]
        write_json(OUTPUT_DIR / "aggregate.json", aggregate)
        aggregate_status = {"source_id": selected["id"], "source_name": selected["name"],
                            "cached": selected["cached"], "sites": count_sites(aggregate),
                            "policy": "complete_config_fallback"}
        log(f"    默认入口：{selected['name']}，保留其完整 JAR/规则，{count_sites(aggregate)} 站点")
    sub_urls = [{
        "name": s["name"],
        "url": f"{delivery_base(repo)}/{s['id']}.json",
    } for s in usable]
    write_json(OUTPUT_DIR / "subscribe.json", {"urls": sub_urls})

    # ---- 产物 4: status.json（机器可读，App/README 消费；S3.1 含历史派生字段）
    ok_n = sum(1 for s in status_list if s["ok"])
    write_json(OUTPUT_DIR / "status.json", {
        "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "repo": repo,
        "summary": {"total": len(status_list), "ok": ok_n,
                    "cached": sum(1 for s in status_list if s["cached"]),
                    "disabled": len(disabled),
                    "runs_tracked": len(hist["runs"])},
        "validation": "config_and_spider_header_only; playback_not_tested",
        "aggregate": aggregate_status,
        "disabled_sources": disabled,
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
    write_json(OUTPUT_DIR / "update_shield.json", {
        "schemaVersion": 1, "label": "更新",
        "message": relative_hours(prev_run_ts),
        "color": "blue",
    })

    # ---- 产物 7: README 状态表回写 + 徽章指向本平台
    rewrite_readme(build_readme_table(status_list, repo, ok_n, len(status_list)))
    rewrite_badge(delivery_base(repo))

    log(f"完成：{ok_n} 在线 / {sum(1 for s in status_list if s['cached'])} 缓存兜底 / "
        f"{sum(1 for s in status_list if not s['ok'] and not s['cached'])} 不可用；"
        f"默认入口 {aggregate_status['source_id'] if aggregate_status else '保留旧产物'}")
    if ok_n == 0 and not any(s["cached"] for s in status_list):
        log("警告：所有源均不可用且无缓存，聚合结果不可用")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
