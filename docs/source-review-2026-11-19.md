# TVBox 源维护调研 · 2026-11-19

## 验证范围

本次在当前机器网络下初筛了 **68 个入口**，包括现有源的主备地址、社区提供的新地址及四个直播上游；对可解析配置抽查顶层爬虫 JAR 和部分规则文件。原始 HTTP 状态、格式与依赖结果见 [检测证据 JSON](source-review-2026-11-19.json)，最终启用源结果见 [status.json](../output/status.json)。时间为 UTC+8。

验证层次必须区分：

1. **入口可读**：HTTP 成功，但返回 HTML、图片或加密文本不等于配置失效。
2. **配置及核心依赖可读**：配置解码后有站点，顶层 JAR 返回 ZIP 文件头。更新器现在按这一层判定在线。
3. **实际可播放**：还取决于具体站点、客户端能力、WebView、账号、地区和网络。本次没有运行上游 JAR 或脚本，没有逐站搜索或影片实播；第二层通过不能当成第三层保证。也没有下载整个 JAR 核对 MD5 或审计其内容。

旧仓库 2026-11-19 19:03 的状态是 **4/12 在线、2 个缓存**。其中 qist 旧入口和 4K 小盒子虽然配置能抓取，核心 JAR 实际已失效，原来的健康统计偏乐观。

维护后于 **2026-11-19 22:44:30** 完整运行点播更新：**12/12 个启用源通过配置及顶层 JAR 检查，0 个使用缓存，另有 3 个源停用**。默认入口采用 qist 家庭合集，163 个站点；摸鱼儿本轮通过 kstore 备用地址恢复，记录了此前几个入口的依赖超时。

## 启用源与接入调整

以下站点数是本次取到的配置条目数，包含本地、公告和网盘等项目，不代表等量的可播放影视站。

| 源 / ID | 当前采用的入口 | 观察与维护动作 | 初筛站点数 |
|---|---|---|---:|
| 肥猫 / `feimao` | `http://肥猫.net/`，备选 `http://hello.肥猫.net/` | `.com/tv` 返回 HTML，`.net/tv` 为 404，kstore 旧地址 401。新根路径返回 `2423` AES-CBC 封装；可解码，JAR 可取 | 41 |
| 饭太硬 / `fantaiying` | `https://www.饭太硬.cc/tv`，备选 `.net/tv`、qist `fty.json` | 图片后附 Base64 配置，原脚本把 JPEG 当 JSON 解析。新旧 `.net` 入口均能解码，JAR 可取 | 49 |
| 王二小 / `wangerxiao` | `https://d.kstore.dev/download/9280/wex.json` | 保留。kstore 镜像和两个中文域名也能读取；有些地址重定向到 `.jpg`，内容仍是 JSON，应按字节内容解析 | 100 |
| 摸鱼儿 / `moyu` | `http://我不是.摸鱼儿.cc/`，备选 `.top`、`www.y456y.com`、`6800.kstore.vip/fish.json` | 固定播放器 UA 能取到 JSON；浏览器 UA 会拿到说明/挑战页。JAR 首次超时，复核可读，存在网络波动 | 109（kstore 111） |
| 小米 / `xiaomi` | `https://raw.githubusercontent.com/ggrrttyyiii/CatVodSpider/main/json/demo.json` | mpanso 旧地址 404。改用可读取的维护仓库配置，JAR 可取；仓库说明对特定客户端/授权有要求，兼容性需用实际客户端确认 | 44 |
| 小盒子 / `4k` | `http://xhztv.top/xhz/` | 改为完整单仓。旧 `4k.json` 的 JAR 返回 403；`/xhz/` 的 JAR 复核为 ZIP。部分响应被压缩分块，检查器必须累计字节再判断文件头 | 54 |
| 潇洒 / `xiaosa` | `https://9877.kstore.space/sun.json`，备选 qist `xiaosa/api.json` | 旧 `one.json` 超时，`AnotherD/api.json` 返回 502；新 `sun.json` 为 `2423` AES-CBC，解码后 JAR 可取 | 119（镜像 117） |
| FongMi / `fongmi` | `https://raw.githubusercontent.com/FongMi/CatVodSpider/main/json/config.json` | 保留，但明确标为本地/WebDAV/扩展示例。`../jar/custom_spider.jar`、`./market.json` 等相对地址必须在转存前补全 | 3 |
| qist / `qist` | `https://raw.githubusercontent.com/qist/tvbox/master/jsm.json` | 原 `367.json` 配置仍为 200，但其中摸鱼儿旧 JAR 与规则地址为 404。改用维护者 README 推荐的家庭配置 `jsm.json`，JAR 可取，设为默认入口首选 | 163 |
| 南风 / `nanfeng` | `https://raw.githubusercontent.com/yoursmile66/TVBox/main/XC.json` | **新增**。`2423` AES-CBC，内嵌密钥与 IV；解码后 JAR 和抽查规则可读 | 93 |
| 香雅情 / `xyq` | `https://raw.githubusercontent.com/xyq254245/xyqonlinerule/main/XYQTVBox.json` | **新增**。JSON，JAR 与抽查规则可读；上游建议 Android 9 以上、WebView 117 以上 | 48 |
| 老刘备 / `liucn` | `https://raw.liucn.cc/box/m.json`，备选 GitHub `liu673cn/box/main/m.json` | **新增**。两处配置和 JAR 可读；两地址为同一维护源，不能当成两份独立冗余 | 234 |

