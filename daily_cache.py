"""Disk snapshots and images shared by queries and scheduled pushes."""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from .card import HTML_TMPL

_CACHE_FILE = re.compile(r"\d{4}-\d{2}-\d{2}-[a-f0-9]{12}\.(?:json|png)(?:\.tmp)?$")
_CONTENT_SETTINGS = (
    "sort_by",
    "sort_order",
    "max_items",
    "enable_score_min",
    "score_min",
    "enable_doing_min",
    "doing_min",
    "render_backend",
    "browser_path",
)


class DailyCache:
    """Keep complete files only; callers serialize access with ``lock``."""

    def __init__(self, root: Path, config: dict):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.lock = asyncio.Lock()
        self.template_hash = hashlib.sha256(HTML_TMPL.encode()).hexdigest()

    def key(self, day: str) -> str:
        settings = {name: self.config.get(name) for name in _CONTENT_SETTINGS}
        payload = json.dumps([1, self.template_hash, settings], sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
        return f"{day}-{digest}"

    def prune(self, day: str) -> None:
        """Remove only owned daily files from other calendar dates."""
        for path in self.root.iterdir():
            if _CACHE_FILE.fullmatch(path.name) and not path.name.startswith(f"{day}-") and path.is_file():
                path.unlink(missing_ok=True)

    def read(self, key: str) -> dict | None:
        path = self.root / f"{key}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("date") != key[:10] or not isinstance(data.get("items"), list):
                return None
            return data
        except (OSError, ValueError):
            return None

    def write(self, key: str, data: dict) -> None:
        path = self.root / f"{key}.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
