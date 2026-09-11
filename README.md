# My TVBox Sources · 自建 TVBox 配置聚合源仓库

> 项目《桌面端设计方案.md》主线 B · S1 的实现：自动聚合 + 冗余回退 + 缓存兜底。
> 桌面端 / TVBox / 影视仓 直接填本仓库 `output/aggregate.json` 的 raw 地址即可。

![源健康](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/JianghaoPi/my-tvbox-sources/main/output/shield.json)

## 使用方式

| 消费者 | 填入地址 |
|---|---|
| 桌面端 / TVBox（开箱即用） | `https://raw.githubusercontent.com/<仓库路径>/main/output/aggregate.json` |
| 影视仓 / 支持多仓切换的端 | `https://raw.githubusercontent.com/<仓库路径>/main/output/subscribe.json` |
| 各源独立配置 | `https://raw.githubusercontent.com/<仓库路径>/main/output/<id>.json` |
| 健康状态（机器可读） | `https://raw.githubusercontent.com/<仓库路径>/main/output/status.json` |

> 首次使用：把 `config/sources.json` 里的 `repo` 字段改成你的 `用户名/仓库名`（Actions 运行时会自动读 `GITHUB_REPOSITORY`，`subscribe.json` 里的地址才会正确）。

## 工作原理

```
config/sources.json ──► scripts/update.py ──► output/*（自动 commit 回仓库）
   人工维护：3~5 个上游源          抓取(多地址回退/重试/宽容解析)      每日 2 次定时 + 手动触发
   每源主地址+备用地址              校验结构 → 缓存兜底 → 合并去重        .github/workflows/update.yml
```

产物说明：

- `aggregate.json` — 所有源 sites 合并去重（key+api 去重、key 冲突加后缀），开箱即用
- `subscribe.json` — 各源独立入口列表，App 内可切换子源
- `<id>.json` — 每个上游源的最新成功快照（抓取失败时沿用上次的，保证不断供）
- `status.json` — 各源健康状态（ok/used_url/error/站点数/耗时），App 与巡检消费
- `shield.json` — README 徽章数据

本地调试：`pip install -r requirements.txt && python scripts/update.py`

## 维护 SOP（每 1~2 周一次，约 5 分钟）

1. 看下方状态表：连续 ❌ 且无 📦 的源 → 去社区（TVBox 接口维护合集、微信群分享）找它的**新地址**，替换 `urls[0]`、旧地址留作备用；
2. 整个源彻底失效 → 删掉，并从社区列表补 1 个新源进来（刻意保持同类冗余 3~5 个）;
3. 提交即自动触发一次全量聚合，无需等待定时任务。

## 当前源状态

<!-- STATUS-START -->
> 自动更新于 2026-09-11 22:04（5/12 源可用）。 手动触发：Actions → update → Run workflow。

| 源 | 状态 | 生效地址 | 站点 | 错误 |
|---|---|---|---|---|
| 肥猫 | ✅ | http://肥猫.net/tv | 39 | - |
| 饭太硬 | ❌ | - | 0 | www.饭太硬.com/tv: ConnectionError; www.饭太硬.net/tv: 返回 HTML 页（挑战页或失效页, 14213B）; ... |
| 王二小 | ✅ | https://d.kstore.dev/download/9280/wex.json | 63 | - |
| 讴歌 | ❌ | - | 0 | tv.nxog.top/m/: HTTP 550; 欧歌.v.nxog.top/m/: ConnectionError; 欧歌zp8.v.nxog.top... |
| 摸鱼 | ❌ | - | 0 | 我不是.摸鱼儿.top: 返回 HTML 页（挑战页或失效页, 138B）; 我不是.摸鱼儿.com: 返回 HTML 页（挑战页或失效页, 6990B）... |
| OK | ❌ | - | 0 | ok321.top/ok: ConnectionError; ok321.top/tv: ConnectionError |
| 小米 | ❌ | - | 0 | www.mpanso.com/小米/DEMO.json: HTTP 404; www.mpanso.com/小米/DEMO.json: HTTP 404;... |
| 巧记 | ❌ | - | 0 | cdn.qiaoji8.com/tvbox.json: ConnectionError |
| 4K小盒子 | ✅ | http://xhztv.top/4k.json | 53 | - |
| 潇洒 | ❌ | - | 0 | 9877.kstore.space/one.json: HTTP 403; 9877.kstore.space/AnotherD/api.json: HT... |
| FongMi(蜂蜜) | ✅ | https://raw.githubusercontent.com/FongMi/CatVodSpider/mai... | 3 | - |
| qist合集 | ✅ | https://raw.githubusercontent.com/qist/tvbox/master/367.json | 106 | - |
<!-- STATUS-END -->

## 免责声明

本仓库仅聚合社区公开配置地址做个人学习使用，不存储任何视频内容；如上游内容侵犯了您的权利，请联系对应上游而非本仓库。
