# My TVBox Sources · 自建 TVBox 配置聚合源仓库

> 自动维护完整配置、多仓切换、上游回退和缓存兜底。
> 桌面端 / TVBox / 影视仓 直接填本仓库 `output/aggregate.json` 的 raw 地址即可。

![源健康](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/JianghaoPi/my-tvbox-sources/main/output/shield.json) ![更新](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/JianghaoPi/my-tvbox-sources/main/output/update_shield.json) ![直播源](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/JianghaoPi/my-tvbox-sources/main/output/live_shield.json)

## 使用方式

| 消费者 | 填入地址 |
|---|---|
| 桌面端 / TVBox（开箱即用，点播+直播） | `https://raw.githubusercontent.com/<仓库路径>/main/output/aggregate.json` |
| 影视仓 / 支持多仓切换的端 | `https://raw.githubusercontent.com/<仓库路径>/main/output/subscribe.json` |
| 各源独立配置 | `https://raw.githubusercontent.com/<仓库路径>/main/output/<id>.json` |
| 直播（TVBox / 影视仓 / 桌面端直播页） | `https://raw.githubusercontent.com/<仓库路径>/main/output/live.txt` |
| 直播（VLC / 其他 m3u 播放器） | `https://raw.githubusercontent.com/<仓库路径>/main/output/live.m3u` |
| 直播（FongMi JSON 消费端） | `https://raw.githubusercontent.com/<仓库路径>/main/output/live.json` |
| 健康看板（浏览器打开，S3） | <https://jianghaopi.github.io/my-tvbox-sources/>（首次需在仓库 Settings → Pages 把 Source 设为 gh-pages 分支） |
| 健康状态（机器可读） | `https://raw.githubusercontent.com/<仓库路径>/main/output/status.json` ｜ 直播：`live_status.json` |

> 首次使用：把 `config/sources.json` 里的 `repo` 字段改成你的 `用户名/仓库名`（Actions 运行时会自动读 `GITHUB_REPOSITORY`，`subscribe.json` 里的地址才会正确）。

## 工作原理

```
config/sources.json ──► scripts/update.py ──► output/*（点播：聚合/订阅/快照/状态）
config/lives.json   ──► scripts/live.py   ──► output/live.*（直播：合并去重+测速保活）
   人工维护：上游源列表               抓取(多地址回退/重试/宽容解析)       每日 2 次定时 + 手动触发
   每源主地址+备用地址                 测速筛掉失效 → 状态回写 README       .github/workflows/update.yml + .cnb.yml
```

产物说明：

- `aggregate.json` — 按 `aggregate_prefer` 选择一套完整配置，在线源优先于缓存。保留该源的 JAR、规则、广告过滤等上下文，不混合不同 JAR 的 `csp_*` 站点；剔除虎牙/斗鱼等黑名单站点，首位置顶本仓库直播入口
- `subscribe.json` — 各源独立入口列表，App 内可切换子源
- `<id>.json` — 每个上游源解码后的最新成功快照，相对资源路径按最终响应 URL 补全；抓取失败沿用上次快照，缓存不保证依赖或播放仍然有效
- `live.m3u` / `live.txt` / `live.json` — 直播源（同一份频道的三种格式；测速筛掉失效地址、按延迟排序、多备线自动切换）
- `status.json` / `live_status.json` — 点播源 / 直播源健康状态（机器可读），含 S3.1 历史派生字段（连续失败次数 / 最近成功 / 延迟趋势），App 与看板消费
- `status_history.json` / `live_status_history.json` — 跨次运行的状态历史（滚动 60 窗口，趋势由它派生）
- `shield.json` / `live_shield.json` / `update_shield.json` — README 徽章数据（源健康 / 直播 / 更新时间）
- `docs/index.html` — 健康看板（gh-pages 分支托管，见上方"健康看板"）

本地调试：`pip install -r requirements.txt && python scripts/update.py && python scripts/live.py --aggregate output/aggregate.json`

## 2026-11-19 源维护

本轮逐项检查了 68 个现有及候选入口，并抽查顶层 JAR 和部分规则文件，详见 [调研记录](docs/source-review-2026-11-19.md)。