肥猫还包含少量 `./api/`、`./js/` 相对 JS 规则，初筛时对应资源为 404。其核心 JAR 可读，但不能据此宣称所有站点都可用。所有源也都有类似的逐站依赖风险。

## 暂时停用

`enabled: false` 的源不再请求，不计入启用源总数，也不进入 `subscribe.json` 和默认入口；地址、停用原因、旧快照与历史仍保留，方便将来恢复。

| 源 | 实测结果 | 处理 |
|---|---|---|
| 欧歌 / `ouge` | 旧 `tv.nxog.top/m/` 为 403；新 `apitv.php?id=3` 为 404；社区给出的几个新多仓域名发生 TLS 主机名不匹配 | 暂停。未降低 TLS 校验或把旧缓存继续当作活跃上游 |
| OK / `ok` | `ok321.top`、`ok213.top` 连接失败，公开 Jihulab 备用入口 401 | 暂停，等待公开可验证的新入口 |
| 巧技 / `qiaoji` | `cdn.qiaoji8.com` 连接失败；`pandown.pro/tvbox/tvbox.json` 的 HTTP、HTTPS 均超时 | 暂停，并纠正原列表“巧记”的名称 |

这些结论是当前网络下的观察，不代表维护者永久停更或其他地区一定无法使用。

## 其他候选

| 候选 | 结果与决定 |
|---|---|
| 高天流云 `gaotianliuyun/gao/master/js.json`、`0827.json` | 配置分别有 298、33 个站点，JAR 和抽查规则可读，可作储备。本轮已有 qist 家庭合集，先避免加入高度重叠的聚合配置 |
| dxawi `https://dxawi.github.io/0/0.json` | 配置 200、48 条站点，但其 Zoho JAR 返回 429；暂不加入 |
| 嗷呜 `cnb.cool/aooooowuuuuu/FreeSpider/.../config` | 此候选配置地址为 404；暂不加入 |

## 为什么修改聚合方式

旧实现把所有配置的 `sites` 拼在一起，再任选一个出现次数最多的顶层 `spider`。实际各源的 `csp_*` 名称可能一样，实现却在不同 JAR；有些配置还依赖自己的 `rules`、全局扩展和过滤项。配置站点数很多时，播放器仍可能缺类或行为不符。

现在 `aggregate.json` 先选择一套**在线、完整**的上游配置，在线都失败才用缓存，再按 `aggregate_prefer` 决定顺序。该配置的 JAR、站点、规则和过滤项一起保留；其他源通过 `subscribe.json` 或各自 `<id>.json` 切换。默认首选 qist 家庭合集，本仓库直播入口继续置顶。

转存独立快照时，会按**最终响应 URL**补全相对资源路径。否则 FongMi 的 `../jar/custom_spider.jar` 会被客户端错误地指向本仓库的 `/jar/`。URL 查询参数中的 `../` 保持原样。

## 直播

本次四个直播上游清单均为 HTTP 200，继续保留：fanmingming/live IPv6、iptv-org 中国、iptv-org 中文语区、Guovin/iptv-api。

没有在本次调研中重新跑全量频道流测速。仓库 2026-11-19 19:12 的既有产物是 **377 频道 / 691 条有效地址**，这属于上一次运行记录，不能理解成本轮重测。IPv6 与地区网络条件依然影响家庭端的实际表现。

## 回归与后续维护

- `python scripts/selftest_sources.py`：7 项离线回归，覆盖图片/Base64/AES、HTML 拒绝、重定向后的资源路径、分块 JAR 签名、坏依赖换地址、完整配置上下文、停用与空订阅缓存保护、在线优先于缓存。
- `python scripts/selftest_status.py`：原状态历史回归，额外隔离 CI 的真实仓库与平台环境变量。
- `python scripts/selftest_live.py`：原直播解析、测速、产物与直播入口注入回归。
- GitHub Actions 和 CNB 都先运行上述测试，再更新产物。依赖增加 `pycryptodome`；后续部署需按 `requirements.txt` 安装。
- 源恢复后清除 `enabled: false`；新增或更换入口后运行 `scripts/update.py`，检查 `status.json`、JAR 和实际客户端，再调整 `aggregate_prefer`。缓存不是永久可用保证。

## 调研依据

- [qist 维护仓库说明](https://github.com/qist/tvbox)：家庭配置、饭太硬和潇洒镜像入口。
- [FongMi 播放器 Decoder.java](https://github.com/FongMi/TV/blob/fongmi/catvod/src/main/java/com/github/catvod/utils/Decoder.java)：图片 Base64、内嵌密钥 AES-CBC 协议及路径处理依据。
- [FongMi 官方示例配置](https://github.com/FongMi/CatVodSpider/blob/main/json/config.json)。
- [小米维护仓库](https://github.com/ggrrttyyiii/CatVodSpider)、[南风维护仓库](https://github.com/yoursmile66/TVBox)、[香雅情维护仓库](https://github.com/xyq254245/xyqonlinerule)、[老刘备维护仓库](https://github.com/liu673cn/box)。
- [社区候选清单](https://github.com/tushen6/Tomorrow)、[近期接口清单](https://github.com/DodgeZhang/tvbox)、[欧歌新接口线索](https://github.com/wuxierj/TVBox)：只用于发现候选地址，是否收录以本次实测为准。
