"""Phase H1: ヘルパー作成（まず input_boolean のみ）。

単純ヘルパーは REST config API が無く、WebSocket の StorageCollection コマンドで扱う:
  ws://<HA>/websocket に接続し auth（アクセストークン。アドオン形態では SUPERVISOR_TOKEN）後、
  {"type": "input_boolean/list" | "input_boolean/create" | "input_boolean/delete"} を送る。

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

SUPPORTED_HELPER_DOMAINS = ("input_boolean",)  # H1 は input_boolean のみ。H2 で拡張する
_HELPER_KEYS = {"id", "icon"}
_ICON_RE = re.compile(r"^mdi:[a-z0-9]+(-[a-z0-9]+)*$")
_UNSET = object()


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


def _normalized(definition: dict) -> dict:
    """保存・比較に使う正規化済み定義（id と icon のみ。name は id から導出）。"""
    out = {"id": definition["id"]}
    if definition.get("icon"):
        out["icon"] = definition["icon"]
    return out


def _from_item(item: dict) -> dict:
    """StorageCollection の項目（name/icon）から正規化済み定義へ。"""
    out = {"id": item["name"]}
    if item.get("icon"):
        out["icon"] = item["icon"]
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
        data = self._load()
        data[entity_id] = {
            "domain": domain,
            "helper_id": definition["id"],
            "definition": _normalized(definition),
            "definition_hash": helper_definition_hash(entity_id, _normalized(definition)),
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
                f"H1 で対応するヘルパーは {list(SUPPORTED_HELPER_DOMAINS)} のみです: {domain!r}")
            return HelperValidationResult(False, domain=domain, errors=errors)
        if not isinstance(definition, dict):
            err("invalid_helper", "ヘルパー定義はオブジェクトである必要があります")
            return HelperValidationResult(False, domain=domain, errors=errors)
        unknown = sorted(set(definition) - _HELPER_KEYS)
        if unknown:
            err("unknown_keys", f"未対応のキーがあります: {unknown}（対応: {sorted(_HELPER_KEYS)}。"
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
        normalized = _normalized({"id": helper_id, "icon": icon})
        return HelperValidationResult(not errors, helper_id=helper_id, entity_id=entity_id,
                                      domain=domain,
                                      normalized=normalized if not errors else None,
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
        items_by_domain: dict[str, dict[str, dict]] = {}
        for domain in SUPPORTED_HELPER_DOMAINS:
            try:
                items = await self._collection.list_items(domain)
            except HAError:
                items = []
            items_by_domain[domain] = {it.get("name"): it for it in items}
        out: list[dict] = []
        for entity_id, entry in sorted(entries.items()):
            domain = entry.get("domain", "input_boolean")
            item = items_by_domain.get(domain, {}).get(entry.get("helper_id"))
            state = state_by_id.get(entity_id)
            if item is None or state is None:
                continue  # 台帳にあっても実体が無ければ管理対象に出さない
            current = _from_item(item)
            out.append({
                "helper_id": entry["helper_id"], "entity_id": entity_id, "domain": domain,
                "state": state.get("state"),
                "created_at": entry.get("created_at"),
                "definition_changed_since_creation":
                    helper_definition_hash(entity_id, current) != entry.get("definition_hash"),
            })
        managed_ids = {o["entity_id"] for o in out}
        unverified = sum(
            1 for eid in state_by_id
            if any(eid.startswith(f"{d}.muse_") for d in SUPPORTED_HELPER_DOMAINS)
            and eid not in managed_ids)
        return {"count": len(out), "helpers": out, "unverified_count": unverified}


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
                return _from_item(it)
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

        v = await self._validator.validate(
            {k: definition.get(k) for k in _HELPER_KEYS if k in definition}, domain)
        step("validate", v.valid)
        if not v.valid:
            return {"deployed": False, "errors": v.errors, "warnings": v.warnings, "steps": steps}
        hid = v.helper_id
        entity_id = v.entity_id
        normalized = v.normalized
        payload: dict[str, Any] = {"name": hid}
        if normalized.get("icon"):
            payload["icon"] = normalized["icon"]
        try:
            await self._collection.create_item(domain, payload)
            step("create", True)
        except HAError as e:
            step("create", False, str(e)[:300])
            return fail("ha_error", str(e))
        # state の出現を確認する
        appeared = await self._wait_state(entity_id, True)
        step("entity_appears", appeared)
        problem = None if appeared else "作成後に entity が出現しませんでした"
        if problem:
            rolled = False
            try:
                await self.delete(domain, hid, skip_ledger=True)
                rolled = not await self._exists(domain, hid)
            except HAError:
                rolled = False
            step("rollback_delete", rolled)
            return fail("verification_failed",
                        problem + ("。作成を取り消しました" if rolled else "。取り消しに失敗しました"),
                        rolled_back=rolled)
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
            rolled = False
            try:
                await self.delete(domain, hid, skip_ledger=True)
                rolled = not await self._exists(domain, hid)
            except HAError:
                rolled = False
            step("rollback_delete", rolled)
            msg = f"作成後の記録（バックアップ／台帳）に失敗しました: {e}"
            msg += "。HA 上のヘルパーはロールバック（削除）しました" if rolled else \
                f"。ロールバックできなかったため {entity_id} が HA に残っている可能性があります。HA 側で手動削除してください"
            return fail("record_failed", msg, rolled_back=rolled)
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
        try:
            await self.delete(domain, helper_id, skip_ledger=True)
        except HAError as e:
            self._audit.write("helper_delete", entity_id, "failed",
                              errors=[{"code": "ha_error", "message": str(e)[:300]}])
            return {"deleted": False, "errors": [{"code": "ha_error", "message": str(e)}]}
        gone = not await self._exists(domain, helper_id)
        warnings: list[str] = []
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