- 恢复肥猫、饭太硬、摸鱼儿、小米、潇洒；qist 从 `367.json` 改用 `jsm.json`，小盒子优先使用 `/xhz/`。
- 新增南风、香雅情、老刘备；欧歌、OK、巧技暂设 `enabled: false`，保留地址与停用原因，停止重试和加入订阅。
- 配置请求使用固定播放器 User-Agent，可在源的 `headers` 覆盖；支持 JSON、图片后附 Base64、响应内嵌密钥的 `2423` AES-CBC 格式。
- “✅”表示配置可解析且顶层 JAR 可读取 ZIP 文件头；没有执行 JAR、逐站搜索或实际播放验证。FongMi 官方配置主要是本地/WebDAV/扩展示例，不能按普通影视内容源理解。
- 默认入口通过完整配置回退保持 JAR 与规则一致；在支持多仓的客户端使用 `subscribe.json` 可以切换全部启用源。

离线回归：`python scripts/selftest_sources.py && python scripts/selftest_status.py && python scripts/selftest_live.py`。

## 维护 SOP（每 1~2 周一次，约 5 分钟，S3.4）

1. 看下方状态表 / [健康看板](https://jianghaopi.github.io/my-tvbox-sources/) 的连续失败与最近成功时间，缓存也需要检查。先区分地址迁移、UA 分流、配置封装和 JAR 失效，再根据维护者说明更新地址；
2. 暂无可验证替代入口 → 设置 `enabled: false` 并记录 `disabled_reason`，从默认入口与订阅移除。新增源先检查配置和依赖，再放入 `sources`；用 `aggregate_prefer` 调整默认回退顺序。点播尽量保留 3~5 个不同维护者的源，直播保持 3~4 个；
3. 直播侧同法检查 `config/lives.json`：某上游"贡献地址"连续走低（看板趋势）→ 换镜像或补新上游；
4. 提交即自动触发一次全量聚合，无需等待定时任务；
5. 桌面端 App 会在启动时读取本仓库 status.json：上游连续失效 / 缓存兜底 / 仓库停超 36h 时自动弹提示（同一份状态只提示一次），无需人工巡检。

## 当前源状态

<!-- STATUS-START -->
> 自动更新于 2026-11-19 22:57（12/12 源通过配置及 JAR 检查，未验证逐站播放）。 手动触发：Actions → update → Run workflow。

| 源 | 状态 | 生效地址 | 站点 | 错误 |
|---|---|---|---|---|
| 肥猫 | ✅ | http://肥猫.net/ | 41 | - |
| 饭太硬 | ✅ | https://www.饭太硬.cc/tv | 49 | - |
| 王二小 | ✅ | https://d.kstore.dev/download/9280/wex.json | 100 | - |
| 摸鱼 | ✅ | http://我不是.摸鱼儿.cc/ | 109 | - |
| 小米 | ✅ | https://raw.githubusercontent.com/ggrrttyyiii/CatVodSpide... | 44 | - |
| 小盒子（单仓） | ✅ | http://xhztv.top/xhz/ | 54 | - |
| 潇洒 | ✅ | https://9877.kstore.space/sun.json | 119 | - |
| FongMi（本地/WebDAV示例） | ✅ | https://raw.githubusercontent.com/FongMi/CatVodSpider/mai... | 3 | - |
| qist家庭合集 | ✅ | https://raw.githubusercontent.com/qist/tvbox/master/jsm.json | 163 | - |
| 南风 | ✅ | https://raw.githubusercontent.com/yoursmile66/TVBox/main/... | 93 | - |
| 香雅情 | ✅ | https://raw.githubusercontent.com/xyq254245/xyqonlinerule... | 48 | - |
| 老刘备 | ✅ | https://raw.liucn.cc/box/m.json | 234 | - |
<!-- STATUS-END -->

## 直播源状态

<!-- LIVE-START -->
> 直播源自动更新于 2026-11-19 23:04：上游 4/4 可用，产出 **382** 频道 / **695** 条有效地址（原始 1811，测速剔除失效 1116）。 手动触发：Actions → update → Run workflow。

| 直播源 | 状态 | 生效地址 | 素材条目 | 贡献地址 | 错误 |
|---|---|---|---|---|---|
| fanmingming/live（IPV6直连） | ✅ | https://raw.githubusercontent.com/fanmingming/live/m... | 82 | 82 | - |
| iptv-org（中国） | ✅ | https://iptv-org.github.io/iptv/countries/cn.m3u | 151 | 151 | - |
| iptv-org（中文语区） | ✅ | https://iptv-org.github.io/iptv/languages/zho.m3u | 222 | 78 | - |
| Guovin/iptv-api（每日自选） | ✅ | https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/outpu... | 1616 | 1500 | - |
<!-- LIVE-END -->

## 免责声明

本仓库仅聚合社区公开配置地址做个人学习使用，不存储任何视频内容；如上游内容侵犯了您的权利，请联系对应上游而非本仓库。
