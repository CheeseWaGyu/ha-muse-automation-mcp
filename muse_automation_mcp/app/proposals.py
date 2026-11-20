"""承認フロー: Muse は「提案」を作るだけ。HA への書き込みはユーザーが承認 UI で承認したときだけ。

- 提案は不変（定義とそのハッシュは保存後に変更されない）。承認はハッシュ一致を要求し、
  ユーザーが画面で見た内容と同一の定義だけがデプロイされる。
- 承認・却下の入口は承認 UI（ui.py）だけ。MCP ツールからは承認も却下もできない。
- 承認時にも deployer.deploy が検証をやり直す（状態が変わっていれば失敗する）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import Settings
from .deployer import Deployer
from .ha_client import HAError, NotManaged
from .validation import AutomationValidator

PROPOSAL_ID_RE = re.compile(r"^p_[0-9a-f]{12}$")
_ENTITY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")

KIND_LABEL = {"deploy": "作成", "delete": "削除", "rollback": "ロールバック",
              "script_deploy": "スクリプト作成", "script_delete": "スクリプト削除"}


def _scan_entities(obj, out: set[str] | None = None) -> list[str]:
    """定義スナップショットから entity_id 参照を集める（表示用）。"""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "entity_id":
                vals = v if isinstance(v, list) else [v]
                for x in vals:
                    if isinstance(x, str) and _ENTITY_RE.match(x):
                        out.add(x)
            else:
                _scan_entities(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _scan_entities(v, out)
    return sorted(out)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def definition_hash(automation_id: str, definition: dict) -> str:
    canon = json.dumps({"id": automation_id, **definition}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def payload_hash(kind: str, automation_id: str, payload) -> str:
    """削除・ロールバック提案用: 種別とスナップショット全体のハッシュ。"""
    canon = json.dumps({"kind": kind, "id": automation_id, "payload": payload},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class ProposalStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, pid: str) -> Path:
        if not PROPOSAL_ID_RE.match(pid):
            raise ValueError("不正な提案 ID です")
        return self.root / f"{pid}.json"

    def save(self, rec: dict) -> None:
        path = self._path(rec["proposal_id"])
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def get(self, pid: str) -> dict | None:
        try:
            path = self._path(pid)
        except ValueError:
            return None
        if not path.exists():
            return None
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec["status"] == "pending" and _now() > datetime.fromisoformat(rec["expires_at"]):
            rec["status"] = "expired"
            rec["decided_at"] = rec["expires_at"]
            self.save(rec)
        return rec

    def all(self) -> list[dict]:
        out = []
        for p in self.root.glob("p_*.json"):
            rec = self.get(p.stem)
            if rec:
                out.append(rec)
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return out


class ProposalService:
    def __init__(self, settings: Settings, validator: AutomationValidator,
                 deployer: Deployer, audit: AuditLog,
                 script_validator=None, script_deployer=None):
        self._s = settings
        self._validator = validator
        self._deployer = deployer
        self._audit = audit
        self._script_validator = script_validator
        self._script_deployer = script_deployer
        self.store = ProposalStore(settings.proposals_dir)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ Muse 側（MCP）
    def _pending_full(self) -> dict | None:
        pending = [r for r in self.store.all() if r["status"] == "pending"]
        if len(pending) >= self._s.max_pending_proposals:
            err = [{"code": "too_many_pending", "path": "",
                    "message": f"承認待ちが上限（{self._s.max_pending_proposals} 件）に達しています"}]
            self._audit.write("propose", None, "rejected", errors=err)
            return {"status": "rejected", "errors": err}
        return None

    def _save_rec(self, *, kind: str, automation_id: str, alias: str, definition: dict,
                  dhash: str, referenced: list[str], warnings: list, payload=None,
                  resource: str = "automation") -> dict:
        created = _now()
        rec = {
            "proposal_id": "p_" + secrets.token_hex(6),
            "kind": kind,
            "resource": resource,
            # スクリプト提案では対象スクリプトの ID（automation_id は表示・互換用に同じ値を入れる）
            "script_id": automation_id if resource == "script" else None,
            "status": "pending",
            "created_at": created.isoformat(),
            "expires_at": (created + timedelta(hours=self._s.proposal_ttl_hours)).isoformat(),
            "automation_id": automation_id,
            "alias": alias,
            "definition": definition,
            "definition_hash": dhash,
            "payload": payload,
            "referenced_entities": referenced,
            "warnings": warnings,
            "decided_at": None, "decided_by": None, "reason": None, "result": None,
        }
        self.store.save(rec)
        self._audit.write("propose", automation_id, "pending",
                          detail={"proposal_id": rec["proposal_id"], "hash": dhash, "kind": kind})
        return rec

    @staticmethod
    def _pending_reply(rec: dict, action_label: str) -> dict:
        out = {
            "status": "pending_approval",
            "proposal_id": rec["proposal_id"],
            "kind": rec["kind"],
            "resource": rec.get("resource", "automation"),
            "automation_id": rec["automation_id"],
            "expires_at": rec["expires_at"],
            "warnings": rec["warnings"],
            "message": f"まだ{action_label}されていません。ユーザーが HA の承認画面（サイドバー「Muse 承認」）で"
                       f"承認すると{action_label}されます。結果は list_managed_automations の proposals で確認できます。",
        }
        if rec.get("resource") == "script":
            out["script_id"] = rec["automation_id"]
            out["message"] = (f"まだ{action_label}されていません。ユーザーが HA の承認画面（サイドバー「Muse 承認」）で"
                              f"承認すると{action_label}されます。結果は list_managed_scripts の proposals で確認できます。")
        return out

    async def propose(self, automation: Any) -> dict:
        if (full := self._pending_full()) is not None:
            return full
        v = await self._validator.validate(automation)
        if not v.valid or v.automation_id is None or v.normalized is None:
            self._audit.write("propose", v.automation_id, "rejected", errors=v.errors)
            return {"status": "rejected", "automation_id": v.automation_id,
                    "errors": v.errors, "warnings": v.warnings}
        rec = self._save_rec(kind="deploy", automation_id=v.automation_id,
                             alias=v.normalized["alias"], definition=v.normalized,
                             dhash=definition_hash(v.automation_id, v.normalized),
                             referenced=v.referenced_entities, warnings=v.warnings)
        return self._pending_reply(rec, "デプロイ")

    async def propose_delete(self, automation_id: str) -> dict:
        """削除の提案を作るだけ（HA には触らない）。現在の定義をスナップショットし、
        承認時に定義が変わっていれば実行しない。"""
        if (full := self._pending_full()) is not None:
            return full
        try:
            current = await self._deployer.current_definition(automation_id)
        except (NotManaged, ValueError) as e:
            err = [{"code": "not_managed", "message": str(e)}]
            self._audit.write("propose_delete", str(automation_id)[:100], "rejected", errors=err)
            return {"status": "rejected", "automation_id": automation_id, "errors": err}
        except HAError as e:
            err = [{"code": "ha_error", "message": str(e)[:300]}]
            self._audit.write("propose_delete", automation_id, "rejected", errors=err)
            return {"status": "rejected", "automation_id": automation_id, "errors": err}
        if current is None:
            err = [{"code": "not_found", "message": f"{automation_id} は存在しません"}]
            self._audit.write("propose_delete", automation_id, "rejected", errors=err)
            return {"status": "rejected", "automation_id": automation_id, "errors": err}
        payload = {"current": current}
        rec = self._save_rec(kind="delete", automation_id=automation_id,
                             alias=str(current.get("alias") or automation_id),
                             definition=current, dhash=payload_hash("delete", automation_id, payload),
                             referenced=_scan_entities(current), warnings=[], payload=payload)
        return self._pending_reply(rec, "削除")

    async def propose_rollback(self, automation_id: str) -> dict:
        """ロールバックの提案を作るだけ（HA には触らない）。対象バックアップを固定し、
        承認時に直近バックアップが変わっていれば実行しない。"""
        if (full := self._pending_full()) is not None:
            return full
        try:
            record = self._deployer.latest_backup_record(automation_id)
        except (NotManaged, ValueError) as e:
            err = [{"code": "not_managed", "message": str(e)}]
            self._audit.write("propose_rollback", str(automation_id)[:100], "rejected", errors=err)
            return {"status": "rejected", "automation_id": automation_id, "errors": err}
        if record is None:
            err = [{"code": "no_backup", "message": "このオートメーションのバックアップがありません"}]
            self._audit.write("propose_rollback", automation_id, "rejected", errors=err)
            return {"status": "rejected", "automation_id": automation_id, "errors": err}
        target = record.get("before")
        payload = {"backup_timestamp": record.get("timestamp"),
                   "backup_operation": record.get("operation"), "target": target,
                   "after": record.get("after")}
        definition = target if isinstance(target, dict) else {}
        alias = str((target or {}).get("alias") or automation_id) if isinstance(target, dict) \
            else automation_id
        rec = self._save_rec(kind="rollback", automation_id=automation_id,
                             alias=f"ロールバック: {alias}",
                             definition=definition,
                             dhash=payload_hash("rollback", automation_id, payload),
                             referenced=_scan_entities(target), warnings=[], payload=payload)
        return self._pending_reply(rec, "ロールバック")

    # ------------------------------------------------------------ スクリプト（S2）
    async def propose_script(self, script: Any) -> dict:
        """スクリプト作成の提案を作るだけ（HA には触らない）。"""
        if self._script_validator is None or self._script_deployer is None:
            err = [{"code": "scripts_not_available", "message": "スクリプト機能が組み込まれていません"}]
            return {"status": "rejected", "errors": err}
        if (full := self._pending_full()) is not None:
            return full
        v = await self._script_validator.validate(script)
        if not v.valid or v.script_id is None or v.normalized is None:
            self._audit.write("propose_script", v.script_id, "rejected", errors=v.errors)
            return {"status": "rejected", "script_id": v.script_id,
                    "errors": v.errors, "warnings": v.warnings}
        rec = self._save_rec(kind="script_deploy", automation_id=v.script_id,
                             alias=v.normalized["alias"], definition=v.normalized,
                             dhash=definition_hash(v.script_id, v.normalized),
                             referenced=v.referenced_entities, warnings=v.warnings,
                             resource="script")
        return self._pending_reply(rec, "作成")

    async def propose_script_delete(self, script_id: str) -> dict:
        """スクリプト削除の提案を作るだけ。台帳に作成記録があるものだけが対象。"""
        if self._script_deployer is None:
            err = [{"code": "scripts_not_available", "message": "スクリプト機能が組み込まれていません"}]
            return {"status": "rejected", "errors": err}
        if (full := self._pending_full()) is not None:
            return full
        try:
            current = await self._script_deployer.current_definition(script_id)
        except (NotManaged, ValueError) as e:
            err = [{"code": "not_managed", "message": str(e)}]
            self._audit.write("propose_script_delete", str(script_id)[:100], "rejected", errors=err)
            return {"status": "rejected", "script_id": script_id, "errors": err}
        except HAError as e:
            err = [{"code": "ha_error", "message": str(e)[:300]}]
            self._audit.write("propose_script_delete", script_id, "rejected", errors=err)
            return {"status": "rejected", "script_id": script_id, "errors": err}
        if current is None:
            err = [{"code": "not_found", "message": f"{script_id} は存在しません"}]
            self._audit.write("propose_script_delete", script_id, "rejected", errors=err)
            return {"status": "rejected", "script_id": script_id, "errors": err}
        if self._script_deployer.ledger_entry(script_id) is None:
            err = [{"code": "not_managed",
                    "message": "管理台帳に作成記録がないため削除提案できません"
                               "（このアドオンが作成したスクリプトだけが対象です）"}]
            self._audit.write("propose_script_delete", script_id, "rejected", errors=err)
            return {"status": "rejected", "script_id": script_id, "errors": err}
        payload = {"current": current}
        rec = self._save_rec(kind="script_delete", automation_id=script_id,
                             alias=str(current.get("alias") or script_id),
                             definition=current,
                             dhash=payload_hash("script_delete", script_id, payload),
                             referenced=_scan_entities(current), warnings=[],
                             payload=payload, resource="script")
        return self._pending_reply(rec, "削除")

    def summaries(self, limit: int = 20) -> list[dict]:
        keys = ("proposal_id", "status", "automation_id", "alias", "created_at",
                "expires_at", "decided_at", "reason")
        out = []
        for r in self.store.all()[:limit]:
            item = {k: r[k] for k in keys}
            item["kind"] = r.get("kind", "deploy")
            item["resource"] = r.get("resource", "automation")
            if r.get("resource") == "script":
                item["script_id"] = r.get("script_id") or r["automation_id"]
            if r["status"] == "failed" and r.get("result"):
                item["errors"] = r["result"].get("errors")
            if r["status"] == "approved" and r.get("result"):
                item["entity_id"] = r["result"].get("entity_id")
            out.append(item)
        return out

    # ------------------------------------------------------------ ユーザー側（承認 UI のみ）
    async def approve(self, proposal_id: str, expected_hash: str, actor: str) -> dict:
        async with self._lock:
            rec = self.store.get(proposal_id)
            if rec is None:
                return {"ok": False, "error": "提案が見つかりません"}
            if rec["status"] != "pending":
                return {"ok": False, "error": f"この提案は {rec['status']} のため承認できません"}
            if expected_hash != rec["definition_hash"]:
                self._audit.write("approve", rec["automation_id"], "rejected",
                                  errors=[{"code": "hash_mismatch"}],
                                  detail={"proposal_id": proposal_id, "actor": actor})
                return {"ok": False, "error": "画面の内容と保存済みの提案が一致しません。再読み込みしてください"}
            kind = rec.get("kind", "deploy")
            aid = rec["automation_id"]
            success_key = {"deploy": "deployed", "delete": "deleted",
                           "rollback": "rolled_back", "script_deploy": "deployed",
                           "script_delete": "deleted"}[kind]
            if kind == "deploy":
                result = await self._deployer.deploy({"id": aid, **rec["definition"]})
            elif kind == "script_deploy":
                if self._script_deployer is None:
                    result = {"deployed": False, "errors": [
                        {"code": "scripts_not_available", "message": "スクリプト機能が組み込まれていません"}]}
                else:
                    result = await self._script_deployer.deploy({"id": aid, **rec["definition"]})
            elif kind == "script_delete":
                if self._script_deployer is None:
                    result = {"deleted": False, "errors": [
                        {"code": "scripts_not_available", "message": "スクリプト機能が組み込まれていません"}]}
                else:
                    try:
                        result = await self._script_deployer.delete_checked(
                            aid, (rec.get("payload") or {}).get("current"))
                    except HAError as e:
                        result = {"deleted": False, "errors": [
                            {"code": "ha_error", "message": str(e)[:300]}]}
            elif kind == "delete":
                # 照合と削除は deployer の同じロックの中で行う（TOCTOU 対策）
                try:
                    result = await self._deployer.delete_checked(
                        aid, (rec.get("payload") or {}).get("current"))
                except HAError as e:
                    result = {"deleted": False, "errors": [
                        {"code": "ha_error", "message": str(e)[:300]}]}
            else:  # rollback
                # timestamp 照合・現在定義と backup.after の照合・実行は deployer の同じロックの中
                ts = (rec.get("payload") or {}).get("backup_timestamp")
                if not ts:
                    result = {"rolled_back": False, "errors": [
                        {"code": "changed_since_proposal",
                         "message": "提案に対象バックアップの記録がありません。新しい提案を作り直してください"}]}
                else:
                    try:
                        result = await self._deployer.rollback_checked(aid, ts)
                    except HAError as e:
                        result = {"rolled_back": False, "errors": [
                            {"code": "ha_error", "message": str(e)[:300]}]}
            ok = bool(result.get(success_key))
            rec["decided_at"] = _now().isoformat()
            rec["decided_by"] = actor
            rec["result"] = {k: result.get(k) for k in
                             ("deployed", "deleted", "rolled_back", "entity_id",
                              "errors", "steps", "result", "backup_saved")}
            rec["status"] = "approved" if ok else "failed"
            self.store.save(rec)
            self._audit.write("approve", aid, "success" if ok else "failed",
                              errors=result.get("errors"),
                              detail={"proposal_id": proposal_id, "actor": actor, "kind": kind})
            return {"ok": ok, "result": result, "proposal": rec}

    async def reject(self, proposal_id: str, reason: str, actor: str) -> dict:
        async with self._lock:
            rec = self.store.get(proposal_id)
            if rec is None:
                return {"ok": False, "error": "提案が見つかりません"}
            if rec["status"] != "pending":
                return {"ok": False, "error": f"この提案は {rec['status']} のため却下できません"}
            rec.update(status="rejected", decided_at=_now().isoformat(), decided_by=actor,
                       reason=reason[:500] or None)
            self.store.save(rec)
            self._audit.write("reject", rec["automation_id"], "success",
                              detail={"proposal_id": proposal_id, "actor": actor,
                                      "reason": rec["reason"]})
            return {"ok": True, "proposal": rec}
