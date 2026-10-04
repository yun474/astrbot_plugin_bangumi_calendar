"""纯逻辑函数：无副作用、无 astrbot 依赖（仅 logger 用于告警），可独立测试。

这些函数接收显式参数，由 main.py 的实例方法读取配置后调用。
"""

# Copyright 2026 NoFizz
# SPDX-License-Identifier: AGPL-3.0-or-later

import datetime
import hashlib
import os

from astrbot.api import logger


def safe_anime_id(raw) -> str:
    """把任意输入规范化为安全的缓存文件名片段。

    Args:
        raw: 原始 anime_id（通常为 int，但配置/API 数据不可信）。

    Returns:
        str: 合法整数转为十进制字符串；否则取 SHA1 摘要前 12 位十六进制。
    """
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return hashlib.sha1(str(raw).encode()).hexdigest()[:12]


def parse_push_time(raw) -> tuple[int, int]:
    """解析推送时间，支持 H:MM 或 HH:MM 格式，无效回退到默认 07:00。

    Args:
        raw: 配置项 push_time 的原始值。

    Returns:
        tuple[int, int]: (小时, 分钟)；解析失败或越界时回退 (7, 0)。
    """
    try:
        h, m = map(int, str(raw).strip().split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
        raise ValueError
    except (ValueError, IndexError):
        logger.warning(f"[Bangumi日历] push_time 格式无效: '{raw}'，使用默认 07:00")
        return 7, 0


def _get_rank(item: dict) -> int:
    """取条目全站排名，0/缺失视为未上榜。

    Args:
        item: 番剧条目。

    Returns:
        int: 排名（1 起）；无 rank 字段或值不大于 0 时返回 0（未上榜）。
    """
    rank = (item.get("rating") or {}).get("rank")
    return rank if isinstance(rank, int) and rank > 0 else 0


def sort_items(items: list[dict], sort_by: str, sort_order: str) -> list[dict]:
    """按指定字段与方向对番剧列表原地排序。

    ``doing`` 按在看人数排序；其余值按评分模式做 Rank 优先排序：
    有全站排名（rating.rank > 0）的条目在前，按排名升序（1 最前）；
    未上榜的在后，按评分排。升序时对称反转：未上榜按评分升序在前，
    有排名的按排名降序在后。

    Args:
        items: 番剧条目列表（原地修改后返回同一对象）。
        sort_by: 排序字段，``doing`` 按在看数，其余按评分（Rank 优先）。
        sort_order: 排序方向，非 ``asc`` 一律按降序处理。

    Returns:
        list[dict]: 排序后的原列表对象。
    """
    reverse = sort_order != "asc"
    if sort_by == "doing":
        items.sort(key=lambda a: (a.get("collection") or {}).get("doing", 0), reverse=reverse)
        return items
    ranked = [it for it in items if _get_rank(it) > 0]
    unranked = [it for it in items if _get_rank(it) == 0]
    if reverse:
        ranked.sort(key=_get_rank)
        unranked.sort(key=lambda a: (a.get("rating") or {}).get("score", 0), reverse=True)
    else:
        unranked.sort(key=lambda a: (a.get("rating") or {}).get("score", 0))
        ranked.sort(key=_get_rank, reverse=True)
    # 降序：有排名在前；升序：未上榜在前（对称反转）
    items[:] = (ranked + unranked) if reverse else (unranked + ranked)
    return items


def filter_items_by_limits(
    items: list[dict],
    enable_score_min: bool,
    score_min: float,
    enable_doing_min: bool,
    doing_min: int,
) -> list[dict]:
    """按评分/在看人数下限过滤番剧，返回过滤后的新列表。

    开启的开关各自生效；两个都开启时条目须同时满足两个下限（AND）。
    缺失的评分/在看人数按 0 参与比较（开启对应下限时会被过滤）。

    Args:
        items: 番剧条目列表。
        enable_score_min: 是否启用评分下限过滤。
        score_min: 评分下限（仅启用时生效）。
        enable_doing_min: 是否启用在看人数下限过滤。
        doing_min: 在看人数下限（仅启用时生效）。

    Returns:
        list[dict]: 过滤后的新列表；开关均关闭时内容与原列表一致。
    """
    result = list(items)
    if enable_score_min:
        result = [it for it in result if ((it.get("rating") or {}).get("score") or 0.0) >= score_min]
    if enable_doing_min:
        result = [it for it in result if ((it.get("collection") or {}).get("doing") or 0) >= doing_min]
    return result


def get_today_items(calendar: list[dict], today_weekday: int) -> list[dict]:
    """从日历数据中提取指定星期（ISO 编号）的番剧条目。

    Args:
        calendar: Bangumi /calendar 返回的每周条目列表。
        today_weekday: 今天 isoweekday()，1=周一，7=周日。

    Returns:
        list[dict]: 当日条目；未匹配到时返回空列表。
    """
    for day in calendar:
        if day.get("weekday", {}).get("id") == today_weekday:
            return day.get("items", [])
    return []


def calculate_sleep_time(now: datetime.datetime, hour: int, minute: int) -> float:
    """计算从 now 到下一个 (hour, minute) 时刻的秒数。

    Args:
        now: 当前时刻（由调用方传入，便于注入固定时钟）。
        hour: 推送小时。
        minute: 推送分钟。

    Returns:
        float: 距离下次推送的秒数；今日时刻已过则跨到次日。
    """
    next_push = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_push <= now:
        next_push += datetime.timedelta(days=1)
    return (next_push - now).total_seconds()


def get_proxy(proxy_config) -> str | None:
    """获取代理地址：配置优先，环境变量回退，均无则直连。

    Args:
        proxy_config: 配置项 proxy 的原始值。

    Returns:
        str | None: 代理地址；无可用代理时返回 None。
    """
    proxy = str(proxy_config).strip()
    if proxy:
        return proxy
    return os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or None


def clean_umos(raw) -> list[str]:
    """清洗推送目标 UMO 列表：去空白、过滤空串、保持顺序去重。

    Args:
        raw: 配置项 umos 的原始值（可迭代）。

    Returns:
        list[str]: 清洗后的目标列表（按首次出现顺序去重）。
    """
    cleaned = [str(u).strip() for u in raw if str(u).strip()]
    return list(dict.fromkeys(cleaned))


# 来源类型 tag：规范名集合 + 常见简写映射（先归一化再匹配）
_SOURCE_ALIASES = {"漫改": "漫画改"}
_SOURCE_TAGS = {"原创", "漫画改", "小说改", "游戏改", "动画改", "影视改"}
# 放送方式 tag：严格匹配，不做别名扩展
_AIRING_TAGS = {"TV", "WEB", "OVA", "剧场版", "动态漫画"}
# 作品题材 tag：白名单（用户指定全表），按 count 降序填充
_GENRE_TAGS = {
    "科幻",
    "喜剧",
    "同人",
    "百合",
    "校园",
    "惊悚",
    "后宫",
    "机战",
    "悬疑",
    "恋爱",
    "奇幻",
    "推理",
    "运动",
    "耽美",
    "音乐",
    "战斗",
    "冒险",
    "萌系",
    "穿越",
    "玄幻",
    "乙女",
    "恐怖",
    "历史",
    "日常",
    "剧情",
    "武侠",
    "美食",
    "职场",
}
# 国家/地区 tag：一律排除，不进入任何位（与题材白名单无交集，双保险防误入）
_COUNTRY_TAGS = {"日本", "国产", "中国", "美国", "韩国", "英国", "法国", "德国"}


def _normalize_tag_name(name: str) -> str:
    """把来源 tag 的常见简写归一化为规范名，其余原样返回。

    Args:
        name: 原始 tag 名。

    Returns:
        str: 规范名（如 漫改→漫画改）。
    """
    return _SOURCE_ALIASES.get(name, name)


def select_tags(raw_tags: list[dict] | None, max_count: int = 5) -> list[str]:
    """筛选番剧官方标签：来源 + 放送 + 题材，按添加人数 count 降序。

    排位固定为 来源(若有) → 放送(若有) → 题材(按 count 降序)，不做二次排序；
    某类缺失就跳过对应位，不强制凑满，总长 ≤ max_count。同名（归一化后）tag
    的 count 合并相加。国家/地区与白名单外的杂项 tag 一律跳过。

    Args:
        raw_tags: Bangumi ``/v0/subjects/{id}`` 返回的 tags 列表
            （``[{name, count}, ...]``，count 为添加人数）。
        max_count: 最多返回的 tag 数，默认 5；小于等于 0 返回空列表。

    Returns:
        list[str]: 筛选后的 tag 名列表；无可用 tag 时为空列表。
    """
    if max_count <= 0:
        return []
    counts: dict[str, int] = {}
    for tag in raw_tags or []:
        if not isinstance(tag, dict):
            continue
        name = _normalize_tag_name((tag.get("name") or "").strip())
        count = tag.get("count")
        if not name or not isinstance(count, int):
            continue
        counts[name] = counts.get(name, 0) + count
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    selected: list[str] = []
    # 第一位：来源类型（count 最大者）
    for name, _ in ranked:
        if name in _SOURCE_TAGS:
            selected.append(name)
            break
    # 第二位：放送方式（count 最大者）
    for name, _ in ranked:
        if name in _AIRING_TAGS:
            selected.append(name)
            break
    # 第三位起：题材白名单按 count 降序填充至 max_count
    for name, _ in ranked:
        if len(selected) >= max_count:
            break
        if name in _GENRE_TAGS and name not in _COUNTRY_TAGS:
            selected.append(name)
    return selected
