"""ヘルパー作成（Phase H1: input_boolean、Phase H2: 単純ヘルパー全般）。

単純ヘルパーは REST config API が無く、WebSocket の StorageCollection コマンドで扱う:
  ws://<HA>/websocket に接続し auth（アクセストークン。アドオン形態では SUPERVISOR_TOKEN）後、
  {"type": "<domain>/list" | "<domain>/create" | "<domain>/delete"} を送る。
  対応ドメインは SUPPORTED_HELPER_DOMAINS（input_* / counter / timer / schedule）。

設計はスクリプト（scripts.py）と同じ形にそろえる:
- 作成は create-only（名前 = muse_ ID で確定。entity_id は input_boolean.<id> で確定する）
- 管理台帳（managed_helpers.json）に作成記録があるものだけを管理対象とし、削除は台帳必須
- 削除は「提案時点の定義」と「台帳の作成時ハッシュ」の両方と照合してから実行する
- バックアップはリソース別（backups/helper/<id>/）
- 作成後のバックアップ／台帳記録までをトランザクションに含め、失敗時は WS 削除でロールバック

entity_id を確定させるため、ヘルパーの表示名（name）は ID と同一に固定する。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .audit import AuditLog
from .backup import MANAGED_ID_RE, BackupStore
from .config import Settings
from .ha_client import HAClient, HAError
from .scripts import _canon

log = logging.getLogger("muse.helpers")

SUPPORTED_HELPER_DOMAINS = (
    "input_boolean", "input_number", "input_text", "input_select",
    "input_datetime", "counter", "timer", "schedule",
)  # H1 は input_boolean のみだった。H2（2026-11-20）で単純ヘルパー全般へ拡張
_ICON_RE = re.compile(r"^mdi:[a-z0-9]+(-[a-z0-9]+)*$")
_UNSET = object()
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DURATION_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")

# ドメインごとの定義キー（id / icon は全ドメイン共通で別扱い）
_DOMAIN_KEYS: dict[str, set[str]] = {
    "input_boolean": set(),
    "input_number": {"min", "max", "step", "initial", "unit_of_measurement", "mode"},
    "input_text": {"min", "max", "pattern", "mode", "initial"},
    "input_select": {"options", "initial"},
    "input_datetime": {"has_date", "has_time", "initial"},
    "counter": {"initial", "step", "minimum", "maximum", "restore"},
    "timer": {"duration", "restore"},
    "schedule": set(_WEEKDAYS),
}


def _num(value: Any) -> Any:
    """数値の正規化: 2.0 のような整数値 float を int にそろえる（HA 側の float 化対策）。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_time_str(value: Any) -> bool:
    """HH:MM:SS（ゼロ埋め厳密）かどうか。"""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, "%H:%M:%S")
    except ValueError:
        return False
    return parsed.strftime("%H:%M:%S") == value


