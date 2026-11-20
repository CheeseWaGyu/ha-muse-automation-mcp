"""mtime ベースの YAML キャッシュ（編集が再起動なしで反映される）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger("muse.yaml")


class YamlFile:
    def __init__(self, path: Path, parse: Callable[[Any], Any], empty: Any,
                 error_value: Any = None):
        """empty: ファイルが無い場合の値。error_value: 読み込み・検証失敗時の値
        （省略時は empty と同じ。policy のように「壊れたら既定に戻すと危険」な
        設定では、呼び出し側が fail-closed な値を渡す）。"""
        self.path = path
        self._parse = parse
        self._empty = empty
        self._error = empty if error_value is None else error_value
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
        except Exception as e:  # 壊れた設定は呼び出し側が決めた fail-closed 値にする
            log.error("%s を読めません（fail-closed 値として扱います）: %s", self.path, e)
            self._value = self._error
        self._mtime = mtime
        return self._value
