"""世代管理バックアップ: <backup_dir>/<automation_id>/<timestamp>.json

各レコード: {operation, automation_id, timestamp, before, after}
  before = 操作前の定義（存在しなければ null）, after = 操作後の定義（存在しなければ null）
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 管理 ID の形式と上限はここを唯一の定義とする（2026-11-20 監査対応で統一）。
# 全体最大 100 文字 = "muse_"(5) + 後続 95 文字。validation.py の MAX_ID_LEN=100 と一致させること。
# 以前は後続 80 文字までで、検証器が受け付ける 86〜100 文字の ID がここで NotManaged になっていた。
MANAGED_ID_MAX_LEN = 100
MANAGED_ID_RE = re.compile(r"^muse_[a-z0-9_]{1,95}$")


class BackupStore:
    def __init__(self, root: Path, generations: int = 20):
        self.root = root
        self.generations = max(1, generations)

    def _dir(self, automation_id: str) -> Path:
        if not MANAGED_ID_RE.match(automation_id):  # パストラバーサル防止も兼ねる
            raise ValueError(f"管理対象外の ID です: {automation_id!r}")
        return self.root / automation_id

    def save(
        self, automation_id: str, operation: str, before: Any | None, after: Any | None
    ) -> Path:
        d = self._dir(automation_id)
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        record = {
            "operation": operation,
            "automation_id": automation_id,
            "timestamp": ts,
            "before": before,
            "after": after,
        }
        path = d / f"{ts}.json"
        tmp = d / f".{ts}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        self._prune(d)
        return path

    def _files(self, d: Path) -> list[Path]:
        return sorted(p for p in d.glob("*.json"))

    def _prune(self, d: Path) -> None:
        files = self._files(d)
        for old in files[: max(0, len(files) - self.generations)]:
            old.unlink(missing_ok=True)

    def latest(self, automation_id: str) -> dict | None:
        d = self._dir(automation_id)
        if not d.exists():
            return None
        files = self._files(d)
        if not files:
            return None
        return json.loads(files[-1].read_text(encoding="utf-8"))

    def count(self, automation_id: str) -> int:
        d = self._dir(automation_id)
        return len(self._files(d)) if d.exists() else 0
