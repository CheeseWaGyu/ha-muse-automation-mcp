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
from .validation import AutomationValidator

PROPOSAL_ID_RE = re.compile(r"^p_[0-9a-f]{12}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def definition_hash(automation_id: str, definition: dict) -> str:
    canon = json.dumps({"id": automation_id, **definition}, sort_keys=True, ensure_ascii=False)
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
                 deployer: Deployer, audit: AuditLog):
        self._s = settings
        self._validator = validator
        self._deployer = deployer
        self._audit = audit
        self.store = ProposalStore(settings.proposals_dir)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ Muse 側（MCP）
    async def propose(self, automation: Any) -> dict:
        pending = [r for r in self.store.all() if r["status"] == "pending"]
        if len(pending) >= self._s.max_pending_proposals:
            err = [{"code": "too_many_pending", "path": "",
                    "message": f"承認待ちが上限（{self._s.max_pending_proposals} 件）に達しています"}]
            self._audit.write("propose", None, "rejected", errors=err)
            return {"status": "rejected", "errors": err}
        v = await self._validator.validate(automation)
        if not v.valid or v.automation_id is None or v.normalized is None:
            self._audit.write("propose", v.automation_id, "rejected", errors=v.errors)
            return {"status": "rejected", "automation_id": v.automation_id,
                    "errors": v.errors, "warnings": v.warnings}
        created = _now()
        rec = {
            "proposal_id": "p_" + secrets.token_hex(6),
            "status": "pending",
            "created_at": created.isoformat(),
            "expires_at": (created + timedelta(hours=self._s.proposal_ttl_hours)).isoformat(),
            "automation_id": v.automation_id,
            "alias": v.normalized["alias"],
            "definition": v.normalized,
            "definition_hash": definition_hash(v.automation_id, v.normalized),
            "referenced_entities": v.referenced_entities,
            "warnings": v.warnings,
            "decided_at": None, "decided_by": None, "reason": None, "result": None,
        }
        self.store.save(rec)
        self._audit.write("propose", v.automation_id, "pending",
                          detail={"proposal_id": rec["proposal_id"], "hash": rec["definition_hash"]})
        return {
            "status": "pending_approval",
            "proposal_id": rec["proposal_id"],
            "automation_id": v.automation_id,
            "expires_at": rec["expires_at"],
            "warnings": v.warnings,
            "message": "まだデプロイされていません。ユーザーが HA の承認画面（サイドバー「Muse 承認」）で"
                       "承認するとデプロイされます。結果は list_managed_automations の proposals で確認できます。",
        }

    def summaries(self, limit: int = 20) -> list[dict]:
        keys = ("proposal_id", "status", "automation_id", "alias", "created_at",
                "expires_at", "decided_at", "reason")
        out = []
        for r in self.store.all()[:limit]:
            item = {k: r[k] for k in keys}
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
            result = await self._deployer.deploy({"id": rec["automation_id"], **rec["definition"]})
            rec["decided_at"] = _now().isoformat()
            rec["decided_by"] = actor
            rec["result"] = {k: result.get(k) for k in
                             ("deployed", "entity_id", "errors", "steps", "rolled_back")}
            rec["status"] = "approved" if result.get("deployed") else "failed"
            self.store.save(rec)
            self._audit.write("approve", rec["automation_id"],
                              "success" if result.get("deployed") else "failed",
                              errors=result.get("errors"),
                              detail={"proposal_id": proposal_id, "actor": actor})
            return {"ok": bool(result.get("deployed")), "result": result, "proposal": rec}

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
