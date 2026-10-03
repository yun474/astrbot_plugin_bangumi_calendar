# Copyright 2026 NoFizz
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import datetime
import html as html_mod
import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

# 测试通过 plugin_main.httpx.AsyncClient 做 monkeypatch（httpx 是共享模块对象，
# service.py 中的属性查找同样生效），故在此保留导入并在 __all__ 中再导出。
import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .card import HTML_TMPL
from .daily_cache import DailyCache
from .local_renderer import render_local
from .models import WEEKDAY_NAMES, _CACHE_EXPIRE_DAYS, _COVERS_DIR
from .parser import (
    calculate_sleep_time,
    clean_umos,
    filter_items_by_limits,
    get_proxy,
    get_today_items,
    parse_push_time,
    safe_anime_id,
    select_tags,
    sort_items,
)
from .service import cleanup_old_covers, download_covers, fetch_calendar, fetch_subject_details

__all__ = ["httpx"]


# 用户可见文案表：_t() 按语言取模板并格式化，键为文案标识。
# 英文表缺失的键回退中文表；两表都缺失时原样返回键名（仅编程错误时出现）。
_REPLY_CN = {
    "today_failed": "获取新番信息失败，请稍后再试",
    "push_done": "已向 {count} 个目标推送今日新番",
    "status_title": "Bangumi新番日历插件",
    "status_push_time": "推送时间",
    "status_targets": "目标数",
    "status_proxy": "代理",
    "status_direct": "直连",
    "status_next_push": "距离下次推送: {hours}小时{minutes}分钟",
}

_REPLY_EN = {
    "today_failed": "Failed to fetch anime info, please try again later",
    "push_done": "Pushed today's anime to {count} target(s)",
    "status_title": "Bangumi Calendar Plugin",
    "status_push_time": "Push time",
    "status_targets": "Targets",
    "status_proxy": "Proxy",
    "status_direct": "Direct",
    "status_next_push": "Next push in {hours}h {minutes}m",
}


