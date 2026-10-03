# 新番日推

每日整理 Bangumi 当天星期对应的放送日历，生成图片，通过 AstrBot 消息适配器查询或定时推送。

基于 [NoFizz 的原插件](https://github.com/NoFizz/astrbot_plugin_bangumi_calendar) 修改，由 yun474 维护此 fork。保留原卡片样式、数据筛选及推送时间逻辑，增加本地 Playwright 渲染和自然日图片缓存。

## 图片效果

<img src="./docs/screenshots/card.png" width="400" alt="新番日推卡片示例">

卡片包含封面、中日文名、评分、排名、在看人数、首播日期和类型标签。数据来自 Bangumi 周历，不检测视频平台实际更新、集数或临时停播。

## 安装

在 AstrBot 插件管理中，从以下仓库安装：

```
https://github.com/yun474/astrbot_plugin_bangumi_calendar
```

手动安装时，将插件放入 `AstrBot/data/plugins/astrbot_plugin_bangumi_calendar`，在 AstrBot 使用的 Python 环境中安装依赖后重载：

```bash
python -m pip install -r data/plugins/astrbot_plugin_bangumi_calendar/requirements.txt
```

本地渲染还需要安装浏览器：

```bash
python -m playwright install chromium
```

Linux 容器如缺少系统库，可在构建或部署时使用 `python -m playwright install --with-deps chromium`，并安装中文字体（如 Noto CJK）。也可通过 `browser_path` 使用现有 Chrome/Edge；Docker 中填写容器内路径。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `render_backend` | `remote` | `remote` 使用 AstrBot 文转图服务；`local` 使用本地 Playwright |
| `browser_path` | 空 | 本地浏览器可执行文件绝对路径；留空使用 Playwright Chromium |
| `umos` | `[]` | 推送目标 UMO 列表 |
| `push_time` | `07:00` | 每日推送时间，服务器时区，支持 H:MM 或 HH:MM |
| `max_items` | `0` | 最多显示的番剧数，0 不限制 |
| `sort_by` | `score` | `score` 排名优先、未上榜按评分；`doing` 按在看人数 |
| `sort_order` | `desc` | 降序 `desc` 或升序 `asc` |
| `proxy` | 空 | 支持 HTTP/SOCKS5；空时读取环境变量，否则直连 |
| `max_retries` | `3` | API 请求最大尝试次数，最小 1 |
| `enable_score_min` | `false` | 是否启用评分下限 |
| `score_min` | `0` | 评分下限 |
| `enable_doing_min` | `false` | 是否启用在看人数下限 |
| `doing_min` | `0` | 在看人数下限 |

启用本地渲染只需设置 `render_backend=local`。插件不自动下载浏览器，远程渲染不会启动本地浏览器。

UMO 格式为 `平台实例ID:GroupMessage:会话ID`。QQ 官方机器人使用群 OpenID，不是数字 QQ 群号，请从 AstrBot 实际会话复制。查询和主动推送均走 AstrBot 适配器，由适配器上传本地图片；插件不直接调用 botpy。

定时推送沿用原调度逻辑。官机需在线且目标允许主动消息；当前适配器可能需要先收到该会话的消息来识别群聊或频道场景。

## 指令

| 中文 | 英文 | 说明 | 权限 |
| --- | --- | --- | --- |
| `/新番 今日` | `/bangumi today` | 查看今日图片 | 所有人 |
| `/新番 推送` | `/bangumi push` | 推送到全部配置目标 | 管理员 |
| `/新番 状态` | `/bangumi status` | 查看推送时间、目标数及渲染配置 | 管理员 |

## 渲染与缓存

- 图片由原 HTML 模板生成，支持 AstrBot `html_render(return_url=False)` 或本地 Playwright。
- 本地模式使用独立无头浏览器；截图完成、异常或取消后关闭浏览器及 Playwright 驱动。
- 封面预先下载并嵌入 HTML，本地截图阶段不联网。远程模式会将模板、公开番剧数据与封面发送给配置的文转图服务。
- 每日数据和最终 PNG 保存在 `data/plugin_data/astrbot_plugin_bangumi_calendar/daily/`。同一天、同一配置只生成一次；查询、定时推送及重启后复用本地缓存，并发请求也不会重复生成。
- 日期按服务器本地自然日划分。午夜清理旧日期缓存，正在发送的任务完成后再清理；离线时在下次启动补清理。次日首次查询或推送生成当天图片。
- 排序、筛选、数量、渲染后端、浏览器路径或图片模板变化会生成新缓存。当天评分和排名保持快照值。
- 图片先写入临时文件，成功后再发布到缓存；失败不会留下可发送的半成品。
- 封面仍缓存在插件目录 `covers/`，沿用原有 30 天清理逻辑。

数据来源：[Bangumi API](https://bangumi.github.io/api/)，使用 `/calendar` 及 `/v0/subjects/{id}`。

## 验证与排查

本地验证环境：Windows、Python 3.14、AstrBot 4.26.8、Edge。涵盖真实 AstrBot 消息链、本地浏览器、并发缓存、重启复用、跨日和失败清理。尚未连接真实 QQ 账号验证发送。该环境版本不是技术最低版本声明，本次未新增或抬高 `astrbot_version`。

在安装了 AstrBot 与插件依赖的环境中运行：

```bash
python -m unittest discover -s tests -v
```

查询失败会回复提示，推送失败会记录日志并跳过该目标。请先用 `/新番 今日` 检查图片：远程模式检查文转图服务，本地模式检查浏览器、系统库和中文字体。推送时间以服务器时区为准，非法时间回退到 07:00。

## 作者与许可

原作者：[NoFizz](https://github.com/NoFizz)。此 fork 维护者：[yun474](https://github.com/yun474)。版本号暂保留 1.1.1。

遵循原项目 [AGPL-3.0](LICENSE) 许可。
