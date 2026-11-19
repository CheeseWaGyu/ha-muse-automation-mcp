"""デプロイ・削除・ロールバック・一覧。

create-only: 既存 ID への POST は経路上どこからも到達できない
  (1) validate が id_exists で拒否 (2) deploy の明示的な存在確認 (3) HAClient.create_automation_config の内部確認
削除・ロールバックは muse_ ID のみ（HAClient / BackupStore も同じ検証を持つ）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from .audit import AuditLog
from .backup import BackupStore
from .config import Settings
from .ha_client import (AlreadyExists, HAClient, HAError, NotManaged, assert_managed,
                        find_automation_state)
from .validation import AutomationValidator, ValidationResult, slugify_ascii

log = logging.getLogger("muse.deploy")


def _canon(d: dict | None) -> str:
    d = {k: v for k, v in (d or {}).items() if k != "id"}
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def predicted_entity_id(alias: str) -> str:
    return f"automation.{slugify_ascii(alias, 60)}"


class Deployer:
    def __init__(self, settings: Settings, ha: HAClient, validator: AutomationValidator,
                 audit: AuditLog, backups: BackupStore):
        self._s = settings
        self._ha = ha
        self._validator = validator
        self._audit = audit
        self._backups = backups
        self._lock = asyncio.Lock()  # 書き込み系は直列化（TOCTOU 対策）

    # ------------------------------------------------------------------ 読み取り
    async def preview(self, automation: Any) -> dict:
        v = await self._validator.validate(automation)
        out: dict[str, Any] = {**v.public(), "referenced_entities": v.referenced_entities}
        if v.valid and v.normalized is not None:
            out.update(
                automation_id=v.automation_id,
                normalized_definition={"id": v.automation_id, **v.normalized},
                predicted_entity_id=predicted_entity_id(v.normalized["alias"]),
                notes=[
                    "entity_id は HA が alias から決めるため予測値です（重複時は _2 等が付く／日本語 alias は変換される）。"
                    "デプロイ結果には実際の entity_id が入ります。",
                    "ID を自動生成した場合、デプロイ時は別のランダムサフィックスになります。"
                    "同じ ID にしたい場合は定義に `id` として指定してください。",
                ],
            )
        return out

    async def list_managed(self) -> dict:
        states = await self._ha.get_states()
        items = []
        for st in states:
            attrs = st.get("attributes") or {}
            aid = attrs.get("id")
            if st.get("entity_id", "").startswith("automation.") and isinstance(aid, str) \
                    and aid.startswith("muse_"):
                items.append({
                    "automation_id": aid,
                    "entity_id": st["entity_id"],
                    "alias": attrs.get("friendly_name"),
                    "state": st.get("state"),
                    "last_triggered": attrs.get("last_triggered"),
                })
        items.sort(key=lambda x: x["automation_id"])
        return {"count": len(items), "automations": items}

    # ------------------------------------------------------------------ 共通
    async def _wait_entity(self, automation_id: str) -> dict | None:
        attempts = max(1, math.ceil(self._s.entity_wait_seconds / self._s.entity_wait_interval)) \
            if self._s.entity_wait_seconds > 0 else 1
        for i in range(attempts):
            st = find_automation_state(await self._ha.get_states(), automation_id)
            if st:
                return st
            if i < attempts - 1:
                await asyncio.sleep(self._s.entity_wait_interval)
        return None

    # ------------------------------------------------------------------ deploy
    async def deploy(self, automation: Any) -> dict:
        async with self._lock:
            steps: list[dict] = []

            def step(name: str, ok: bool, info: str = "") -> None:
                steps.append({"step": name, "ok": ok, "info": info})

            # 1. 検証
            v: ValidationResult = await self._validator.validate(automation)
            step("validate", v.valid)
            aid = v.automation_id
            if not v.valid or aid is None or v.normalized is None:
                self._audit.write("deploy", aid, "rejected", errors=v.errors, detail={"steps": steps})
                return {"deployed": False, "automation_id": aid, "errors": v.errors,
                        "warnings": v.warnings, "steps": steps, "rolled_back": False}

            def fail(code: str, msg: str, rolled_back: bool = False) -> dict:
                err = [{"code": code, "path": "", "message": msg}]
                self._audit.write("deploy", aid, "failed", errors=err,
                                  detail={"steps": steps, "rolled_back": rolled_back})
                return {"deployed": False, "automation_id": aid, "errors": err,
                        "warnings": v.warnings, "steps": steps, "rolled_back": rolled_back}

            try:
                # 2. 不在確認
                if await self._ha.get_automation_config(aid) is not None:
                    step("check_id_absent", False)
                    return fail("id_exists", f"ID {aid} は既に存在します。上書きはしません")
                step("check_id_absent", True)

                # 3. HA 全体の設定検証（デプロイ前の現状確認）
                log.info("check_config はデプロイ前の現状確認です (id=%s)", aid)
                cc = await self._ha.check_config()
                if cc.get("result") != "valid":
                    step("check_config", False, str(cc.get("errors"))[:300])
                    return fail("check_config_failed",
                                f"HA の設定検証が有効ではないため中止: {cc.get('errors')}")
                step("check_config", True, "pre-deploy state check")

                # 4. 作成
                await self._ha.create_automation_config(aid, v.normalized)
                step("create", True)
            except AlreadyExists as e:
                step("create", False)
                return fail("id_exists", str(e))
            except HAError as e:
                step("create", False, str(e)[:300])
                return fail("ha_error", str(e))

            # 5. 読み戻し + entity 出現確認。失敗したら削除してロールバック
            problem = None
            try:
                stored = await self._ha.get_automation_config(aid)
                if stored is None or _canon(stored) != _canon(v.normalized):
                    problem = "読み戻した定義が送信内容と一致しません"
                    step("readback", False)
                else:
                    step("readback", True)
                    st = await self._wait_entity(aid)
                    if st is None:
                        problem = "automation entity が states に現れませんでした"
                        step("entity_appears", False)
                    else:
                        step("entity_appears", True, st["entity_id"])
            except HAError as e:
                problem = f"確認中に HA エラー: {e}"

            if problem:
                rolled = False
                try:
                    await self._ha.delete_automation_config(aid)
                    rolled = await self._ha.get_automation_config(aid) is None
                except HAError as e:
                    log.error("ロールバック削除に失敗 id=%s: %s", aid, e)
                step("rollback_delete", rolled)
                return fail("verification_failed", problem, rolled_back=rolled)

            # 6. バックアップ + 監査
            self._backups.save(aid, "deploy", before=None, after={"id": aid, **v.normalized})
            self._audit.write("deploy", aid, "success",
                              detail={"steps": steps, "entity_id": st["entity_id"]})
            return {"deployed": True, "automation_id": aid, "entity_id": st["entity_id"],
                    "validation": v.public(), "steps": steps}

    # ------------------------------------------------------------------ delete
    async def delete(self, automation_id: str) -> dict:
        async with self._lock:
            try:
                assert_managed(automation_id)
            except NotManaged as e:
                self._audit.write("delete", str(automation_id)[:100], "rejected",
                                  errors=[{"code": "not_managed", "message": str(e)}])
                return {"deleted": False, "errors": [{"code": "not_managed", "message": str(e)}]}
            try:
                current = await self._ha.get_automation_config(automation_id)
                if current is None:
                    self._audit.write("delete", automation_id, "failed",
                                      errors=[{"code": "not_found"}])
                    return {"deleted": False, "errors": [{"code": "not_found",
                            "message": f"{automation_id} は存在しません"}]}
                self._backups.save(automation_id, "delete", before=current, after=None)
                await self._ha.delete_automation_config(automation_id)
                gone = await self._ha.get_automation_config(automation_id) is None
            except HAError as e:
                self._audit.write("delete", automation_id, "failed",
                                  errors=[{"code": "ha_error", "message": str(e)[:300]}])
                return {"deleted": False, "errors": [{"code": "ha_error", "message": str(e)}]}
            self._audit.write("delete", automation_id, "success" if gone else "failed")
            return {"deleted": gone, "automation_id": automation_id, "backup_saved": True}

    # ------------------------------------------------------------------ rollback
    async def rollback(self, automation_id: str) -> dict:
        """直近の記録済み操作を取り消し、その操作の直前の状態へ戻す。

        deploy を取り消す → 削除（作成直前＝存在しない状態）
        delete を取り消す → 削除前の定義で再作成（既に同 ID が存在する場合は拒否）
        """
        async with self._lock:
            try:
                assert_managed(automation_id)
                record = self._backups.latest(automation_id)
            except (NotManaged, ValueError) as e:
                self._audit.write("rollback", str(automation_id)[:100], "rejected",
                                  errors=[{"code": "not_managed", "message": str(e)}])
                return {"rolled_back": False, "errors": [{"code": "not_managed", "message": str(e)}]}
            if record is None:
                self._audit.write("rollback", automation_id, "failed",
                                  errors=[{"code": "no_backup"}])
                return {"rolled_back": False, "errors": [{"code": "no_backup",
                        "message": "このオートメーションのバックアップがありません"}]}
            target = record["before"]
            try:
                current = await self._ha.get_automation_config(automation_id)
                if target is None:  # → 存在しない状態へ
                    if current is None:
                        self._audit.write("rollback", automation_id, "success", detail={"noop": True})
                        return {"rolled_back": True, "automation_id": automation_id,
                                "result": "already_absent"}
                    self._backups.save(automation_id, "rollback", before=current, after=None)
                    await self._ha.delete_automation_config(automation_id)
                    ok = await self._ha.get_automation_config(automation_id) is None
                    result = "deleted"
                else:  # → 以前の定義へ（再作成のみ。上書きはしない）
                    if current is not None:
                        err = {"code": "id_exists", "message":
                               "同じ ID が既に存在するため復元できません（上書き禁止）。先に削除してください"}
                        self._audit.write("rollback", automation_id, "failed", errors=[err])
                        return {"rolled_back": False, "errors": [err]}
                    cc = await self._ha.check_config()
                    if cc.get("result") != "valid":
                        err = {"code": "check_config_failed", "message": str(cc.get("errors"))[:300]}
                        self._audit.write("rollback", automation_id, "failed", errors=[err])
                        return {"rolled_back": False, "errors": [err]}
                    body = {k: v for k, v in target.items() if k != "id"}
                    self._backups.save(automation_id, "rollback", before=None, after=target)
                    await self._ha.create_automation_config(automation_id, body)
                    stored = await self._ha.get_automation_config(automation_id)
                    ok = stored is not None and _canon(stored) == _canon(body)
                    ok = ok and (await self._wait_entity(automation_id)) is not None
                    result = "restored"
            except HAError as e:
                self._audit.write("rollback", automation_id, "failed",
                                  errors=[{"code": "ha_error", "message": str(e)[:300]}])
                return {"rolled_back": False, "errors": [{"code": "ha_error", "message": str(e)}]}
            self._audit.write("rollback", automation_id, "success" if ok else "failed",
                              detail={"result": result})
            return {"rolled_back": ok, "automation_id": automation_id, "result": result}