@register(
    "astrbot_plugin_bangumi_calendar",
    "NoFizz, yun474",
    "每日新番放送日历，支持本地渲染与图片缓存推送",
    "1.1.1",
)
class BangumiCalendarPlugin(Star):
    """Bangumi 新番日历定时推送插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化插件：创建封面目录、启动时清理过期缓存、启动每日定时任务。

        Args:
            context: AstrBot 插件上下文。
            config: 插件配置（AstrBot 自动注入）。
        """
        super().__init__(context)
        self.config = config
        self._cache = DailyCache(StarTools.get_data_dir("astrbot_plugin_bangumi_calendar") / "daily", config)
        self._cache.prune(datetime.date.today().isoformat())
        os.makedirs(_COVERS_DIR, exist_ok=True)
        self._cleanup_old_covers()
        # 依赖插件加载时宿主的事件循环已在运行：create_task 必须在运行中的循环内调用
        self._monitoring_task = asyncio.create_task(self._daily_task())
        self._cache_task = asyncio.create_task(self._daily_cache_cleanup())
        logger.info(f"[Bangumi日历] 插件已加载, 推送时间: {self.config.get('push_time', '07:00')}")

    def _parse_push_time(self) -> tuple[int, int]:
        """解析推送时间，支持 H:MM 或 HH:MM 格式，无效回退到默认 07:00"""
        return parse_push_time(self.config.get("push_time", "07:00"))

    def _get_target_umos(self) -> list[str]:
        """获取推送目标UMO列表"""
        return clean_umos(self.config.get("umos", []))

    def _get_proxy(self) -> str | None:
        """获取代理地址：配置优先，环境变量回退，均无则直连"""
        return get_proxy(self.config.get("proxy", ""))

    def _sort_items(self, items: list[dict]) -> list[dict]:
        """根据配置的排序方式对番剧列表排序"""
        return sort_items(
            items,
            self.config.get("sort_by", "score"),
            self.config.get("sort_order", "desc"),
        )

    def _filter_items(self, items: list[dict]) -> list[dict]:
        """根据配置的评分/在看人数下限过滤番剧列表（开关关闭时下限不生效）"""
        return filter_items_by_limits(
            items,
            self.config.get("enable_score_min", False),
            self.config.get("score_min", 0),
            self.config.get("enable_doing_min", False),
            self.config.get("doing_min", 0),
        )

    def _get_cache_path(self, anime_id) -> str:
        """计算封面缓存文件路径。

        Args:
            anime_id: Bangumi 条目 ID（任意类型，安全化后拼接）。

        Returns:
            str: covers 目录下的 jpg 缓存路径。
        """
        return os.path.join(_COVERS_DIR, f"{safe_anime_id(anime_id)}.jpg")

    def _cleanup_old_covers(self):
        """删除超过30天未使用的缓存封面"""
        cleanup_old_covers(_COVERS_DIR, _CACHE_EXPIRE_DAYS)

    def _t(self, key: str, lang: str = "zh", **fmt: object) -> str:
        """按语言取回复文案模板并格式化，缺失时回退中文。

        Args:
            key: 文案标识（_REPLY_CN/_REPLY_EN 的键）。
            lang: 回复语言，zh 中文、en 英文，其他值回退中文。
            **fmt: 模板格式化参数（如 push_done 的 count）。

        Returns:
            str: 格式化后的回复文案。
        """
        template = _REPLY_CN.get(key)
        if lang == "en":
            template = _REPLY_EN.get(key, template)
        if template is None:
            return key
        return template.format(**fmt)

    @filter.command_group("新番")
    def bangumi_cn(self):
        """新番日历命令组（中文）"""
        pass

    @filter.command_group("bangumi")
    def bangumi_en(self):
        """Bangumi calendar command group (English)"""
        pass

    # ---- 中文指令 ----

    @bangumi_cn.command("今日")
    async def today_anime_cn(self, event: AstrMessageEvent):
        """查看今日更新的新番。

        Args:
            event: AstrBot 消息事件。

        Returns:
            AsyncGenerator: 渲染成功时产出图片消息，失败时产出失败提示文本。
        """
        async for result in self._handle_today(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bangumi_cn.command("推送")
    async def manual_push_cn(self, event: AstrMessageEvent):
        """手动推送今日新番到所有目标。

        Args:
            event: AstrBot 消息事件。

        Returns:
            AsyncGenerator: 产出包含推送目标数的文本结果。
        """
        async for result in self._handle_push(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bangumi_cn.command("状态")
    async def check_status_cn(self, event: AstrMessageEvent):
        """查看插件运行状态。

        Args:
            event: AstrBot 消息事件。

        Returns:
            AsyncGenerator: 产出状态信息文本。
        """
        async for result in self._handle_status(event):
            yield result

    # ---- 英文指令 ----

    @bangumi_en.command("today")
    async def today_anime_en(self, event: AstrMessageEvent):
        """View today's anime schedule.

        Args:
            event: AstrBot message event.

        Returns:
            AsyncGenerator: image result on success, failure text otherwise.
        """
        async for result in self._handle_today(event, lang="en"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bangumi_en.command("push")
    async def manual_push_en(self, event: AstrMessageEvent):
        """Push today's anime to all targets.

        Args:
            event: AstrBot message event.

        Returns:
            AsyncGenerator: text result with the pushed target count.
        """
        async for result in self._handle_push(event, lang="en"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bangumi_en.command("status")
    async def check_status_en(self, event: AstrMessageEvent):
        """Check plugin status.

        Args:
            event: AstrBot message event.

        Returns:
            AsyncGenerator: status text result.
        """
        async for result in self._handle_status(event, lang="en"):
            yield result

    async def _handle_today(self, event: AstrMessageEvent, lang: str = "zh") -> AsyncGenerator:
        """Serialize generation and sending so midnight cleanup cannot remove an in-flight image."""
        try:
            async with self._cache.lock:
                message = await self._build_message()
                await event.send(message)
        except Exception:
            logger.exception("[Bangumi日历] 获取或发送新番失败")
            yield event.plain_result(self._t("today_failed", lang))

    async def _handle_push(self, event: AstrMessageEvent, lang: str = "zh") -> AsyncGenerator:
        """中英文「推送」命令共享核心：推送今日新番并报告成功数。

        Args:
            event: AstrBot 消息事件。
            lang: 回复语言，zh 中文、en 英文，默认中文。

        Returns:
            AsyncGenerator: 产出包含推送目标数的文本结果。
        """
        count = await self._push_to_all_groups()
        yield event.plain_result(self._t("push_done", lang, count=count))

    async def _handle_status(self, event: AstrMessageEvent, lang: str = "zh") -> AsyncGenerator:
        """中英文「状态」命令共享核心：产出对应语言的状态信息文本。

        Args:
            event: AstrBot 消息事件。
            lang: 回复语言，zh 中文、en 英文，默认中文。

        Returns:
            AsyncGenerator: 产出状态信息文本。
        """
        yield event.plain_result(self._build_status_text(lang=lang))

    def _build_status_text(self, lang: str = "zh") -> str:
        """构建状态信息文本，标签文案按语言切换。

        Args:
            lang: 回复语言，zh 中文、en 英文，默认中文。

        Returns:
            str: 多行状态文本，数值与格式不随语言变化。
        """
        sleep_time = self._calculate_sleep_time()
        hours = int(sleep_time / 3600)
        minutes = int((sleep_time % 3600) / 60)
        umos = self._get_target_umos()
        proxy = self._get_proxy()
        return (
            f"{self._t('status_title', lang)}\n"
            f"{self._t('status_push_time', lang)}: {self.config.get('push_time', '07:00')}\n"
            f"{self._t('status_targets', lang)}: {len(umos)}\n"
            f"{self._t('status_proxy', lang)}: {proxy or self._t('status_direct', lang)}\n"
            f"渲染: {self.config.get('render_backend', 'remote')}\n"
            f"缓存日期: {datetime.date.today().isoformat()}（服务器时区）\n"
            f"{self._t('status_next_push', lang, hours=hours, minutes=minutes)}"
        )

    async def terminate(self):
        """插件卸载时停止定时任务。

        Returns:
            None: 取消定时任务并等待其退出。
        """
        tasks = [self._monitoring_task, self._cache_task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[Bangumi日历] 插件已卸载")

    async def _fetch_calendar(self) -> list | None:
        """获取Bangumi每周放送日历（自动重试）"""
        return await fetch_calendar(self.config.get("max_retries", 3), self._get_proxy())

    def _get_today_items(self, calendar: list[dict]) -> list[dict]:
        """从日历数据中提取今日番剧"""
        today_weekday = datetime.datetime.now().isoweekday()
        return get_today_items(calendar, today_weekday)

    async def _download_covers(self, items: list[dict]) -> dict[str, str]:
        """下载封面图，支持本地缓存。返回 {原始URL: data URI} 映射"""
        return await download_covers(items, self._get_proxy())

    async def _fetch_day(self, day: datetime.date) -> dict:
        """Build the day's shared snapshot with the original selection and ranking rules."""
        calendar = await self._fetch_calendar()
        if calendar is None:
            raise RuntimeError("Bangumi 日历获取失败")
        items = get_today_items(calendar, day.isoweekday())
        subject_map = await fetch_subject_details(items, self._get_proxy()) if items else {}
        for anime in items:
            rank, _ = subject_map.get(anime.get("id"), (None, []))
            if not isinstance(anime.get("rating"), dict):
                anime["rating"] = {}
            anime["rating"]["rank"] = rank
        items = self._filter_items(self._sort_items(items))
        max_items = self.config.get("max_items", 0)
        if max_items > 0:
            items = items[:max_items]
        data = {
            "date": day.isoformat(),
            "weekday": WEEKDAY_NAMES[day.isoweekday() - 1],
            "count": len(items),
            "items": [],
        }
        for index, anime in enumerate(items, 1):
            images = anime.get("images") or {}
            rating = anime.get("rating") or {}
            _, tags = subject_map.get(anime.get("id"), (None, []))
            data["items"].append(
                {
                    "id": anime.get("id"),
                    "index": index,
                    "name": html_mod.unescape(anime.get("name") or ""),
                    "name_cn": html_mod.unescape(anime.get("name_cn") or ""),
                    "score": rating.get("score", "暂无"),
                    "rank": rating.get("rank"),
                    "doing": (anime.get("collection") or {}).get("doing", 0),
                    "air_date": anime.get("air_date") or "",
                    "cover": images.get("large") or images.get("common") or images.get("medium") or "",
                    "tags": select_tags(tags),
                }
            )
        return data

    async def _build_message(self) -> MessageChain:
        """Read or create today's output. Caller holds the cache lock through sending."""
        while True:
            day = datetime.date.today()
            key = self._cache.key(day.isoformat())
            self._cache.prune(day.isoformat())
            data = self._cache.read(key)
            image_path = self._cache.root / f"{key}.png"
            if data is None:
                # An invalid snapshot must not be paired with an older rendered image.
                image_path.unlink(missing_ok=True)
                data = await self._fetch_day(day)
                if day != datetime.date.today():
                    continue
                self._cache.write(key, data)
            if not image_path.is_file() or image_path.stat().st_size == 0:
                await self._render_image(data, image_path)
            # Rendering may cross midnight; never label yesterday's image as today's.
            if day != datetime.date.today():
                continue
            return MessageChain().file_image(str(image_path)).use_t2i(False)

    async def _render_image(self, data: dict, destination: Path) -> None:
        """Render once and atomically publish a local PNG, using the selected backend."""
        items = [dict(item) for item in data["items"]]
        cover_items = [{"id": item["id"], "images": {"large": item["cover"]}} for item in items]
        cover_map = await self._download_covers(cover_items)
        for item in items:
            item["cover"] = cover_map.get(item["cover"], "")
        template_data = dict(data, items=items)
        temporary = destination.with_suffix(".png.tmp")
        try:
            if self.config.get("render_backend", "remote") == "local":
                await render_local(HTML_TMPL, template_data, temporary, self.config.get("browser_path", "").strip())
            else:
                options = {
                    "type": "png",
                    "full_page": True,
                    "timeout": 60000,
                    "viewport_width": 760,
                    "viewport_height": 800,
                    "device_scale_factor_level": "ultra",
                }
                for attempt in range(3):
                    try:
                        # AstrBot downloads the service result when return_url=False.
                        source = await self.html_render(HTML_TMPL, template_data, return_url=False, options=options)
                        if not source:
                            raise RuntimeError("html_render 返回空结果")
                        await asyncio.to_thread(shutil.copyfile, source, temporary)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(1)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("渲染没有生成有效图片")
            temporary.replace(destination)
            logger.info("[Bangumi日历] 图片已缓存: %s", destination.name)
        finally:
            temporary.unlink(missing_ok=True)

    async def _push_to_all_groups(self) -> int:
        """Push the cached image through AstrBot's adapter."""
        success = 0
        for umo in self._get_target_umos():
            try:
                async with self._cache.lock:
                    message = await self._build_message()
                    sent = await self.context.send_message(umo, message)
                if sent is False:
                    logger.warning("[Bangumi日历] 推送目标不可用: %s", umo)
                    continue
                success += 1
                logger.info("[Bangumi日历] 已推送至 %s", umo)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("[Bangumi日历] 推送至 %s 失败", umo)
        return success

    async def _daily_cache_cleanup(self):
        """Expire old snapshots and images at local midnight, without launching a browser."""
        while True:
            now = datetime.datetime.now()
            midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time())
            await asyncio.sleep(max(1, (midnight - now).total_seconds()))
            try:
                async with self._cache.lock:
                    self._cache.prune(datetime.date.today().isoformat())
            except OSError:
                logger.exception("[Bangumi日历] 清理每日缓存失败")

    def _calculate_sleep_time(self) -> float:
        """计算距离下次推送的秒数"""
        now = datetime.datetime.now()
        hour, minute = self._parse_push_time()
        return calculate_sleep_time(now, hour, minute)

    async def _daily_task(self):
        """定时任务主循环"""
        while True:
            try:
                sleep_time = self._calculate_sleep_time()
                next_push = datetime.datetime.now() + datetime.timedelta(seconds=sleep_time)
                logger.info(f"[Bangumi日历] 下次推送: {next_push.strftime('%Y-%m-%d %H:%M')}")
                await asyncio.sleep(sleep_time)
                umos = self._get_target_umos()
                if umos:
                    await self._push_to_all_groups()
                    # 推送完成后清理过期缓存（文件 IO 卸载到线程，避免阻塞事件循环）
                    await asyncio.to_thread(cleanup_old_covers, _COVERS_DIR, _CACHE_EXPIRE_DAYS)
                else:
                    logger.info("[Bangumi日历] 未配置推送目标，跳过")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[Bangumi日历] 定时任务异常")
                await asyncio.sleep(300)
