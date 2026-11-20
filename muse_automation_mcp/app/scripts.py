"""スクリプトの読み取り系（Phase S1: list / preview）と管理台帳。

作成・削除（deploy_script / delete_managed_script）は Phase S2 で承認フローに載せる。
ここでは HA への書き込みは一切しない（台帳はアドオン側データ領域への記録のみ）。

管理対象の判定（2026-11-20 外部監査対応）:
  states の `script.muse_*` という接頭辞だけを管理の根拠にはしない。
  「管理対象」とするのは次の 3 点がそろったものだけ:
    1. 本モジュールの管理台帳（ScriptLedger）に作成記録がある
    2. HA の config API で定義が読み戻せる（YAML 定義など config 外のものは対象外）
    3. states に script.<id> が存在する
  接頭辞は一致するが台帳にないものは、件数だけを `unverified_count` として返す
  （管理対象とは主張せず、存在自体は所有者に見えるようにする）。
  台帳の定義ハッシュと現在の定義が食い違う場合は、除外はせず
  `definition_changed_since_creation: true` を付けて返す（S2 の削除・復元では
  このフラグと changed_since_proposal 照合で書き込みを止める）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .backup import MANAGED_ID_RE, BackupStore
from .config import Settings
from .ha_client import (AlreadyExists, HAClient, HAError, NotManaged, assert_managed,
                        find_script_state)
from .validation import ScriptValidator, ScriptValidationResult

log = logging.getLogger("muse.script_deploy")


def predicted_script_entity_id(script_id: str) -> str:
    return f"script.{script_id}"


def script_definition_hash(script_id: str, definition: dict) -> str:
    body = {k: v for k, v in definition.items() if k != "id"}
    canon = json.dumps({"id": script_id, **body}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class ScriptLedger:
    """アドオンが作成したスクリプトの台帳（data_dir/managed_scripts.json）。

    S2 の deploy 成功時に register() で記録し、delete 成功時に unregister() する。
    S1 では記録経路はまだ無く、既存台帳の読み取りだけが使われる。
    """

    def __init__(self, path: Path):
        self._path = path

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items()
                if isinstance(k, str) and MANAGED_ID_RE.match(k) and isinstance(v, dict)}

    def get(self, script_id: str) -> dict | None:
        return self._load().get(script_id)

    def register(self, script_id: str, definition: dict) -> dict:
        assert_managed(script_id)
        entries = self._load()
        rec = {
            "script_id": script_id,
            "alias": definition.get("alias"),
            "definition_hash": script_definition_hash(script_id, definition),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        entries[script_id] = rec
        self._save(entries)
        return rec

    def unregister(self, script_id: str) -> None:
        entries = self._load()
        if script_id in entries:
            del entries[script_id]
            self._save(entries)

    def _save(self, entries: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self._path)


class ScriptManager:
    def __init__(self, ha: HAClient, validator: ScriptValidator,
                 ledger: ScriptLedger | None = None):
        self._ha = ha
        self._validator = validator
        self._ledger = ledger

    # S2 の deploy 成功時に呼ぶ（台帳が無ければ何もしない）
    def register_created(self, script_id: str, definition: dict) -> dict | None:
        if self._ledger is None:
            return None
        return self._ledger.register(script_id, definition)

    async def preview(self, script: Any) -> dict:
        v = await self._validator.validate(script)
        out: dict[str, Any] = {**v.public(), "referenced_entities": v.referenced_entities}
        if v.valid and v.normalized is not None and v.script_id is not None:
            notes = [
                "entity_id は script.<id> で確定します（オートメーションと違い alias 由来ではありません）。",
                "ID を自動生成した場合、デプロイ時は別のランダムサフィックスになります。"
                "同じ ID にしたい場合は定義に `id` として指定してください。",
                "Phase S1 では読み取り・検証のみです。作成は S2（承認フロー経由）で有効化します。",
            ]
            # テンプレート方針（監査対応）: 静的解析はベストエフォートであり、
            # テンプレートを含む定義は承認画面でも警告を明示する前提で扱う。
            if any(w.get("code") == "template_partial_analysis" for w in v.warnings):
                notes.append(
                    "この定義はテンプレートを含みます。テンプレートの entity 参照チェックは静的解析"
                    "（ベストエフォート）で、複雑な記述では検出漏れがあり得ます。S2 で作成する場合は、"
                    "承認画面にこの警告が表示されることを確認してから承認してください。")
            out.update(
                script_id=v.script_id,
                normalized_definition={"id": v.script_id, **v.normalized},
                predicted_entity_id=predicted_script_entity_id(v.script_id),
                notes=notes,
            )
        return out

    async def list_managed(self) -> dict:
        states = await self._ha.get_states()
        items = []
        unverified = 0
        for st in states:
            eid = st.get("entity_id", "")
            if not eid.startswith("script.muse_"):
                continue
            script_id = eid[len("script."):]
            try:
                assert_managed(script_id)
            except Exception:
                unverified += 1
                continue
            entry = self._ledger.get(script_id) if self._ledger is not None else None
            if entry is None:
                # 接頭辞は一致するが台帳に作成記録がない = このアドオンの管理対象とは言えない
                unverified += 1
                continue
            try:
                current = await self._ha.get_script_config(script_id)
            except HAError:
                current = None
            if current is None:
                # config API で読み戻せない（YAML 定義等）ものは管理対象に含めない
                unverified += 1
                continue
            attrs = st.get("attributes") or {}
            items.append({
                "script_id": script_id,
                "entity_id": eid,
                "alias": attrs.get("friendly_name"),
                "state": st.get("state"),
                "last_triggered": attrs.get("last_triggered"),
                "mode": attrs.get("mode"),
                "current": attrs.get("current"),
                "created_at": entry.get("created_at"),
                "definition_changed_since_creation":
                    script_definition_hash(script_id, current) != entry.get("definition_hash"),
            })
        items.sort(key=lambda x: x["script_id"])
        return {"count": len(items), "scripts": items, "unverified_count": unverified}

    async def current_definition(self, script_id: str) -> dict | None:
        """現在の定義を返す（S2 の承認提案スナップショット用）。管理対象外 ID は例外。"""
        assert_managed(script_id)
        return await self._ha.get_script_config(script_id)


def _canon(d: dict | None) -> str:
    d = {k: v for k, v in (d or {}).items() if k != "id"}
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


_UNSET = object()


class ScriptDeployer:
    """スクリプトの作成・削除（Phase S2）。承認フローからのみ呼ばれる。

    監査対応の前提をここで満たす:
      - 承認→作成→読み戻し→失敗時ロールバックまでを単一ロックで直列化する
      - 作成成功時は管理台帳へ記録する（台帳が管理対象判定の根拠）
      - 削除は台帳に記録があるものだけ。照合（changed_since_proposal）と削除も同じロック内
    """

    def __init__(self, settings: Settings, ha: HAClient, validator: ScriptValidator,
                 audit: AuditLog, backups: BackupStore, ledger: ScriptLedger | None):
        self._s = settings
        self._ha = ha
        self._validator = validator
        self._audit = audit
        self._backups = backups
        self._ledger = ledger
        self._lock = asyncio.Lock()

    def ledger_entry(self, script_id: str) -> dict | None:
        return self._ledger.get(script_id) if self._ledger is not None else None

    async def current_definition(self, script_id: str) -> dict | None:
        assert_managed(script_id)
        return await self._ha.get_script_config(script_id)

    async def _wait_entity(self, script_id: str) -> dict | None:
        attempts = max(1, math.ceil(self._s.entity_wait_seconds / self._s.entity_wait_interval)) \
            if self._s.entity_wait_seconds > 0 else 1
        for i in range(attempts):
            st = find_script_state(await self._ha.get_states(), script_id)
            if st:
                return st
            if i < attempts - 1:
                await asyncio.sleep(self._s.entity_wait_interval)
        return None

    # ------------------------------------------------------------------ deploy
    async def deploy(self, script: Any) -> dict:
        async with self._lock:
            steps: list[dict] = []

            def step(name: str, ok: bool, info: str = "") -> None:
                steps.append({"step": name, "ok": ok, "info": info})

            v: ScriptValidationResult = await self._validator.validate(script)
            step("validate", v.valid)
            sid = v.script_id
            if not v.valid or sid is None or v.normalized is None:
                self._audit.write("script_deploy", sid, "rejected", errors=v.errors,
                                  detail={"steps": steps})
                return {"deployed": False, "script_id": sid, "errors": v.errors,
                        "warnings": v.warnings, "steps": steps, "rolled_back": False}

            def fail(code: str, msg: str, rolled_back: bool = False) -> dict:
                err = [{"code": code, "path": "", "message": msg}]
                self._audit.write("script_deploy", sid, "failed", errors=err,
                                  detail={"steps": steps, "rolled_back": rolled_back})
                return {"deployed": False, "script_id": sid, "errors": err,
                        "warnings": v.warnings, "steps": steps, "rolled_back": rolled_back}

            try:
                if await self._ha.get_script_config(sid) is not None:
                    step("check_id_absent", False)
                    return fail("id_exists", f"ID {sid} は既に存在します。上書きはしません")
                step("check_id_absent", True)
                cc = await self._ha.check_config()
                if cc.get("result") != "valid":
                    step("check_config", False, str(cc.get("errors"))[:300])
                    return fail("check_config_failed",
                                f"HA の設定検証が有効ではないため中止: {cc.get('errors')}")
                step("check_config", True, "pre-deploy state check")
                await self._ha.create_script_config(sid, v.normalized)
                step("create", True)
            except AlreadyExists as e:
                step("create", False)
                return fail("id_exists", str(e))
            except HAError as e:
                step("create", False, str(e)[:300])
                return fail("ha_error", str(e))

            problem = None
            st = None
            try:
                stored = await self._ha.get_script_config(sid)
                if stored is None or _canon(stored) != _canon(v.normalized):
                    problem = "読み戻した定義が送信内容と一致しません"
                    step("readback", False)
                else:
                    step("readback", True)
                    st = await self._wait_entity(sid)
                    if st is None:
                        problem = "script entity が states に現れませんでした"
                        step("entity_appears", False)
                    else:
                        step("entity_appears", True, st["entity_id"])
            except HAError as e:
                problem = f"確認中に HA エラー: {e}"

            if problem:
                rolled = False
                try:
                    await self._ha.delete_script_config(sid)
                    rolled = await self._ha.get_script_config(sid) is None
                except HAError as e:
                    log.error("スクリプトのロールバック削除に失敗 id=%s: %s", sid, e)
                step("rollback_delete", rolled)
                return fail("verification_failed", problem, rolled_back=rolled)

            self._backups.save(sid, "script_deploy", before=None,
                               after={"id": sid, **v.normalized})
            if self._ledger is not None:
                self._ledger.register(sid, v.normalized)
            step("ledger_registered", True)
            self._audit.write("script_deploy", sid, "success",
                              detail={"steps": steps, "entity_id": st["entity_id"]})
            return {"deployed": True, "script_id": sid, "entity_id": st["entity_id"],
                    "validation": v.public(), "steps": steps}

    # ------------------------------------------------------------------ delete
    async def delete(self, script_id: str) -> dict:
        return await self._delete(script_id, expected_current=_UNSET)

    async def delete_checked(self, script_id: str, expected_current) -> dict:
        """承認フロー用: 台帳確認・照合・削除を同じロックの中で行う。"""
        return await self._delete(script_id, expected_current=expected_current)

    async def _delete(self, script_id: str, expected_current) -> dict:
        async with self._lock:
            try:
                assert_managed(script_id)
            except NotManaged as e:
                self._audit.write("script_delete", str(script_id)[:100], "rejected",
                                  errors=[{"code": "not_managed", "message": str(e)}])
                return {"deleted": False, "errors": [{"code": "not_managed", "message": str(e)}]}
            if self.ledger_entry(script_id) is None:
                err = {"code": "not_managed",
                       "message": "管理台帳に作成記録がないため削除しません"
                                  "（このアドオンが作成したスクリプトだけが削除対象です）"}
                self._audit.write("script_delete", script_id, "rejected", errors=[err])
                return {"deleted": False, "errors": [err]}
            try:
                current = await self._ha.get_script_config(script_id)
                if current is None:
                    self._audit.write("script_delete", script_id, "failed",
                                      errors=[{"code": "not_found"}])
                    return {"deleted": False, "errors": [{"code": "not_found",
                            "message": f"{script_id} は存在しません"}]}
                if expected_current is not _UNSET and _canon(current) != _canon(expected_current):
                    err = {"code": "changed_since_proposal",
                           "message": "提案後に定義が変更されたため削除しません。新しい提案を作り直してください"}
                    self._audit.write("script_delete", script_id, "failed", errors=[err])
                    return {"deleted": False, "errors": [err]}
                self._backups.save(script_id, "script_delete", before=current, after=None)
                await self._ha.delete_script_config(script_id)
                gone = await self._ha.get_script_config(script_id) is None
            except HAError as e:
                self._audit.write("script_delete", script_id, "failed",
                                  errors=[{"code": "ha_error", "message": str(e)[:300]}])
                return {"deleted": False, "errors": [{"code": "ha_error", "message": str(e)}]}
            if gone and self._ledger is not None:
                self._ledger.unregister(script_id)
            self._audit.write("script_delete", script_id, "success" if gone else "failed")
            return {"deleted": gone, "script_id": script_id, "backup_saved": True}
