"""スクリプトの読み取り系（Phase S1: list / preview）。

作成・削除（deploy_script / delete_managed_script）は Phase S2 で承認フローに載せる。
ここでは HA への書き込みは一切しない。

管理対象の判定: スクリプトは config の id がそのまま entity_id（script.<id>）になるため、
states の script.muse_* が管理対象一覧になる（REST には config の一覧取得 API がない）。
"""
from __future__ import annotations

from typing import Any

from .ha_client import HAClient, assert_managed
from .validation import ScriptValidator


def predicted_script_entity_id(script_id: str) -> str:
    return f"script.{script_id}"


class ScriptManager:
    def __init__(self, ha: HAClient, validator: ScriptValidator):
        self._ha = ha
        self._validator = validator

    async def preview(self, script: Any) -> dict:
        v = await self._validator.validate(script)
        out: dict[str, Any] = {**v.public(), "referenced_entities": v.referenced_entities}
        if v.valid and v.normalized is not None and v.script_id is not None:
            out.update(
                script_id=v.script_id,
                normalized_definition={"id": v.script_id, **v.normalized},
                predicted_entity_id=predicted_script_entity_id(v.script_id),
                notes=[
                    "entity_id は script.<id> で確定します（オートメーションと違い alias 由来ではありません）。",
                    "ID を自動生成した場合、デプロイ時は別のランダムサフィックスになります。"
                    "同じ ID にしたい場合は定義に `id` として指定してください。",
                    "Phase S1 では読み取り・検証のみです。作成は S2（承認フロー経由）で有効化します。",
                ],
            )
        return out

    async def list_managed(self) -> dict:
        states = await self._ha.get_states()
        items = []
        for st in states:
            eid = st.get("entity_id", "")
            if not eid.startswith("script.muse_"):
                continue
            script_id = eid[len("script."):]
            # states にあっても config が読めないもの（YAML 定義等）は管理対象に含めない
            try:
                assert_managed(script_id)
            except Exception:
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
            })
        items.sort(key=lambda x: x["script_id"])
        return {"count": len(items), "scripts": items}

    async def current_definition(self, script_id: str) -> dict | None:
        """現在の定義を返す（S2 の承認提案スナップショット用）。管理対象外 ID は例外。"""
        assert_managed(script_id)
        return await self._ha.get_script_config(script_id)
