"""mtime ベースの YAML キャッシュ（編集が再起動なしで反映される）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger("muse.yaml")


class YamlFile:
    def __init__(self, path: Path, parse: Callable[[Any], Any], empty: Any):
        self.path = path
        self._parse = parse
        self._empty = empty
        self._mtime: float | None = None
        self._value: Any = empty

    def get(self) -> Any:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._mtime, self._value = None, self._empty
            return self._value
        if mtime == self._mtime:
            return self._value
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            self._value = self._parse(raw)
        except Exception as e:  # 壊れた設定は fail-closed（空扱い）
            log.error("%s を読めません（空として扱います）: %s", self.path, e)
            self._value = self._empty
        self._mtime = mtime
        return self._value
