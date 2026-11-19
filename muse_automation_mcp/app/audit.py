"""追記専用 JSON Lines 監査ログ。トークン類は記録しない。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_FORBIDDEN_SUBSTR = ("token", "authorization", "password", "secret")


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if any(s in str(k).lower() for s in _FORBIDDEN_SUBSTR) else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        operation: str,
        automation_id: str | None,
        result: str,
        errors: list | None = None,
        detail: dict | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "automation_id": automation_id,
            "result": result,
            "errors": _scrub(errors or []),
            "detail": _scrub(detail or {}),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
