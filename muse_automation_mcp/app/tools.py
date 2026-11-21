"""MCP ツール定義。

オートメーション系 7 つ（デプロイ系 3 つは DEPLOY_ENABLED=true のときのみ公開）に加え、
スクリプトの読み取り・検証系 3 つ（list_managed_scripts / validate_script /
preview_script）を常時公開する。スクリプト作成系 2 つ（deploy_script /
delete_managed_script）は Phase S2 で、SCRIPTS_ENABLED + DEPLOY_ENABLED の
両方が true のときだけ公開する（いずれも承認待ち提案を作るだけ）。
ヘルパー（Phase H1、input_boolean のみ）も同型: 読み取り・検証系 3 つ
（list_managed_helpers / validate_helper / preview_helper）を常時公開し、作成系 2 つ
（deploy_helper / delete_managed_helper）は HELPERS_ENABLED + DEPLOY_ENABLED の
両方が true のときだけ公開する。
定義読み戻し（v0.5.5）: get_automation_definition を常時公開する。管理対象
（muse_ 始まり）のオートメーションの現在の定義をそのまま返す読み取り専用ツールで、
内部的には削除提案のスナップショット用に既にある Deployer.current_definition を使う。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .catalog import CatalogService
from .config import Settings
from .deployer import Deployer
from .ha_client import HAError, NotManaged

_ENTITY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")


def _scan_entities(obj: Any, out: set[str] | None = None) -> list[str]:
    """定義から entity_id 参照を集める（proposals._scan_entities と同じ規則・表示用）。"""
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


@dataclass
class Services:
    catalog: CatalogService
    deployer: Deployer
    validator: Any
    proposals: Any = None
    script_validator: Any = None
    script_manager: Any = None
    script_deployer: Any = None
    helper_validator: Any = None
    helper_manager: Any = None
    helper_deployer: Any = None


INSTRUCTIONS = (
    "Home Assistant の automation を追加専用でデプロイするサーバーです。"
    "まず get_entity_catalog で使える entity を確認し、validate_automation → preview_deployment で検証してから"
    "（有効な場合のみ）deploy_automation を呼んでください。deploy_automation は「承認待ちの提案」を作るだけで、"
    "ユーザーが HA の承認画面で承認するまでデプロイされません（承認・却下は Muse からはできません）。"
    "delete_managed_automation / rollback_deployment も同様に承認待ち提案を作るだけで、承認されるまで実行されません。"
    "結果は list_managed_automations の proposals で確認してください。"
    "既存の管理対象オートメーションの現在の定義は get_automation_definition で読み戻せます（読み取り専用）。"
    "定義は HA 2024.10+ 記法"
    "（alias, triggers, conditions, actions, mode）で、トリガーは `trigger:`、アクションは `action:` を使います。"
    "参照できるのはカタログに載った entity_id のみで、area_id/device_id 指定は使えません。"
    "各 entity の `description`（カタログに自然言語で書かれた説明）があれば、"
    "friendly_name だけでなくその説明も読んで、目的に合う entity を選んでください。"
    "スクリプトについては validate_script / preview_script / list_managed_scripts が使えます。"
    "スクリプト定義は alias / sequence / mode / fields / variables で、"
    "sequence 内の参照もカタログ掲載 entity のみです。entity_id は script.<id> で確定します。"
    "deploy_script / delete_managed_script が公開されている場合は、それらも承認待ち提案を作るだけで、"
    "承認されるまで作成・削除されません。結果は list_managed_scripts の proposals で確認してください。"
    "削除できるのは管理台帳に作成記録があるスクリプトだけです。"
    "ヘルパーについては validate_helper / preview_helper / list_managed_helpers が使えます（H1 は input_boolean のみ）。"
    "ヘルパー定義は {id?, icon?} で、表示名は id と同一になり、entity_id は input_boolean.<id> で確定します。"
    "deploy_helper / delete_managed_helper が公開されている場合は、それらも承認待ち提案を作るだけで、"
    "結果は list_managed_helpers の proposals で確認してください。"
    "削除できるのは管理台帳に作成記録があるヘルパーだけです。"
)


def build_mcp(settings: Settings, svc: Services) -> FastMCP:
    mcp = FastMCP(
        "ha-automation",
        instructions=INSTRUCTIONS,
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        log_level=settings.log_level.upper(),
        # Tailscale Service 経由では Host ヘッダがカスタム FQDN になるため無効化。Bearer 認証で保護する。
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def get_entity_catalog(
        domains: list[str] | None = None,
        areas: list[str] | None = None,
        tags: list[str] | None = None,
        include_attributes: bool = True,
    ) -> dict[str, Any]:
        """許可リストに載っている entity だけを返す（フィルタは AND、各配列内は OR）。
        各要素: entity_id, friendly_name, description, area, state, tags（include_attributes=true なら device_class, unit_of_measurement も）。
        description はカタログ YAML の `description:` の値で、その entity が具体的に何なのかを
        自然言語で説明したもの（未記入なら null）。entity を選ぶ判断材料として使うこと。"""
        return await svc.catalog.get_catalog(domains, areas, tags, include_attributes)

    @mcp.tool()
    async def validate_automation(automation: dict[str, Any]) -> dict[str, Any]:
        """オートメーション定義を検証する（書き込みなし）。{valid, errors, warnings} を返す。"""
        v = await svc.validator.validate(automation)
        return v.public()

    @mcp.tool()
    async def preview_deployment(automation: dict[str, Any]) -> dict[str, Any]:
        """検証に加え、作成される正規化済み定義・ID・予測 entity_id を返す（書き込みなし）。"""
        return await svc.deployer.preview(automation)

    @mcp.tool()
    async def list_managed_automations() -> dict[str, Any]:
        """本サーバーが作成した（ID が muse_ で始まる）オートメーションの一覧。"""
        try:
            out = await svc.deployer.list_managed()
        except HAError as e:
            out = {"count": 0, "automations": [], "errors": [{"code": "ha_error", "message": str(e)}]}
        if settings.deploy_enabled and svc.proposals is not None:
            # 承認状況は種別ごとに分けて返す（スクリプト分は list_managed_scripts、
            # ヘルパー分は list_managed_helpers 側）
            out["proposals"] = [p for p in svc.proposals.summaries()
                                if not str(p.get("kind", "")).startswith(("script_", "helper_"))]
        return out

    @mcp.tool()
    async def get_automation_definition(automation_id: str) -> dict[str, Any]:
        """管理対象（muse_ 始まり）のオートメーションの現在の定義を読み戻す（読み取り専用・書き込みなし）。

        HA の config API に保存されている定義をそのまま返し、あわせて定義中の
        entity_id 参照の一覧（referenced_entities）も返す。entity の改名などで
        参照が古いままになっていないかの確認に使うこと。管理対象外の ID は
        not_managed エラーで拒否し、存在しない ID は found=false で返す。"""
        try:
            definition = await svc.deployer.current_definition(automation_id)
        except NotManaged as e:
            return {"automation_id": automation_id, "found": False, "definition": None,
                    "referenced_entities": [],
                    "errors": [{"code": "not_managed", "message": str(e)}]}
        except HAError as e:
            return {"automation_id": automation_id, "found": False, "definition": None,
                    "referenced_entities": [],
                    "errors": [{"code": "ha_error", "message": str(e)}]}
        if definition is None:
            return {"automation_id": automation_id, "found": False, "definition": None,
                    "referenced_entities": [],
                    "errors": [{"code": "not_found",
                                "message": f"{automation_id} は存在しません"}]}
        return {"automation_id": automation_id, "found": True, "definition": definition,
                "referenced_entities": _scan_entities(definition)}

    # ---- スクリプト Phase S1（読み取り・検証のみ、フラグ OFF でも公開） ----
    @mcp.tool()
    async def validate_script(script: dict[str, Any]) -> dict[str, Any]:
        """スクリプト定義を検証する（書き込みなし）。{valid, errors, warnings} を返す。
        sequence の entity 参照・サービスポリシーはオートメーションと同じ検証が効く。"""
        v = await svc.script_validator.validate(script)
        return v.public()

    @mcp.tool()
    async def preview_script(script: dict[str, Any]) -> dict[str, Any]:
        """検証に加え、作成される正規化済み定義・ID・確定する entity_id（script.<id>）を返す（書き込みなし）。"""
        return await svc.script_manager.preview(script)

    @mcp.tool()
    async def list_managed_scripts() -> dict[str, Any]:
        """管理対象スクリプトの一覧（読み取りのみ）。

        管理対象とは「アドオンの管理台帳に作成記録があり、config API で定義が読み戻せ、
        states に存在する」ものだけ。script.muse_ 接頭辞が一致するだけのものは管理対象に
        含めず、件数だけ unverified_count で返す。定義が作成後に変わったものは
        definition_changed_since_creation=true で知らせる。"""
        try:
            out = await svc.script_manager.list_managed()
        except HAError as e:
            out = {"count": 0, "scripts": [], "errors": [{"code": "ha_error", "message": str(e)}]}
        if settings.deploy_enabled and svc.proposals is not None:
            out["proposals"] = [p for p in svc.proposals.summaries()
                                if str(p.get("kind", "")).startswith("script_")]
        return out

    # ---- ヘルパー（H1: input_boolean、H2: 単純ヘルパー全般。読み取り・検証は常時公開） ----
    @mcp.tool()
    async def validate_helper(definition: dict[str, Any],
                              domain: str = "input_boolean") -> dict[str, Any]:
        """ヘルパー定義を検証する（書き込みなし）。domain は input_boolean / input_number /
        input_text / input_select / input_datetime / counter / timer / schedule。
        共通キーは {id?, icon?}、ドメイン固有キーはドメインごとに異なる。
        表示名は ID と同一になり、entity_id は <domain>.<id> で確定する。"""
        v = await svc.helper_validator.validate(definition, domain)
        return v.to_dict()

    @mcp.tool()
    async def preview_helper(definition: dict[str, Any],
                             domain: str = "input_boolean") -> dict[str, Any]:
        """検証に加え、作成される正規化済み定義・確定する entity_id を返す（書き込みなし）。"""
        return await svc.helper_manager.preview(definition, domain)

    @mcp.tool()
    async def list_managed_helpers() -> dict[str, Any]:
        """管理対象ヘルパーの一覧（読み取りのみ）。

        管理対象とは「管理台帳に作成記録があり、StorageCollection に実体があり、
        states に存在する」ものだけ。台帳にない muse_ ヘルパーは unverified_count に計上する。"""
        try:
            out = await svc.helper_manager.list_managed()
        except HAError as e:
            out = {"count": 0, "helpers": [], "errors": [{"code": "ha_error", "message": str(e)}]}
        if settings.deploy_enabled and svc.proposals is not None:
            out["proposals"] = [p for p in svc.proposals.summaries()
                                if str(p.get("kind", "")).startswith("helper_")]
        return out

    if settings.deploy_enabled and settings.helpers_enabled:

        @mcp.tool()
        async def deploy_helper(definition: dict[str, Any],
                                domain: str = "input_boolean") -> dict[str, Any]:
            """検証を通過したヘルパー定義の「承認待ち提案」を作る。HA にはまだ何も書き込まれない。
            ユーザーが承認画面で承認した時点で作成され、管理台帳に記録される。"""
            return await svc.proposals.propose_helper(definition, domain)

        @mcp.tool()
        async def delete_managed_helper(entity_id: str) -> dict[str, Any]:
            """管理台帳に作成記録があるヘルパーの「削除の承認待ち提案」を作る。
            まだ削除されない。ユーザーが承認画面で承認したときだけ、削除前にバックアップを取って削除する。"""
            return await svc.proposals.propose_helper_delete(entity_id)

    if settings.deploy_enabled and settings.scripts_enabled:

        @mcp.tool()
        async def deploy_script(script: dict[str, Any]) -> dict[str, Any]:
            """検証を通過したスクリプト定義の「承認待ち提案」を作る。HA にはまだ何も書き込まれない。
            ユーザーが承認画面で承認した時点で、作成のみ（既存 ID の上書き不可）で作成され、
            管理台帳に記録される。"""
            return await svc.proposals.propose_script(script)

        @mcp.tool()
        async def delete_managed_script(script_id: str) -> dict[str, Any]:
            """管理台帳に作成記録があるスクリプトの「削除の承認待ち提案」を作る。
            まだ削除されない。ユーザーが承認画面で承認したときだけ、削除前にバックアップを取って削除する。"""
            return await svc.proposals.propose_script_delete(script_id)

    if settings.deploy_enabled:

        @mcp.tool()
        async def deploy_automation(automation: dict[str, Any]) -> dict[str, Any]:
            """検証を通過した定義の「承認待ち提案」を作る。HA にはまだ何も書き込まれない。
            ユーザーが承認画面で承認した時点で、作成のみ（既存 ID の上書き不可）でデプロイされる。"""
            return await svc.proposals.propose(automation)

        @mcp.tool()
        async def delete_managed_automation(automation_id: str) -> dict[str, Any]:
            """本サーバー管理（muse_ 始まり）のオートメーションの「削除の承認待ち提案」を作る。
            まだ削除されない。ユーザーが承認画面で承認したときだけ、削除前にバックアップを取って削除する。"""
            return await svc.proposals.propose_delete(automation_id)

        @mcp.tool()
        async def rollback_deployment(automation_id: str) -> dict[str, Any]:
            """直近の操作を取り消す「ロールバックの承認待ち提案」を作る（deploy→削除、delete→再作成）。
            まだ実行されない。ユーザーが承認画面で承認したときだけ実行される。"""
            return await svc.proposals.propose_rollback(automation_id)

    return mcp