def _valid_date_str(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%d") == value


def _valid_datetime_str(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%d %H:%M:%S") == value


def _pattern_risk(pattern: str) -> str | None:
    """破滅的バックトラックの代表形を構造的に検出する（H2 再監査 P2）。

    完全な ReDoS 検出ではないが、古典的な指数爆発パターン（入れ子の量指定子と
    後方参照）を作成時点で拒否する。パースできない場合は None を返し、
    コンパイル可否の検査側で別途エラーにする。
    """
    try:
        from re import _parser as sre
    except ImportError:  # pragma: no cover
        try:
            import sre_parse as sre  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        tree = sre.parse(pattern)
    except re.error:
        return None

    def contains_repeat(node: Any) -> bool:
        if isinstance(node, sre.SubPattern):
            return any(contains_repeat(item) for item in node)
        if isinstance(node, tuple):
            if node and node[0] in (sre.MAX_REPEAT, sre.MIN_REPEAT):
                return True
            return any(contains_repeat(x) for x in node[1:])
        if isinstance(node, list):
            return any(contains_repeat(x) for x in node)
        return False

    def walk(node: Any) -> str | None:
        if isinstance(node, sre.SubPattern):
            for item in node:
                found = walk(item)
                if found:
                    return found
            return None
        if isinstance(node, tuple):
            if not node:
                return None
            op = node[0]
            if op is sre.GROUPREF:
                return "後方参照（\\1 など）は使えません"
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT) and len(node) > 1:
                spec = node[1]
                if isinstance(spec, tuple) and len(spec) > 2 \
                        and contains_repeat(spec[2]):
                    return "入れ子の量指定子（(a+)+ のような形）は使えません"
            for x in node[1:]:
                found = walk(x)
                if found:
                    return found
            return None
        if isinstance(node, list):
            for x in node:
                found = walk(x)
                if found:
                    return found
        return None

    return walk(tree)


def _canonical_fields(domain: str, raw: dict) -> dict:
    """ドメイン固有フィールドの正規形（作成時正規化と実体からの読み戻しで共用する）。

    既定値の補完と数値の正規化を両経路で同一に行うことで、HA が既定値を保存するか
    どうかに依らず定義ハッシュが安定する。raw は検証済み（または HA の実体）前提。
    """
    g = raw.get
    if domain == "input_number":
        out: dict[str, Any] = {"min": _num(g("min", 0)), "max": _num(g("max", 100)),
                               "step": _num(g("step", 1)), "mode": g("mode", "slider")}
        if g("initial") is not None:
            out["initial"] = _num(g("initial"))
        if g("unit_of_measurement") is not None:
            out["unit_of_measurement"] = g("unit_of_measurement")
        return out
    if domain == "input_text":
        out = {"min": g("min", 0), "max": g("max", 100), "mode": g("mode", "text")}
        if g("pattern") is not None:
            out["pattern"] = g("pattern")
        if g("initial") is not None:
            out["initial"] = g("initial")
        return out
    if domain == "input_select":
        opts = g("options")
        out = {"options": list(opts) if isinstance(opts, (list, tuple)) else []}
        if g("initial") is not None:
            out["initial"] = g("initial")
        return out
    if domain == "input_datetime":
        out = {"has_date": bool(g("has_date", False)), "has_time": bool(g("has_time", False))}
        if g("initial") is not None:
            out["initial"] = g("initial")
        return out
    if domain == "counter":
        out = {"initial": g("initial", 0), "step": g("step", 1),
               "restore": bool(g("restore", True))}
        if g("minimum") is not None:
            out["minimum"] = g("minimum")
        if g("maximum") is not None:
            out["maximum"] = g("maximum")
        return out
    if domain == "timer":
        return {"duration": g("duration"), "restore": bool(g("restore", False))}
    if domain == "schedule":
        out = {}
        for day in _WEEKDAYS:
            ranges = g(day)
            if isinstance(ranges, list):
                # 防御的に、from/to がともに文字列の dict だけを対象にする
                # （iterable でない値や型の混在でも例外にしない。H2 再監査 P2 低）
                clean = sorted(
                    ({"from": r["from"], "to": r["to"]} for r in ranges
                     if isinstance(r, dict) and isinstance(r.get("from"), str)
                     and isinstance(r.get("to"), str)),
                    key=lambda r: r["from"])
                if clean:
                    out[day] = clean
        return out
    return {}


def _validate_fields(domain: str, d: dict, err: Any) -> None:
    """ドメイン固有フィールドの型・範囲チェック。err(code, message) に積む。"""
    if domain == "input_number":
        for key in ("min", "max", "step", "initial"):
            if key in d and not _is_number(d[key]):
                err("invalid_field", f"{key} は数値で指定してください: {d[key]!r}")
        mn, mx = d.get("min", 0), d.get("max", 100)
        if _is_number(mn) and _is_number(mx) and not mn < mx:
            err("invalid_range", f"min は max より小さくしてください: min={mn!r}, max={mx!r}")
        if _is_number(d.get("step")) and not d["step"] > 0:
            err("invalid_field", f"step は 0 より大きくしてください: {d['step']!r}")
        if _is_number(d.get("initial")) and _is_number(mn) and _is_number(mx) \
                and not mn <= d["initial"] <= mx:
            err("invalid_range", f"initial は min〜max の範囲で指定してください: {d['initial']!r}")
        if "unit_of_measurement" in d and (not isinstance(d["unit_of_measurement"], str)
                                          or len(d["unit_of_measurement"]) > 16):
            err("invalid_field", "unit_of_measurement は16文字までの文字列で指定してください")
        if "mode" in d and d["mode"] not in ("slider", "box"):
            err("invalid_field", f"mode は slider / box のいずれかです: {d['mode']!r}")
    elif domain == "input_text":
        for key in ("min", "max"):
            if key in d and not _is_int(d[key]):
                err("invalid_field", f"{key} は整数で指定してください: {d[key]!r}")
        mn, mx = d.get("min", 0), d.get("max", 100)
        if _is_int(mn) and not 0 <= mn <= 255:
            err("invalid_range", f"min は 0〜255 です: {mn!r}")
        if _is_int(mx) and not 1 <= mx <= 255:
            err("invalid_range", f"max は 1〜255 です: {mx!r}")
        if _is_int(mn) and _is_int(mx) and mn > mx:
            err("invalid_range", f"min は max 以下にしてください: min={mn!r}, max={mx!r}")
        if "pattern" in d:
            if not isinstance(d["pattern"], str):
                err("invalid_field", "pattern は文字列（正規表現）で指定してください")
            elif len(d["pattern"]) > 200:
                # 過度に複雑な正規表現が HA 側の照合を重くしないよう長さを制限（H2 監査の補足）
                err("invalid_field", "pattern は200文字までです")
            else:
                try:
                    re.compile(d["pattern"])
                except re.error as e:
                    err("invalid_field", f"pattern が正規表現として不正です: {e}")
                else:
                    risk = _pattern_risk(d["pattern"])
                    if risk:
                        err("invalid_field", f"pattern が複雑すぎます: {risk}")
        if "mode" in d and d["mode"] not in ("text", "password"):
            err("invalid_field", f"mode は text / password のいずれかです: {d['mode']!r}")
        if "initial" in d:
            if not isinstance(d["initial"], str):
                err("invalid_field", "initial は文字列で指定してください")
            elif _is_int(mx) and len(d["initial"]) > mx:
                err("invalid_range", f"initial が max（{mx}文字）を超えています")
            elif _is_int(mn) and mn > 0 and len(d["initial"]) < mn:
                err("invalid_range", f"initial が min（{mn}文字）未満です")
    elif domain == "input_select":
        options = d.get("options")
        if options is None:
            err("missing_field", "options は必須です（選択肢の配列）")
        elif not isinstance(options, list) or not options:
            err("invalid_field", "options は1件以上の配列で指定してください")
        elif len(options) > 100:
            err("invalid_field", "options は100件までです")
        elif any(not isinstance(o, str) or not o or len(o) > 255 for o in options):
            err("invalid_field", "options の各要素は1〜255文字の文字列にしてください")
        elif len(set(options)) != len(options):
            err("invalid_field", "options に重複があります")
        if "initial" in d and isinstance(options, list) and d["initial"] not in options:
            err("invalid_range", f"initial は options のいずれかにしてください: {d['initial']!r}")
    elif domain == "input_datetime":
        for key in ("has_date", "has_time"):
            if key in d and not isinstance(d[key], bool):
                err("invalid_field", f"{key} は真偽値で指定してください: {d[key]!r}")
        has_date, has_time = d.get("has_date", False), d.get("has_time", False)
        if has_date is False and has_time is False:
            err("invalid_field", "has_date / has_time の少なくとも一方を true にしてください")
        if "initial" in d:
            if has_date and has_time:
                ok, fmt = _valid_datetime_str(d["initial"]), "YYYY-MM-DD HH:MM:SS"
            elif has_date:
                ok, fmt = _valid_date_str(d["initial"]), "YYYY-MM-DD"
            elif has_time:
                ok, fmt = _valid_time_str(d["initial"]), "HH:MM:SS"
            else:
                ok, fmt = False, ""
            if not ok:
                err("invalid_field", f"initial は {fmt} 形式で指定してください: {d['initial']!r}")
    elif domain == "counter":
        for key in ("initial", "step", "minimum", "maximum"):
            if key in d and not _is_int(d[key]):
                err("invalid_field", f"{key} は整数で指定してください: {d[key]!r}")
        if _is_int(d.get("initial")) and d["initial"] < 0:
            err("invalid_range", f"initial は 0 以上です: {d['initial']!r}")
        if _is_int(d.get("step")) and not d["step"] > 0:
            err("invalid_field", f"step は 0 より大きくしてください: {d['step']!r}")
        if _is_int(d.get("minimum")) and d["minimum"] < 0:
            err("invalid_range", f"minimum は 0 以上です: {d['minimum']!r}")
        if _is_int(d.get("minimum")) and _is_int(d.get("maximum")) \
                and d["minimum"] > d["maximum"]:
            err("invalid_range", "minimum は maximum 以下にしてください")
        if _is_int(d.get("initial")):
            if _is_int(d.get("minimum")) and d["initial"] < d["minimum"]:
                err("invalid_range", "initial が minimum 未満です")
            if _is_int(d.get("maximum")) and d["initial"] > d["maximum"]:
                err("invalid_range", "initial が maximum を超えています")
        if "restore" in d and not isinstance(d["restore"], bool):
            err("invalid_field", f"restore は真偽値で指定してください: {d['restore']!r}")
    elif domain == "timer":
        duration = d.get("duration")
        if duration is None:
            err("missing_field", "duration は必須です（H:MM:SS 形式）")
        elif not isinstance(duration, str) or not _DURATION_RE.match(duration):
            err("invalid_field", f"duration は H:MM:SS 形式で指定してください: {duration!r}")
        else:
            h, m, s = (int(p) for p in duration.split(":"))
            if h * 3600 + m * 60 + s <= 0:
                err("invalid_range", "duration は 0 より大きくしてください")
        if "restore" in d and not isinstance(d["restore"], bool):
            err("invalid_field", f"restore は真偽値で指定してください: {d['restore']!r}")
    elif domain == "schedule":
        seen = 0
        for day in _WEEKDAYS:
            if day not in d:
                continue
            ranges = d[day]
            if not isinstance(ranges, list):
                err("invalid_field", f"{day} は時間帯の配列で指定してください")
                continue
            if len(ranges) > 8:
                err("invalid_field", f"{day} の時間帯は8件までです")
            prev_to = ""
            for r in sorted((r for r in ranges if isinstance(r, dict)),
                            key=lambda r: str(r.get("from", ""))):
                seen += 1
                if set(r) - {"from", "to"} or not _valid_time_str(r.get("from")) \
                        or not _valid_time_str(r.get("to")):
                    err("invalid_field",
                        f"{day} の各時間帯は from / to（HH:MM:SS）で指定してください: {r!r}")
                    continue
                if not r["from"] < r["to"]:
                    err("invalid_range", f"{day} の時間帯は from < to にしてください: {r!r}")
                if prev_to and r["from"] < prev_to:
                    err("invalid_range", f"{day} の時間帯が重なっています: {r!r}")
                prev_to = max(prev_to, r["to"])
            if any(not isinstance(r, dict) for r in ranges):
                err("invalid_field", f"{day} の各時間帯はオブジェクトで指定してください")
        if seen == 0:
            err("missing_field", "少なくとも1つの曜日・時間帯を指定してください")


def websocket_url(ha_api_base: str) -> str:
    base = ha_api_base.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    if base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    return base + "/websocket"


def helper_definition_hash(entity_id: str, definition: dict) -> str:
    payload = json.dumps({"entity_id": entity_id, "definition": _canon(definition)},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized(domain: str, definition: dict) -> dict:
    """保存・比較に使う正規化済み定義（id / icon + ドメイン固有フィールド。name は id から導出）。"""
    out: dict[str, Any] = {"id": definition["id"]}
    if definition.get("icon"):
        out["icon"] = definition["icon"]
    out.update(_canonical_fields(domain, definition))
    return out


def _from_item(domain: str, item: dict) -> dict:
    """StorageCollection の項目（name/icon + ドメイン固有フィールド）から正規化済み定義へ。"""
    out: dict[str, Any] = {"id": item["name"]}
    if item.get("icon"):
        out["icon"] = item["icon"]
    out.update(_canonical_fields(domain, item))
    return out


class HelperCollection(Protocol):
    """StorageCollection へのアクセス（実 WS 実装とテスト用フェイクが実装する）。"""

    async def list_items(self, domain: str) -> list[dict]: ...
    async def create_item(self, domain: str, data: dict) -> dict: ...
    async def delete_item(self, domain: str, item_id: str) -> None: ...


class WSHelperCollection:
    """実 WebSocket 実装。接続は遅延（初回利用時）で、呼び出しごとに張って閉じる。"""

    def __init__(self, url: str, token: str):
        self._url = url
        self._token = token
        self._seq = 0

    async def _call(self, msg_type: str, **fields: Any) -> Any:
        try:
            from websockets.asyncio.client import connect
        except ImportError as e:  # pragma: no cover
            raise HAError(f"websockets パッケージがありません: {e}") from e
        try:
            async with connect(self._url, open_timeout=10) as ws:
                greeting = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if greeting.get("type") != "auth_required":
                    raise HAError(f"HA WebSocket の挨拶が想定外です: {greeting.get('type')!r}")
                await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if auth.get("type") != "auth_ok":
                    raise HAError("HA WebSocket の認証に失敗しました")
                self._seq += 1
                mid = self._seq
                await ws.send(json.dumps({"id": mid, "type": msg_type, **fields}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if msg.get("id") != mid or msg.get("type") != "result":
                        continue
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        raise HAError(
                            f"{msg_type} が失敗しました: {err.get('code', '')} {err.get('message', '')}".strip())
                    return msg.get("result")
        except HAError:
            raise
        except Exception as e:
            raise HAError(f"HA WebSocket への接続/呼び出しに失敗しました ({msg_type}): {e}") from e

    async def list_items(self, domain: str) -> list[dict]:
        result = await self._call(f"{domain}/list")
        return result if isinstance(result, list) else []

    async def create_item(self, domain: str, data: dict) -> dict:
        result = await self._call(f"{domain}/create", **data)
        return result if isinstance(result, dict) else {}

    async def delete_item(self, domain: str, item_id: str) -> None:
        await self._call(f"{domain}/delete", **{f"{domain}_id": item_id})


# ---------------------------------------------------------------- 台帳
class HelperLedger:
    """このアドオンが作成したヘルパーの台帳（managed_helpers.json）。"""

    def __init__(self, path: Path):
        self._path = path

    def _load(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def get(self, entity_id: str) -> dict | None:
        return self._load().get(entity_id)

    def all(self) -> dict:
        return self._load()

    def register(self, domain: str, definition: dict) -> None:
        entity_id = f"{domain}.{definition['id']}"
        normalized = _normalized(domain, definition)
        data = self._load()
        data[entity_id] = {
            "domain": domain,
            "helper_id": definition["id"],
            "definition": normalized,
            "definition_hash": helper_definition_hash(entity_id, normalized),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)

    def unregister(self, entity_id: str) -> None:
        data = self._load()
        data.pop(entity_id, None)
        self._save(data)


# ---------------------------------------------------------------- 検証
@dataclass
class HelperValidationResult:
    valid: bool
    helper_id: str | None = None
    entity_id: str | None = None
    domain: str = "input_boolean"
    normalized: dict | None = None
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"valid": self.valid, "errors": self.errors,
                               "warnings": self.warnings}
        if self.helper_id:
            out["helper_id"] = self.helper_id
            out["entity_id"] = self.entity_id
            out["domain"] = self.domain
        if self.normalized is not None:
            out["normalized"] = self.normalized
        return out


def _generate_helper_id() -> str:
    return f"muse_helper_{secrets.token_hex(3)}"


class HelperValidator:
    def __init__(self, ha: HAClient, collection: HelperCollection):
        self._ha = ha
        self._collection = collection

    async def validate(self, definition: Any, domain: str = "input_boolean") -> HelperValidationResult:
        errors: list[dict] = []
        warnings: list[dict] = []

        def err(code: str, message: str) -> None:
            errors.append({"code": code, "path": "", "message": message})

        if domain not in SUPPORTED_HELPER_DOMAINS:
            err("helper_domain_not_supported",
                f"対応するヘルパーは {list(SUPPORTED_HELPER_DOMAINS)} のみです: {domain!r}")
            return HelperValidationResult(False, domain=domain, errors=errors)
        if not isinstance(definition, dict):
            err("invalid_helper", "ヘルパー定義はオブジェクトである必要があります")
            return HelperValidationResult(False, domain=domain, errors=errors)
        allowed = {"id", "icon"} | _DOMAIN_KEYS[domain]
        unknown = sorted(set(definition) - allowed)
        if unknown:
            err("unknown_keys", f"未対応のキーがあります: {unknown}（{domain} で対応: {sorted(allowed)}。"
                                "表示名は ID と同一になります）")
        icon = definition.get("icon")
        if icon is not None and (not isinstance(icon, str) or not _ICON_RE.match(icon)):
            err("invalid_icon", f"icon は mdi: 形式で指定してください: {icon!r}")
        helper_id = definition.get("id")
        if helper_id is None:
            helper_id = _generate_helper_id()
        if not isinstance(helper_id, str) or not MANAGED_ID_RE.match(helper_id):
            err("invalid_id", "id は muse_ で始まる英小文字・数字・アンダースコア（全体100文字まで）にしてください")
            return HelperValidationResult(False, domain=domain, errors=errors, warnings=warnings)
        if not errors:
            _validate_fields(domain, definition, err)
        entity_id = f"{domain}.{helper_id}"
        # 重複チェック: コレクション内に同名があれば create-only 違反、state があれば使用中
        try:
            items = await self._collection.list_items(domain)
            if any(it.get("name") == helper_id for it in items):
                err("already_exists", f"同名のヘルパーが既に存在します: {entity_id}（上書きはしません）")
        except HAError as e:
            err("ha_error", str(e))
        try:
            states = await self._ha.get_states()
            if any(s.get("entity_id") == entity_id for s in states):
                err("already_exists", f"entity が既に存在します: {entity_id}（上書きはしません）")
        except HAError as e:
            err("ha_error", str(e))
        merged = {k: definition[k] for k in allowed if k in definition}
        merged["id"] = helper_id
        if icon is None:
            merged.pop("icon", None)
        # 検証エラーがある場合は正規化に進まない（不正な schedule 等で正規化が
        # 例外を投げ、検証結果自体を返せなくなるのを防ぐ。2026-11-20 H2 監査 P2）
        normalized = _normalized(domain, merged) if not errors else None
        return HelperValidationResult(not errors, helper_id=helper_id, entity_id=entity_id,
                                      domain=domain,
                                      normalized=normalized,
                                      errors=errors, warnings=warnings)


# ---------------------------------------------------------------- 読み取り系
class HelperManager:
    def __init__(self, ha: HAClient, collection: HelperCollection,
                 validator: HelperValidator, ledger: HelperLedger | None = None):
        self._ha = ha
        self._collection = collection
        self._validator = validator
        self._ledger = ledger

    async def preview(self, definition: Any, domain: str = "input_boolean") -> dict:
        v = await self._validator.validate(definition, domain)
        out = v.to_dict()
        if v.valid:
            out["predicted_entity_id"] = v.entity_id
        return out

    async def list_managed(self) -> dict:
        entries = self._ledger.all() if self._ledger else {}
        states = await self._ha.get_states()
        state_by_id = {s.get("entity_id"): s for s in states}
        items_by_domain: dict[str, dict[str, dict] | None] = {}
        errors: list[dict] = []
        for domain in SUPPORTED_HELPER_DOMAINS:
            try:
                items = await self._collection.list_items(domain)
                items_by_domain[domain] = {it.get("name"): it for it in items}
            except HAError as e:
                # 取得失敗を「0件」と区別して返す（2026-11-20 H1 監査 P2）。
                # このドメインの管理判定はできないので、台帳分は一覧に出さず errors で知らせる。
                items_by_domain[domain] = None
                errors.append({"code": "ha_error", "domain": domain,
                               "message": str(e)[:300]})
        out: list[dict] = []
        for entity_id, entry in sorted(entries.items()):
            domain = entry.get("domain", "input_boolean")
            by_name = items_by_domain.get(domain)
            if by_name is None:
                continue  # 取得できていないドメインは判定不能（errors 参照）
            item = by_name.get(entry.get("helper_id"))
            state = state_by_id.get(entity_id)
            if item is None or state is None:
                continue  # 台帳にあっても実体が無ければ管理対象に出さない
            current = _from_item(domain, item)
            state_value = state.get("state")
            if domain == "input_text":
                # input_text の現在値は秘密情報になり得る（mode=password や個人情報の
                # 手入力）ため、一覧では値を返さない（2026-11-20 H2 監査 P1）。
                # 存在確認は state 自体の有無でできているので、マスクしても管理判定に影響しない。
                state_value = "***" if state_value is not None else None
            out.append({
                "helper_id": entry["helper_id"], "entity_id": entity_id, "domain": domain,
                "state": state_value,
                "created_at": entry.get("created_at"),
                "definition_changed_since_creation":
                    helper_definition_hash(entity_id, current) != entry.get("definition_hash"),
            })
        managed_ids = {o["entity_id"] for o in out}
        unverified = sum(
            1 for eid in state_by_id
            if any(eid.startswith(f"{d}.muse_") for d in SUPPORTED_HELPER_DOMAINS)
            and eid not in managed_ids)
        result: dict[str, Any] = {"count": len(out), "helpers": out,
                                  "unverified_count": unverified}
        if errors:
            result["errors"] = errors
        return result


# ---------------------------------------------------------------- 作成・削除（承認後に呼ばれる）
class HelperDeployer:
    """承認後の作成・削除。スクリプトと同じ安全策（単一ロック、台帳、照合、記録込みトランザクション）。"""

    def __init__(self, settings: Settings, ha: HAClient, collection: HelperCollection,
                 validator: HelperValidator, audit: AuditLog, backups: BackupStore,
                 ledger: HelperLedger | None = None):
        self._s = settings
        self._ha = ha
        self._collection = collection
        self._validator = validator
        self._audit = audit
        self._backups = backups
        self._ledger = ledger
        self._lock = asyncio.Lock()

    def ledger_entry(self, entity_id: str) -> dict | None:
        return self._ledger.get(entity_id) if self._ledger else None

    async def current_definition(self, entity_id: str) -> dict | None:
        domain, _, helper_id = entity_id.partition(".")
        if domain not in SUPPORTED_HELPER_DOMAINS or not helper_id:
            return None
        items = await self._collection.list_items(domain)
        for it in items:
            if it.get("name") == helper_id:
                return _from_item(domain, it)
        return None

    async def _wait_state(self, entity_id: str, want_present: bool) -> bool:
        deadline = asyncio.get_event_loop().time() + max(0.0, self._s.entity_wait_seconds)
        while True:
            states = await self._ha.get_states()
            present = any(s.get("entity_id") == entity_id for s in states)
            if present == want_present:
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(min(0.25, max(0.01, self._s.entity_wait_interval)))

    async def _rollback_create(self, domain: str, helper_id: str,
                               item_id: str | None) -> tuple[str, str]:
        """作成の補償削除（2026-11-20 H1 監査 P1 + 所有権確認の追加監査対応）。

        補償削除は「今回の作成で作られた実体」と証明できる場合だけ行う。証明には
        作成応答に含まれる StorageCollection の item ID を使う:
        - item_id が無い（作成応答を失った／応答に id が含まれない）→ 同名の実体があっても
          今回の作成物かどうか分からないため自動削除しない（status "unknown"。
          「他者のものと確認できた」わけではないので foreign にはしない）
        - item_id があり、再照会した同名実体の item ID と明確に異なる → 今回の作成物では
          ないと確認できるため触らない（status "foreign"）
        戻り値 (status, 詳細): "gone" = 今回の作成物は残っていない、"present" = 今回の
        作成物が残っている（補償削除できなかった）、"foreign" = 同名の実体は今回の作成物で
        ないと確認できたため残してある、"unknown" = 結果を確認できない（再照会不能、
        または item ID 不明で所有権を判定できない）。
        """
        try:
            items = await self._collection.list_items(domain)
        except HAError as e:
            return "unknown", f"実体の再照会に失敗しました: {e}"
        target = [it for it in items if it.get("name") == helper_id]
        if not target:
            return "gone", "実体は存在しませんでした"
        if item_id is None:
            return "unknown", ("同名の実体が存在しますが、作成応答から item ID を取得できないため"
                               "今回の作成物か特定できず、自動削除しませんでした（結果不明）")
        if target[0].get("id") != item_id:
            return "foreign", ("同名の実体は今回の作成物ではありません（item ID が一致しない）ため、"
                               "自動削除しませんでした")
        delete_note = ""
        try:
            await self._collection.delete_item(domain, item_id)
        except HAError as e:
            delete_note = f"補償削除の応答は不明です: {e}。"
        try:
            items = await self._collection.list_items(domain)
        except HAError as e:
            return "unknown", delete_note + f"補償削除後の確認に失敗しました: {e}"
        if any(it.get("id") == item_id for it in items):
            return "present", delete_note + "補償削除後も今回の作成物が残っています"
        return "gone", delete_note + "補償削除で今回の作成物が消えました"

    async def deploy(self, definition: dict, domain: str = "input_boolean") -> dict:
        async with self._lock:
            return await self._deploy(definition, domain)

    async def _deploy(self, definition: dict, domain: str) -> dict:
        steps: list[dict] = []

        def step(name: str, ok: bool, detail: str = "") -> None:
            steps.append({"step": name, "ok": ok, "detail": detail})

        def fail(code: str, message: str, **kw: Any) -> dict:
            errors = [{"code": code, "path": "", "message": message}]
            self._audit.write("helper_deploy", str(definition.get("id")), "failed",
                              errors=errors, detail={"steps": steps, **kw})
            return {"deployed": False, "errors": errors, "steps": steps, **kw}

        v = await self._validator.validate(definition, domain)
        step("validate", v.valid)
        if not v.valid:
            return {"deployed": False, "errors": v.errors, "warnings": v.warnings, "steps": steps}
        hid = v.helper_id
        entity_id = v.entity_id
        normalized = v.normalized
        payload: dict[str, Any] = {"name": hid}
        payload.update({k: val for k, val in normalized.items() if k != "id"})
        # 作成コマンドの応答が失われても HA 側では作成済みのことがある（監査 P1）。
        # 例外は「結果不明」として扱い、実体を再照会してから結論を出す。ただし応答を
        # 失った場合は作成物の item ID を証明できないため、補償削除は行わない（追加監査）。
        create_error: HAError | None = None
        created_item_id: str | None = None
        try:
            created = await self._collection.create_item(domain, payload)
            created_item_id = created.get("id") if isinstance(created, dict) else None
            step("create", True)
        except HAError as e:
            create_error = e
            step("create", False, str(e)[:300])
        if create_error is not None:
            status, note = await self._rollback_create(domain, hid, None)
            step("rollback_check", status == "gone", note)
            if status == "gone":
                return fail("create_failed_rolled_back",
                            f"作成コマンドが失敗しました: {create_error}。{note}ため、台帳には登録されていません",
                            rolled_back=True, entity_id=entity_id)
            # 同名実体があっても所有権を判定できない場合（unknown）も、再照会自体が
            # できない場合も、成功・失敗を断定せず手動確認に回す
            return fail("create_outcome_unknown",
                        f"作成の結果を確認できませんでした: {create_error}。{note}。"
                        f"{entity_id} が今回の作成で作られたものなら台帳に未登録のまま残っています。"
                        "HA 側で確認し、不要なら手動削除してください（台帳には未登録です）",
                        entity_id=entity_id, outcome_unknown=True)
        # state の出現を確認する。確認中の通信エラーも例外で抜けず、曖昧ケースとして扱う（監査 P1）
        verify_error: HAError | None = None
        try:
            appeared = await self._wait_state(entity_id, True)
        except HAError as e:
            appeared, verify_error = False, e
        step("entity_appears", appeared)
        if verify_error is not None or not appeared:
            status, note = await self._rollback_create(domain, hid, created_item_id)
            step("rollback_delete", status == "gone", note)
            cause = (f"作成後の状態確認で通信エラーが発生しました: {verify_error}"
                     if verify_error is not None else "作成後に entity が出現しませんでした")
            if status == "gone":
                return fail("verification_failed",
                            cause + f"。{note}ため、作成は取り消されています",
                            rolled_back=True, entity_id=entity_id)
            if status == "present":
                return fail("verification_ambiguous_orphaned",
                            cause + f"。{entity_id} が台帳に未登録のまま HA に残っています（{note}）。"
                                    "HA 側で手動削除してください",
                            rolled_back=False, entity_id=entity_id, manual_delete_required=True)
            if status == "foreign":
                # 今回の作成物は残っていない。同名の別実体は他者のものの可能性があるため残した
                return fail("verification_failed",
                            cause + f"。今回の作成物は残っていません。{note}",
                            rolled_back=True, entity_id=entity_id,
                            warnings=[f"同名のヘルパー（{entity_id}）が HA に存在しますが、今回の作成物では"
                                      "ないため残してあります。HA 側で確認してください"])
            return fail("verification_outcome_unknown",
                        cause + f"。作成結果を確認できません（{note}）。{entity_id} が HA に存在する"
                                "可能性があります。HA 側で確認し、存在すれば手動削除してください（台帳には未登録です）",
                        entity_id=entity_id, outcome_unknown=True)
        # バックアップ・台帳記録までをトランザクションに含める（失敗時はロールバック）
        try:
            self._backups.save(hid, "helper_deploy", before=None,
                               after={"entity_id": entity_id, **normalized})
            if self._ledger is not None:
                self._ledger.register(domain, normalized)
            step("record", True)
        except Exception as e:
            step("record", False, str(e)[:300])
            try:
                self._backups.save(hid, "helper_deploy_failed",
                                   before={"entity_id": entity_id, **normalized}, after=None,
                                   note=f"作成後の記録が失敗したためロールバックします: {e}")
            except Exception:
                pass
            status, note = await self._rollback_create(domain, hid, created_item_id)
            step("rollback_delete", status == "gone", note)
            msg = f"作成後の記録（バックアップ／台帳）に失敗しました: {e}"
            extra: dict[str, Any] = {}
            if status == "gone":
                msg += f"。{note}ため、HA 上のヘルパーはロールバック（削除）しました"
                extra["rolled_back"] = True
            elif status == "present":
                msg += f"。ロールバックできなかったため {entity_id} が HA に残っています（{note}）。HA 側で手動削除してください"
                extra["rolled_back"] = False
                extra["manual_delete_required"] = True
            elif status == "foreign":
                msg += f"。今回の作成物は残っていません。{note}"
                extra["rolled_back"] = True
                extra["warnings"] = [f"同名のヘルパー（{entity_id}）が HA に存在しますが、今回の作成物では"
                                     "ないため残してあります。HA 側で確認してください"]
            else:
                msg += f"。ロールバック結果を確認できません（{note}）。{entity_id} が HA に残っている可能性があります"
                extra["rolled_back"] = None
                extra["outcome_unknown"] = True
            return fail("record_failed", msg, entity_id=entity_id, **extra)
        self._audit.write("helper_deploy", hid, "success", detail={"steps": steps})
        return {"deployed": True, "helper_id": hid, "entity_id": entity_id,
                "validation": v.to_dict(), "steps": steps}

    async def _exists(self, domain: str, helper_id: str) -> bool:
        items = await self._collection.list_items(domain)
        return any(it.get("name") == helper_id for it in items)

    async def delete(self, domain: str, helper_id: str, skip_ledger: bool = False) -> None:
        items = await self._collection.list_items(domain)
        for it in items:
            if it.get("name") == helper_id:
                await self._collection.delete_item(domain, it["id"])
                return
        raise HAError(f"ヘルパーが見つかりません: {domain}.{helper_id}")

    async def delete_checked(self, entity_id: str, expected_current: Any = _UNSET) -> dict:
        async with self._lock:
            return await self._delete_checked(entity_id, expected_current)

    async def _delete_checked(self, entity_id: str, expected_current: Any) -> dict:
        domain, _, helper_id = entity_id.partition(".")
        if domain not in SUPPORTED_HELPER_DOMAINS or not helper_id or \
                not MANAGED_ID_RE.match(helper_id):
            return {"deleted": False, "errors": [
                {"code": "not_managed", "message": f"管理対象のヘルパーではありません: {entity_id}"}]}
        if self.ledger_entry(entity_id) is None:
            return {"deleted": False, "errors": [
                {"code": "not_managed",
                 "message": "管理台帳に作成記録がないため削除しません"}]}
        current = await self.current_definition(entity_id)
        if current is None:
            return {"deleted": False, "errors": [
                {"code": "not_found", "message": f"HA にヘルパーが見つかりません: {entity_id}"}]}
        if expected_current is not _UNSET and expected_current is not None and \
                _canon(current) != _canon(expected_current):
            err = {"code": "changed_since_proposal",
                   "message": "提案後に定義が変更されたため削除しません。新しい提案を作り直してください"}
            self._audit.write("helper_delete", entity_id, "failed", errors=[err])
            return {"deleted": False, "errors": [err]}
        entry = self.ledger_entry(entity_id)
        if entry is not None and \
                helper_definition_hash(entity_id, current) != entry.get("definition_hash"):
            err = {"code": "definition_changed_since_creation",
                   "message": "作成後に定義が変更されているため削除しません。"
                              "HA 側で作成時の定義に戻すか、所有者の台帳再登録（引き継ぎ）が必要です"}
            self._audit.write("helper_delete", entity_id, "failed", errors=[err])
            return {"deleted": False, "errors": [err]}
        try:
            self._backups.save(helper_id, "helper_delete",
                               before={"entity_id": entity_id, **current}, after=None)
        except Exception as e:
            err = {"code": "record_failed",
                   "message": f"削除前バックアップの保存に失敗したため削除しません: {e}"}
            self._audit.write("helper_delete", entity_id, "failed", errors=[err])
            return {"deleted": False, "errors": [err]}
        # 削除コマンドの応答が失われても HA 側では削除済みのことがある（2026-11-20 H1 監査 P2）。
        # コマンドの成否ではなく「実体が消えたか」で結果を確定させる。確認自体ができない場合は
        # 失敗と即断せず「結果不明」として、台帳は触らずに復旧手順を返す。
        delete_error: HAError | None = None
        try:
            await self.delete(domain, helper_id, skip_ledger=True)
        except HAError as e:
            delete_error = e
        gone: bool | None = None
        for _attempt in range(2):
            try:
                gone = not await self._exists(domain, helper_id)
                break
            except HAError:
                gone = None
        if gone is None:
            err = {"code": "delete_outcome_unknown",
                   "message": "削除の結果を確認できませんでした。"
                              f"{entity_id} が HA に残っているか、HA 側で確認してください。"
                              "台帳の記録はそのまま残してあります。実体が残っていれば新しい削除提案を"
                              "作り直せます。実体が消えているのに台帳だけ残っている場合は、"
                              "managed_helpers.json からこの entity_id の項目を手動で削除してください"}
            self._audit.write("helper_delete", entity_id, "unknown", errors=[err],
                              detail={"delete_error": str(delete_error)[:300] if delete_error else None})
            return {"deleted": False, "outcome_unknown": True, "helper_id": helper_id,
                    "entity_id": entity_id, "backup_saved": True, "errors": [err]}
        if not gone:
            msg = (f"削除コマンドが失敗し、ヘルパーが残っています: {delete_error}"
                   if delete_error is not None else "削除後もヘルパーが残っています")
            err = {"code": "ha_error", "message": msg}
            self._audit.write("helper_delete", entity_id, "failed", errors=[err])
            return {"deleted": False, "errors": [err]}
        warnings: list[str] = []
        if delete_error is not None:
            warnings.append("削除コマンドの応答は不明でしたが、実体の消失を確認しました")
        if gone and self._ledger is not None:
            try:
                self._ledger.unregister(entity_id)
            except Exception as e:
                warnings.append(
                    f"台帳からの解除に失敗しました（HA 側の削除は完了しています）: {e}。"
                    "managed_helpers.json からこの entity_id の項目を手動で削除してください")
                self._audit.write(
                    "helper_ledger_unregister_failed", entity_id, "failed",
                    errors=[{"code": "ledger_unregister_failed", "message": str(e)[:300]}],
                    detail={"recovery": "managed_helpers.json から該当項目を手動で削除"})
        self._audit.write("helper_delete", entity_id, "success" if gone else "failed",
                          detail={"ledger_unregister_failed": bool(warnings)} if warnings else None)
        out = {"deleted": gone, "helper_id": helper_id, "entity_id": entity_id,
               "backup_saved": True}
        if warnings:
            out["warnings"] = warnings
        return out
