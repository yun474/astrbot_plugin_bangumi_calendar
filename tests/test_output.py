"""Behavior checks using AstrBot's real message components (no QQ credentials)."""

import asyncio
import datetime
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO.parent))
main = importlib.import_module(f"{REPO.name}.main")
cache_module = importlib.import_module(f"{REPO.name}.daily_cache")
renderer = importlib.import_module(f"{REPO.name}.local_renderer")


def snapshot(day, count=5):
    return {
        "date": day.isoformat(),
        "weekday": "周六",
        "count": count,
        "items": [
            {
                "id": index,
                "index": index,
                "name": f"Anime {index}",
                "name_cn": f"番剧 {index}",
                "score": 8.0,
                "rank": index,
                "doing": 100,
                "air_date": "2026-10-03",
                "cover": "https://example.com/cover.jpg",
                "tags": ["原创", "TV"],
            }
            for index in range(1, count + 1)
        ],
    }


class OutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.day = datetime.date(2026, 10, 3)
        test = self

        class ClockDate(datetime.date):
            @classmethod
            def today(cls):
                return test.day

        self.clock = patch.object(main, "datetime", SimpleNamespace(date=ClockDate))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        schema = json.loads((REPO / "_conf_schema.json").read_text(encoding="utf-8"))
        self.config = {name: item["default"] for name, item in schema.items()}
        self.plugin = object.__new__(main.BangumiCalendarPlugin)
        self.plugin.config = self.config
        self.plugin._cache = cache_module.DailyCache(Path(self.tmp.name), self.config)
        self.plugin._fetch_day = AsyncMock(side_effect=lambda day: snapshot(day))

        async def render(data, destination):
            await asyncio.sleep(0)
            destination.write_bytes(b"rendered image")

        self.plugin._render_image = AsyncMock(side_effect=render)

    def event(self):
        return SimpleNamespace(
            send=AsyncMock(),
            plain_result=lambda text: text,
        )

    async def query(self, event, **kwargs):
        return [result async for result in self.plugin._handle_today(event, **kwargs)]

    async def test_concurrent_queries_generate_once_and_send_local_files(self):
        events = [self.event() for _ in range(6)]
        replies = await asyncio.gather(*(self.query(event) for event in events))
        self.assertEqual(replies, [[]] * 6)
        self.plugin._fetch_day.assert_awaited_once()
        self.plugin._render_image.assert_awaited_once()
        for event in events:
            event.send.assert_awaited_once()
            message = event.send.call_args.args[0]
            self.assertTrue(message.chain[0].file.startswith("file:///"))
            self.assertFalse(message.use_t2i_)

    async def test_restart_reuses_disk_without_network_or_render(self):
        await self.query(self.event())
        self.plugin._cache = cache_module.DailyCache(Path(self.tmp.name), self.config)
        await self.query(self.event())
        self.plugin._fetch_day.assert_awaited_once()
        self.plugin._render_image.assert_awaited_once()

    async def test_new_day_rebuilds_and_deletes_yesterday(self):
        await self.query(self.event())
        yesterday = self.plugin._cache.key(self.day.isoformat())
        self.day += datetime.timedelta(days=1)
        await self.query(self.event())
        self.assertEqual(self.plugin._render_image.await_count, 2)
        self.assertFalse((Path(self.tmp.name) / f"{yesterday}.png").exists())
        self.assertFalse((Path(self.tmp.name) / f"{yesterday}.json").exists())

    async def test_render_crossing_midnight_rebuilds_before_sending(self):
        async def render(data, destination):
            destination.write_bytes(b"image")
            if self.plugin._render_image.await_count == 1:
                self.day += datetime.timedelta(days=1)

        self.plugin._render_image.side_effect = render
        event = self.event()
        await self.query(event)
        self.assertEqual(self.plugin._render_image.await_count, 2)
        self.assertIn("2026-10-04", event.send.call_args.args[0].chain[0].file)

    async def test_corrupt_snapshot_is_rebuilt_with_its_image(self):
        await self.query(self.event())
        key = self.plugin._cache.key(self.day.isoformat())
        (Path(self.tmp.name) / f"{key}.json").write_text("{broken", encoding="utf-8")
        await self.query(self.event())
        self.assertEqual(self.plugin._fetch_day.await_count, 2)
        self.assertEqual(self.plugin._render_image.await_count, 2)

    async def test_push_uses_adapter_and_reuses_query_image(self):
        await self.query(self.event())
        self.config["umos"] = ["official:GroupMessage:openid"]
        self.plugin.context = SimpleNamespace(
            send_message=AsyncMock(return_value=True),
        )
        with patch.object(main.asyncio, "sleep", new=AsyncMock()):
            self.assertEqual(await self.plugin._push_to_all_groups(), 1)
        message = self.plugin.context.send_message.call_args.args[1]
        self.assertTrue(message.chain[0].file.startswith("file:///"))
        self.plugin._render_image.assert_awaited_once()

    async def test_original_rank_filter_tag_rules_are_used(self):
        self.plugin._fetch_calendar = AsyncMock(
            return_value=[
                {
                    "weekday": {"id": 6},
                    "items": [
                        {"id": 1, "name": "High score", "rating": {"score": 9}, "collection": {"doing": 2}},
                        {"id": 2, "name": "Rank first", "rating": {"score": 7}, "collection": {"doing": 8}},
                        {"id": 3, "name": "Filtered", "rating": {"score": 1}, "collection": {"doing": 1}},
                    ],
                }
            ]
        )
        self.config.update(enable_score_min=True, score_min=5)
        details = {1: (None, []), 2: (50, [{"name": "TV", "count": 10}]), 3: (None, [])}
        with patch.object(main, "fetch_subject_details", new=AsyncMock(return_value=details)):
            data = await main.BangumiCalendarPlugin._fetch_day(self.plugin, self.day)
        self.assertEqual([item["id"] for item in data["items"]], [2, 1])
        self.assertEqual(data["items"][0]["tags"], ["TV"])

    async def test_local_failure_does_not_publish_partial_image(self):
        self.config["render_backend"] = "local"
        self.plugin._download_covers = AsyncMock(return_value={})
        destination = Path(self.tmp.name) / "output.png"

        async def fail(template, data, path, browser_path):
            path.write_bytes(b"partial")
            raise RuntimeError("browser failed")

        with patch.object(main, "render_local", new=AsyncMock(side_effect=fail)):
            with self.assertRaises(RuntimeError):
                await main.BangumiCalendarPlugin._render_image(self.plugin, snapshot(self.day), destination)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_suffix(".png.tmp").exists())

    async def test_remote_renderer_downloads_once_and_keeps_snapshot_urls(self):
        source = Path(self.tmp.name) / "service.png"
        source.write_bytes(b"service image")
        destination = Path(self.tmp.name) / "cached.png"
        self.plugin.html_render = AsyncMock(return_value=str(source))
        self.plugin._download_covers = AsyncMock(
            return_value={"https://example.com/cover.jpg": "data:image/png;base64,abc"}
        )
        data = snapshot(self.day)
        await main.BangumiCalendarPlugin._render_image(self.plugin, data, destination)
        self.assertFalse(self.plugin.html_render.call_args.kwargs["return_url"])
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertTrue(data["items"][0]["cover"].startswith("https://"))

    async def test_prune_only_removes_owned_old_daily_files(self):
        root = Path(self.tmp.name)
        old = root / f"{self.plugin._cache.key('2026-10-02')}.png"
        old.write_bytes(b"old")
        marker = root / "user-note.txt"
        marker.write_text("keep")
        self.plugin._cache.prune(self.day.isoformat())
        self.assertFalse(old.exists())
        self.assertTrue(marker.exists())


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_and_driver_close_on_success_error_and_cancel(self):
        for error in [None, RuntimeError("screenshot failed"), asyncio.CancelledError()]:
            with self.subTest(error=type(error).__name__):
                page = SimpleNamespace(
                    set_default_timeout=Mock(),
                    set_content=AsyncMock(),
                    wait_for_function=AsyncMock(),
                    screenshot=AsyncMock(side_effect=error),
                )
                context = SimpleNamespace(set_offline=AsyncMock(), new_page=AsyncMock(return_value=page))
                browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
                playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
                manager = AsyncMock()
                manager.__aenter__.return_value = playwright
                with patch("playwright.async_api.async_playwright", return_value=manager):
                    if error is None:
                        await renderer.render_local("<p>{{ name }}</p>", {"name": "hello"}, Path("unused.png"))
                    else:
                        with self.assertRaises(type(error)):
                            await renderer.render_local("<p>hello</p>", {}, Path("unused.png"))
                browser.close.assert_awaited_once()
                manager.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
