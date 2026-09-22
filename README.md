# My TVBox Sources · 自建 TVBox 配置聚合源仓库

> 项目《桌面端设计方案.md》主线 B · S1 的实现：自动聚合 + 冗余回退 + 缓存兜底。
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

- `aggregate.json` — 所有源 sites 合并去重（key+api 去重、key 冲突加后缀、黑名单剔除虎牙/斗鱼等网络直播站），开箱即用，lives 首位置顶指向本仓库 live.txt 的直播条目（上游无效直播配置自动剔除）
- `subscribe.json` — 各源独立入口列表，App 内可切换子源
- `<id>.json` — 每个上游源的最新成功快照（抓取失败时沿用上次的，保证不断供）
- `live.m3u` / `live.txt` / `live.json` — 直播源（同一份频道的三种格式；测速筛掉失效地址、按延迟排序、多备线自动切换）
- `status.json` / `live_status.json` — 点播源 / 直播源健康状态（机器可读），含 S3.1 历史派生字段（连续失败次数 / 最近成功 / 延迟趋势），App 与看板消费
- `status_history.json` / `live_status_history.json` — 跨次运行的状态历史（滚动 60 窗口，趋势由它派生）
- `shield.json` / `live_shield.json` / `update_shield.json` — README 徽章数据（源健康 / 直播 / 更新时间）
- `docs/index.html` — 健康看板（gh-pages 分支托管，见上方"健康看板"）

本地调试：`pip install -r requirements.txt && python scripts/update.py && python scripts/live.py --aggregate output/aggregate.json`

## 维护 SOP（每 1~2 周一次，约 5 分钟，S3.4）

1. 看下方状态表 / [健康看板](https://jianghaopi.github.io/my-tvbox-sources/)：**连续 ❌ 且无 📦** 的源（看板上有"连续 N 次"标记与延迟趋势）→ 去社区（TVBox 接口维护合集、微信群分享）找它的**新地址**，替换 `urls[0]`、旧地址留作备用；
2. 整个源彻底失效 → 删掉，并从社区列表补 1 个新源进来。**冗余度检查**：点播同类源刻意保持 3~5 个、直播上游保持 3~4 个——低于这个数就该补，否则上游批量失效时兜底不够；
3. 直播侧同法检查 `config/lives.json`：某上游"贡献地址"连续走低（看板趋势）→ 换镜像或补新上游；
4. 提交即自动触发一次全量聚合，无需等待定时任务；
5. 桌面端 App 会在启动时读取本仓库 status.json：上游连续失效 / 缓存兜底 / 仓库停超 36h 时自动弹提示（同一份状态只提示一次），无需人工巡检。

## 当前源状态

<!-- STATUS-START -->
> 自动更新于 2026-09-22 16:08（5/12 源可用）。 手动触发：Actions → update → Run workflow。

| 源 | 状态 | 生效地址 | 站点 | 错误 |
|---|---|---|---|---|
| 肥猫 | ✅ | http://肥猫.net/tv | 39 | - |
| 饭太硬 | ❌ | - | 0 | www.饭太硬.com/tv: ConnectionError; www.饭太硬.net/tv: 返回 HTML 页（挑战页或失效页, 14213B）; ... |
| 王二小 | ✅ | https://d.kstore.dev/download/9280/wex.json | 63 | - |
| 讴歌 | 📦 缓存兜底 | - | 12 | tv.nxog.top/m/: HTTP 403; 欧歌.v.nxog.top/m/: SSLError; 欧歌zp8.v.nxog.top/m/: HT... |
| 摸鱼 | ❌ | - | 0 | 我不是.摸鱼儿.top: 返回 HTML 页（挑战页或失效页, 101400B）; 我不是.摸鱼儿.com: 返回 HTML 页（挑战页或失效页, 698... |
| OK | ❌ | - | 0 | ok321.top/ok: ConnectionError; ok321.top/tv: ConnectionError |
| 小米 | ❌ | - | 0 | www.mpanso.com/小米/DEMO.json: HTTP 404; www.mpanso.com/小米/DEMO.json: HTTP 404;... |
| 巧记 | ❌ | - | 0 | cdn.qiaoji8.com/tvbox.json: ConnectionError |
| 4K小盒子 | ✅ | http://xhztv.top/4k.json | 53 | - |
| 潇洒 | ❌ | - | 0 | 9877.kstore.space/one.json: HTTP 404; 9877.kstore.space/AnotherD/api.json: HT... |
| FongMi(蜂蜜) | ✅ | https://raw.githubusercontent.com/FongMi/CatVodSpider/mai... | 3 | - |
| qist合集 | ✅ | https://raw.githubusercontent.com/qist/tvbox/master/367.json | 106 | - |
<!-- STATUS-END -->

## 直播源状态

<!-- LIVE-START -->
> 直播源自动更新于 2026-09-22 16:16：上游 4/4 可用，产出 **399** 频道 / **747** 条有效地址（原始 1802，测速剔除失效 1055）。 手动触发：Actions → update → Run workflow。

| 直播源 | 状态 | 生效地址 | 素材条目 | 贡献地址 | 错误 |
|---|---|---|---|---|---|
| fanmingming/live（IPV6直连） | ✅ | https://raw.githubusercontent.com/fanmingming/live/m... | 82 | 82 | - |
| iptv-org（中国） | ✅ | https://iptv-org.github.io/iptv/countries/cn.m3u | 144 | 144 | - |
| iptv-org（中文语区） | ✅ | https://iptv-org.github.io/iptv/languages/zho.m3u | 212 | 75 | - |
| Guovin/iptv-api（每日自选） | ✅ | https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/outpu... | 1616 | 1501 | - |
<!-- LIVE-END -->

## 免责声明

本仓库仅聚合社区公开配置地址做个人学习使用，不存储任何视频内容；如上游内容侵犯了您的权利，请联系对应上游而非本仓库。
