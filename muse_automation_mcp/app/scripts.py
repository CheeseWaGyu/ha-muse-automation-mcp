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

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backup import MANAGED_ID_RE
from .ha_client import HAClient, HAError, assert_managed
from .validation import ScriptValidator


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
