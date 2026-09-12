#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TVBox 直播源保活管线（S2）。

流程：读 config/lives.json → 逐上游抓取（多地址回退，复用 update.py 工具）
     → 解析 m3u/txt/json → 频道名规范化合并去重（频道名+URL）
     → 并发测速（HEAD → 失败降级 GET 首包）筛掉失效地址 → 生成 live.m3u / live.txt / live.json
     → live_status.json / live_shield.json → README 直播状态表 → aggregate.json 注入 lives。

设计要点（承接《桌面端设计方案.md》6.3.2 / S2 任务清单）：
  1. 上游素材：fanmingming/live、iptv-org/iptv、Guovin/iptv-api，每源主+备多地址（实测可达的镜像排前面）
  2. 合并去重：频道名规范化（NFKC/繁转简/去画质后缀/CCTV 编号归一）后按规范名合并多 URL；
     URL 全局去重（先到先得，上游优先级 = 配置顺序）
  3. 测速过滤（S2.3）：并发 HEAD → 失败降级 GET 读首包；超时/4xx/5xx/空响应判死；
     workers/timeout/conn_retries/max_latency_ms 全部在 lives.json 可调
  4. 有效地址按延迟升序排列（txt 的 # 多备线 = 自动切换顺序，快的在前），每频道截前 N 条
  5. 产物三格式（S2.4）：live.m3u（带 tvg-logo/EPG）/ live.txt(#genre#) / live.json(FongMi)；
     txt 按 TVBox 兼容惯例对 name/group 里的 "type" 做同形字替换、URL 里的做百分号编码
     （否则含 "type" 字样的文本会被 TVBox 误判成 FongMi JSON 格式）
  6. 空结果不覆盖旧产物：上游全挂/全部测死时保住上次有效清单（同 update.py 缓存兜底思想）
  7. aggregate.json 自动注入 lives 条目 → 用户导入点播聚合配置即自带直播源
  8. 防死循环：只写 output/ 与 README.md 的 LIVE 标记段（与 update.py 的 STATUS 段互不重叠）

运行顺序约定：CI 中先 update.py 后 live.py（live.py 会对本轮 aggregate.json 做 lives 注入）。
本地调试：python3 scripts/live.py
"""
import argparse
import json
import re
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update import (  # noqa: E402
    CST, CONFIG_FILE, OUTPUT_DIR, README_FILE, ROOT, UA_POOL,
    decode_body, delivery_base, history_append, history_derive,
    lenient_json, log, looks_like_html, punycode_url, resolve_repo,
)

import random  # noqa: E402  update.py 导入时已完成随机播种

LIVE_CONFIG_FILE = ROOT / "config" / "lives.json"
LIVE_TRANSIENT_RETRIES = 1   # 上游抓取瞬态错误重试次数（镜像多，单地址重试 1 次即可）

# 测速不校验 TLS 证书（大量 IPTV 流自签名/过期证书，播放器默认也不校验）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from zhconv import convert as _zh_hans  # 繁转简（纯 Python，依赖缺失时优雅降级）
except Exception:  # noqa: BLE001
    _zh_hans = None

# ---------------------------------------------------------------- 频道名规范化

_PARENS = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_QUALITY = re.compile(
    r"(?<![a-z0-9])(4k|8k|16k|1080[pi]?|720p|540p|480p|360p|fhd|uhd|qhd|h26[45]|hevc|avc|ipv[46])(?![a-z0-9])"
    r"|(?<![a-z])hd(?![a-z])|超高清|蓝光高清|超清|高清|标清|蓝光|\d+\s*帧|\d+fps",
    re.I)
_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff\u00c0-\u024f+&]")
_CCTV = re.compile(r"cctv(\d{1,2})(plus|\+)?")
_CHANNEL_SUFFIX = ("频道", "channel", "tv")

# iptv-org 的 group-title 是英文分类名 → 统一映射为中文分组（未收录的保持原样）
CAT_MAP = {
    "news": "新闻", "movies": "电影", "music": "音乐", "sports": "体育", "kids": "少儿",
    "documentary": "纪录", "entertainment": "综艺", "general": "综合", "series": "剧集",
    "culture": "文化", "education": "教育", "religious": "宗教", "lifestyle": "生活",
    "business": "财经", "travel": "旅游", "food": "美食", "weather": "天气", "comedy": "喜剧",
    "classic": "经典", "animation": "动画", "family": "家庭", "auto": "汽车", "health": "健康",
    "shop": "购物", "outdoor": "户外", "relax": "休闲", "legislative": "政务", "science": "科学",
}
CANON_GROUPS = ["央视", "卫视", "港澳台"]
_HK_TW = re.compile(
    r"tvb|凤凰|鳳凰|viutv|rthk|港台|有線|有线|tvbs|東森|东森|三立|中天|民視|民视|八大|臺視|台视"
    r"|中視|中视|華視|华视|公視|公视|大愛|大爱|靖天|龍華|龙华|緯來|纬来|momo|喜事|龍祥|龙祥|镜視|镜视")


def _t2s(s: str) -> str:
    return _zh_hans(s, "zh-hans") if _zh_hans else s


def normalize_name(name: str) -> str:
    """规范化频道名作为合并键：NFKC → 繁转简 → 去括注/画质词/分隔符 → CCTV 编号归一。"""
    s = unicodedata.normalize("NFKC", str(name)).strip().lower()
    s = _t2s(s)
    s = _PARENS.sub("", s)
    s = _QUALITY.sub("", s)
    s = _KEEP.sub("", s)
    m = _CCTV.match(s)
    if m:
        return f"cctv{int(m.group(1))}{'+' if m.group(2) else ''}"
    for suf in _CHANNEL_SUFFIX:
        if s.endswith(suf) and len(s) > len(suf) + 1:
            s = s[:-len(suf)]
            break
    return s


def clean_group(group: str) -> str:
    """上游分组名 → 展示分组名（去画质词、英文分类映射中文、空值兜底）。"""
    g = _PARENS.sub("", str(group or "")).strip()
    low = g.lower()
    if not g or low in ("group", "undefined", "other", "misc", "undefinedcategory"):
        return ""
    return CAT_MAP.get(low, g)


def canonical_group(display: str, source_group: str) -> str:
    """频道归属分组：央视/卫视/港澳台按名称规则归组，其余沿用上游分组（首个来源优先）。"""
    n = normalize_name(display)
    if _CCTV.match(n) or n.startswith("cgtn"):
        return "央视"
    if "卫视" in display[-5:]:
        return "卫视"
    if _HK_TW.search(display) or _HK_TW.search(source_group or ""):
        return "港澳台"
    return clean_group(source_group) or "其他"


# ---------------------------------------------------------------- 抓取与解析

def is_live_text(text: str) -> bool:
    if "#EXTM3U" in text or "#EXTINF" in text or "#genre#" in text:
        return True
    try:
        obj = lenient_json(text)
    except ValueError:
        return False
    if isinstance(obj, list):
        return True
    return isinstance(obj, dict) and isinstance(obj.get("live") or obj.get("lives"), list)


def fetch_text(session: requests.Session, url: str):
    """抓取单个地址的直播文本。返回 (text 或 None, 错误说明)。"""
    pu = punycode_url(url)
    last_err = "unknown"
    for attempt in range(LIVE_TRANSIENT_RETRIES + 1):
        try:
            r = session.get(pu, timeout=15, allow_redirects=True,
                            headers={"User-Agent": random.choice(UA_POOL)})
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = type(e).__name__
            time.sleep(2 * (attempt + 1))
            continue
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
        if r.status_code >= 500:
            last_err = f"HTTP {r.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}"
        text = decode_body(r)
        if not text.strip():
            last_err = "空响应"
            time.sleep(2 * (attempt + 1))
            continue
        if looks_like_html(text):  # 挑战页/防爬页，重试无意义
            return None, f"返回 HTML 页（{len(text)}B）"
        if not is_live_text(text):
            return None, "内容不含 EXTINF/#genre#/直播 JSON 结构"
        return text, ""
    return None, last_err


def _entry(name, group, url, sid, logo=None, tvg_id=None):
    return {"name": name, "group": group, "url": url,
            "logo": logo or None, "tvg_id": tvg_id or None, "source": sid}


def _split_extinf(line: str):
    """按第一个"引号外"的逗号切分 #EXTINF 行。

    上游常见 user-agent/referrer 属性值里带逗号（如 "Mozilla/5.0 ..., like Gecko)..."），
    朴素的首逗号切分会把 UA 串当成频道名；引号感知后属性完整、频道名干净。
    """
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ',' and not in_quote:
            return line[:i], line[i + 1:]
    return line, ""


def parse_m3u_entries(text: str, sid: str):
    entries, dropped = [], 0
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs_part, title = _split_extinf(line)
            attrs = dict(re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', attrs_part))
            pending = {"name": title.strip(), "group": (attrs.get("group-title") or "").strip(),
                       "logo": (attrs.get("tvg-logo") or "").strip(),
                       "tvg_id": (attrs.get("tvg-id") or "").strip()}
            continue
        if line.startswith("#"):
            continue
        url = line.split("$", 1)[0].strip()  # 兼容 TVBox "url$线路名" 后缀
        if not re.match(r"^https?://", url, re.I):
            dropped += 1  # udp/rtsp/rtmp 等本管线无法测速、多数播放场景也不可用，直接剔除
            continue
        e = pending or {}
        entries.append(_entry(e.get("name") or url.rsplit("/", 1)[-1], e.get("group", ""),
                              url, sid, e.get("logo"), e.get("tvg_id")))
        pending = None
    return entries, dropped


def parse_txt_entries(text: str, sid: str):
    entries, dropped = [], 0
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("#genre#"):
            group = re.sub(r",?\s*#genre#$", "", line, flags=re.I).strip()
            continue
        idx = line.find(",")
        if idx <= 0:
            continue
        name = line[:idx].strip()
        for part in line[idx + 1:].split("#"):
            u = part.split("$", 1)[0].strip()
            if re.match(r"^https?://", u, re.I):
                entries.append(_entry(name, group, u, sid))
            elif part.strip():
                dropped += 1
    return entries, dropped


def parse_json_entries(text: str, sid: str):
    """FongMi JSON：[{group, channels:[{name, urls:[...]}]}]，兼容 {live:[...]} 包裹。"""
    try:
        obj = lenient_json(text)
    except ValueError:
        return [], 1
    if isinstance(obj, dict):
        obj = obj.get("live") or obj.get("lives") or []
    if not isinstance(obj, list):
        return [], 1
    entries, dropped = [], 0
    for g in obj:
        if not isinstance(g, dict):
            dropped += 1
            continue
        group = str(g.get("group") or g.get("name") or "")
        channels = g.get("channels")
        if not isinstance(channels, list):
            # TVBox 配置包裹格式（lives 条目只有 url 指向另一份直播文件）暂不展开
            dropped += 1
            continue
        for ch in channels:
            if not isinstance(ch, dict):
                dropped += 1
                continue
            name = str(ch.get("name") or "").strip()
            if not name:
                dropped += 1
                continue
            urls = ch.get("urls") if isinstance(ch.get("urls"), list) else [ch.get("url")]
            for u in urls:
                u = str(u or "").split("$", 1)[0].strip()
                if re.match(r"^https?://", u, re.I):
                    entries.append(_entry(name, group, u, sid))
                else:
                    dropped += 1
    return entries, dropped


def parse_auto(text: str, sid: str):
    if text.lstrip().upper().startswith("#EXTM3U") or "#EXTINF" in text[:4096]:
        return parse_m3u_entries(text, sid)
    head = text.lstrip()[:1]
    if head in ("{", "["):
        return parse_json_entries(text, sid)
    return parse_txt_entries(text, sid)


# ---------------------------------------------------------------- 合并去重（S2.2）

def merge_entries(all_entries):
    """频道名规范化后合并：同规范名 → 一条频道多 URL；URL 全局去重（先到先得）。"""
    channels = OrderedDict()
    url_seen = OrderedDict()
    for e in all_entries:
        url = e["url"]
        if url in url_seen:
            continue
        url_seen[url] = True
        norm = normalize_name(e["name"]) or e["name"].strip().lower()
        ch = channels.get(norm)
        if ch is None:
            ch = {"display": e["name"], "group": canonical_group(e["name"], e["group"]),
                  "logo": e["logo"], "tvg_id": e["tvg_id"],
                  "urls": [], "sources": set()}
            channels[norm] = ch
        ch["urls"].append(url)
        ch["sources"].add(e["source"])
        if not ch["logo"] and e["logo"]:
            ch["logo"] = e["logo"]
        if not ch["tvg_id"] and e["tvg_id"]:
            ch["tvg_id"] = e["tvg_id"]
    return channels


# ---------------------------------------------------------------- 测速（S2.3）

_tls = threading.local()


def _probe_session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = _tls.session = requests.Session()
    return s


def _probe_once(url: str, timeout: float):
    s = _probe_session()
    headers = {"User-Agent": random.choice(UA_POOL), "Accept": "*/*", "Connection": "close"}
    t0 = time.time()

    def ms():
        return int((time.time() - t0) * 1000)

    # 1) HEAD：大部分 HLS/HTTP 流支持；4xx/5xx 或异常 → 降级 GET
    try:
        r = s.head(url, timeout=timeout, allow_redirects=True, headers=headers, verify=False)
        if r.status_code < 400:
            return True, ms(), ""
    except requests.exceptions.SSLError:
        return False, ms(), "SSLError"
    except Exception:  # noqa: BLE001
        pass
    # 2) GET 首包：读到任意字节即视为可播
    try:
        with s.get(url, timeout=timeout, stream=True, allow_redirects=True,
                   headers=headers, verify=False) as r:
            if r.status_code >= 400:
                return False, ms(), f"HTTP {r.status_code}"
            for chunk in r.iter_content(1024):
                if chunk:
                    return True, ms(), ""
            return False, ms(), "空响应"
    except requests.exceptions.Timeout:
        return False, ms(), "Timeout"
    except requests.exceptions.SSLError:
        return False, ms(), "SSLError"
    except requests.exceptions.ConnectionError:
        return False, ms(), "ConnectionError"
    except Exception as e:  # noqa: BLE001
        return False, ms(), type(e).__name__


def probe_url(url: str, timeout: float, conn_retries: int):
    """仅对连接错误做一次重试（瞬态）；超时判死（过慢的流没有点播价值）。"""
    ms_, err = 0, "unknown"
    for attempt in range(conn_retries + 1):
        ok, ms_, err = _probe_once(url, timeout)
        if ok or err != "ConnectionError":
            return ok, ms_, err
        time.sleep(1)
    return False, ms_, err


def probe_all(urls, workers: int, timeout: float, conn_retries: int):
    results, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe_url, u, timeout, conn_retries): u for u in urls}
        for fut in as_completed(futs):
            url = futs[fut]
            try:
                results[url] = fut.result()
            except Exception as e:  # noqa: BLE001  测速内部异常一律按死处理
                results[url] = (False, 0, f"internal:{type(e).__name__}")
            done += 1
            if done % 200 == 0:
                ok_n = sum(1 for v in results.values() if v[0])
                log(f"    测速进度 {done}/{len(urls)}，存活 {ok_n}")
    return results


# ---------------------------------------------------------------- 产物（S2.4）

def esc_attr(v: str) -> str:
    return str(v).replace('"', "")


def tvbox_safe_text(s: str) -> str:
    """txt 专用：, 与 # 是格式分隔符需替换；含 "type" 字样会被 TVBox 误判为 FongMi 格式 → 同形字替换。"""
    s = str(s).replace(",", "，").replace("#", "＃")
    return re.sub(r"(?i)type", "ty\u0440e", s)


def tvbox_safe_url(u: str) -> str:
    u = u.replace("#", "%23").replace("$", "%24")
    return re.sub(r"(?i)type", lambda m: ("%74" if m.group(0)[0] == "t" else "%54") + m.group(0)[1:], u)


def natural_key(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def order_groups(groups: OrderedDict):
    ordered = [g for g in CANON_GROUPS if g in groups]
    ordered += [g for g in groups if g not in CANON_GROUPS]
    return ordered


def write_live_outputs(out_dir: Path, groups: OrderedDict, epg: str):
    # live.m3u：每频道每地址一条目（地址按延迟升序），带 tvg-id/tvg-logo/group-title
    lines = ["#EXTM3U" + (f' x-tvg-url="{esc_attr(epg)}"' if epg else "")]
    for g in order_groups(groups):
        for ch in groups[g]:
            attrs = f' tvg-id="{esc_attr(ch["tvg_id"])}"' if ch["tvg_id"] else ""
            if ch["logo"]:
                attrs += f' tvg-logo="{esc_attr(ch["logo"])}"'
            attrs += f' group-title="{esc_attr(g)}"'
            for u in ch["urls"]:
                lines.append(f"#EXTINF:-1{attrs},{ch['display']}")
                lines.append(u)
    (out_dir / "live.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # live.txt：TVBox #genre# 格式，同频道多地址用 # 连接（快的在前，播放失败自动切下一地址）
    lines = []
    for g in order_groups(groups):
        lines.append(f"{tvbox_safe_text(g)},#genre#")
        for ch in groups[g]:
            lines.append(f"{tvbox_safe_text(ch['display'])},"
                         f"{'#'.join(tvbox_safe_url(u) for u in ch['urls'])}")
    (out_dir / "live.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # live.json：FongMi JSON 格式
    data = [{"group": g,
             "channels": [{"name": ch["display"], "urls": ch["urls"]} for ch in groups[g]]}
            for g in order_groups(groups)]
    (out_dir / "live.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                       encoding="utf-8")


# ---------------------------------------------------------------- 状态/README/aggregate

def build_live_readme(status_list, summary: dict) -> str:
    lines = [
        f"> 直播源自动更新于 {datetime.now(CST):%Y-%m-%d %H:%M}：上游 {summary['upstream_ok']}/{summary['upstream_total']} 可用，"
        f"产出 **{summary['channels_out']}** 频道 / **{summary['urls_out']}** 条有效地址"
        f"（原始 {summary['urls_raw']}，测速剔除失效 {summary['urls_raw'] - summary['urls_out']}）。"
        f" 手动触发：Actions → update → Run workflow。",
        "",
        "| 直播源 | 状态 | 生效地址 | 素材条目 | 贡献地址 | 错误 |",
        "|---|---|---|---|---|---|",
    ]
    for s in status_list:
        addr = s["used_url"] or "-"
        if len(addr) > 55:
            addr = addr[:52] + "..."
        err = (s.get("error") or "-").replace("|", "\\|")
        if len(err) > 60:
            err = err[:57] + "..."
        state = "✅" if s["ok"] else "❌"
        lines.append(f"| {s['name']} | {state} | {addr} | {s.get('entries', 0)} | "
                     f"{s.get('urls', 0)} | {err} |")
    return "\n".join(lines)


def rewrite_readme_live(table_md: str):
    if not README_FILE.exists():
        return
    text = README_FILE.read_text(encoding="utf-8")
    pattern = re.compile(r"(<!-- LIVE-START -->).*?(<!-- LIVE-END -->)", re.S)
    if not pattern.search(text):
        log("README 缺少 LIVE 标记，跳过直播状态表回写")
        return
    README_FILE.write_text(
        pattern.sub(lambda m: m.group(1) + "\n" + table_md + "\n" + m.group(2), text),
        encoding="utf-8")


def rewrite_live_badge(base: str):
    if not README_FILE.exists():
        return
    text = README_FILE.read_text(encoding="utf-8")
    new = f"![直播源](https://img.shields.io/endpoint?url={base}/live_shield.json)"
    out = re.sub(r"!\[直播源\]\([^)]*\)", new, text)  # 全部替换：防双平台 CI 交替改写产生的重复徽章
    if "![直播源]" not in out:  # 尚无直播徽章 → 挂到源健康徽章后面
        out = re.sub(r"(!\[源健康\]\([^)]*\))", r"\1 " + new, out, count=1)
    if out != text:
        README_FILE.write_text(out, encoding="utf-8")


def inject_into_aggregate(aggregate_path: Path, live_url: str, epg: str):
    """把本仓库 live.txt 作为 lives 条目注入 aggregate.json → 导入点播聚合即自带直播。"""
    if not aggregate_path.exists():
        return
    try:
        agg = lenient_json(aggregate_path.read_text(encoding="utf-8"))
        lives = agg.get("lives") if isinstance(agg.get("lives"), list) else []
        lives = [x for x in lives if not (isinstance(x, dict) and x.get("url") == live_url)]
        entry = {"name": "自建直播·自动保活", "type": 0, "url": live_url}
        if epg:
            entry["epg"] = epg
        lives.append(entry)
        agg["lives"] = lives
        aggregate_path.write_text(json.dumps(agg, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"    aggregate.json 已注入直播条目（{live_url.rsplit('/', 1)[-1]}）")
    except Exception as e:  # noqa: BLE001  注入失败不影响直播产物本身
        log(f"    aggregate.json 注入失败: {e}")


# ---------------------------------------------------------------- 主流程

def main(cfg_path=None, out_dir=None, readme_path=None, aggregate_path=None,
         delivery=None) -> int:
    global README_FILE
    live_cfg = lenient_json(Path(cfg_path or LIVE_CONFIG_FILE).read_text(encoding="utf-8"))
    src_cfg = {}
    if CONFIG_FILE.exists():
        src_cfg = lenient_json(CONFIG_FILE.read_text(encoding="utf-8"))
    repo = resolve_repo(src_cfg)
    base = delivery or delivery_base(repo)
    if readme_path is not None:
        README_FILE = Path(readme_path)
    out = Path(out_dir or OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)

    probe_cfg = live_cfg.get("probe") or {}
    workers = int(probe_cfg.get("workers", 24))
    timeout = float(probe_cfg.get("timeout", 5))
    conn_retries = int(probe_cfg.get("conn_retries", 1))
    max_latency = int(probe_cfg.get("max_latency_ms", 0))
    max_urls = int(probe_cfg.get("max_urls_per_channel", 6))
    epg = str(live_cfg.get("epg") or "")
    sources = live_cfg.get("sources", [])

    session = requests.Session()
    log(f"直播管线开始：{len(sources)} 个上游源（repo={repo}）")

    status_list, all_entries = [], []
    for source in sources:
        sid, name = source["id"], source["name"]
        log(f"▶ 直播上游 {name} ({sid})")
        text, used_url, err = None, None, ""
        entries, dropped = [], 0
        errors = []
        for url in source.get("urls", []):
            text, ferr = fetch_text(session, url)
            if text is not None:
                used_url = url
                break
            errors.append(f"{url.split('://')[-1][:50]}: {ferr}")
            log(f"    × {errors[-1]}")
        if text is not None:
            entries, dropped = parse_auto(text, sid)
            log(f"    ✓ 解析 {len(entries)} 条目（剔除非 HTTP {dropped}）")
        else:
            err = "; ".join(errors)
            log(f"    ✗ 上游不可用: {err[:120]}")
        all_entries.extend(entries)
        status_list.append({"id": sid, "name": name, "ok": used_url is not None,
                            "used_url": used_url, "error": err,
                            "entries": len(entries), "urls": 0})

    # ---- 合并去重
    channels = merge_entries(all_entries)
    urls_raw = [u for ch in channels.values() for u in ch["urls"]]
    # 按来源统计贡献的唯一地址数（merge 已保证 URL 全局唯一，逐条目首见即计）
    contrib = {}
    seen_contrib = set()
    for e in all_entries:
        if e["url"] not in seen_contrib:
            seen_contrib.add(e["url"])
            contrib[e["source"]] = contrib.get(e["source"], 0) + 1
    for s in status_list:
        s["urls"] = contrib.get(s["id"], 0)
    log(f"合并完成：{len(channels)} 频道 / {len(urls_raw)} 条唯一地址")

    # ---- 测速过滤
    t0 = time.time()
    results = probe_all(urls_raw, workers, timeout, conn_retries)
    probe_seconds = int(time.time() - t0)
    alive = {u: v for u, v in results.items() if v[0]
             and (max_latency <= 0 or v[1] <= max_latency)}

    groups = OrderedDict()
    for ch in channels.values():
        scored = [(results[u][1], u) for u in ch["urls"] if u in alive]
        if not scored:
            continue
        scored.sort()  # 延迟升序 = 备线切换优先级
        ch["urls"] = [u for _, u in scored][:max_urls]
        groups.setdefault(ch["group"], []).append(ch)
    for g in groups:
        groups[g].sort(key=lambda c: natural_key(c["display"]))

    channels_out = sum(len(v) for v in groups.values())
    urls_out = sum(len(ch["urls"]) for v in groups.values() for ch in v)
    summary = {"upstream_total": len(status_list),
               "upstream_ok": sum(1 for s in status_list if s["ok"]),
               "channels_raw": len(channels), "urls_raw": len(urls_raw),
               "channels_out": channels_out, "urls_out": urls_out,
               "groups": len(groups), "probe_seconds": probe_seconds}
    log(f"测速完成（{probe_seconds}s）：存活 {len(alive)}/{len(urls_raw)} 地址，"
        f"产出 {channels_out} 频道 / {len(groups)} 分组")

    # ---- 产物（空结果不覆盖旧文件，保住上次有效清单）
    if channels_out > 0:
        write_live_outputs(out, groups, epg)
    else:
        log("警告：无任何可用直播频道，保留 output/ 现有 live.* 产物不变")

    write_json = lambda p, obj: p.write_text(  # noqa: E731
        json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- S3.1: 直播上游状态历史 → 连续失败/最近成功/贡献地址趋势
    live_states = {s["id"]: {"ok": s["ok"], "urls": s.get("urls", 0)} for s in status_list}
    live_hist = history_append(out / "live_status_history.json", live_states)
    for s in status_list:
        d = history_derive(live_hist, s["id"], field="urls")
        s["last_success_at"] = d["last_success_at"]
        s["last_failure_at"] = d["last_failure_at"]
        s["consecutive_failures"] = d["consecutive_failures"]
        s["urls_trend"] = d["trend"]

    write_json(out / "live_status.json", {
        "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "repo": repo, "epg": epg,
        "summary": summary, "sources": status_list,
    })
    ratio = urls_out / len(urls_raw) if urls_raw else 0
    color = "brightgreen" if urls_out >= 150 else "green" if urls_out >= 60 else \
            "yellowgreen" if urls_out >= 20 else "orange" if urls_out > 0 else "red"
    write_json(out / "live_shield.json", {
        "schemaVersion": 1, "label": "直播源",
        "message": f"{channels_out} 频道", "color": color,
    })

    rewrite_readme_live(build_live_readme(status_list, summary))
    rewrite_live_badge(base)
    if channels_out > 0 and aggregate_path is not None:
        inject_into_aggregate(Path(aggregate_path), f"{base}/live.txt", epg)

    log(f"直播管线完成：{summary['upstream_ok']}/{summary['upstream_total']} 上游可用，"
        f"产出 {channels_out} 频道 / {urls_out} 地址 → live.m3u / live.txt / live.json")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TVBox 直播源保活管线（S2）")
    ap.add_argument("--config", help="直播配置路径（默认 config/lives.json）")
    ap.add_argument("--output", help="产物目录（默认 output/）")
    ap.add_argument("--readme", help="README 路径（默认仓库根 README.md）")
    ap.add_argument("--aggregate", help="aggregate.json 路径（注入 lives 条目；缺省跳过）")
    ap.add_argument("--delivery-base", dest="delivery_base",
                    help="产物 raw 访问前缀（默认按 CI 平台自动推导）")
    a = ap.parse_args()
    sys.exit(main(a.config, a.output, a.readme, a.aggregate, a.delivery_base))
