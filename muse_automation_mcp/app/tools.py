"""MCP ツール定義。ツールは 7 つだけ（デプロイ系 3 つは DEPLOY_ENABLED=true のときのみ公開）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .catalog import CatalogService
from .config import Settings
from .deployer import Deployer
from .ha_client import HAError


@dataclass
class Services:
    catalog: CatalogService
    deployer: Deployer
    validator: Any
    proposals: Any = None


INSTRUCTIONS = (
    "Home Assistant の automation を追加専用でデプロイするサーバーです。"
    "まず get_entity_catalog で使える entity を確認し、validate_automation → preview_deployment で検証してから"
    "（有効な場合のみ）deploy_automation を呼んでください。deploy_automation は「承認待ちの提案」を作るだけで、"
    "ユーザーが HA の承認画面で承認するまでデプロイされません（承認・却下は Muse からはできません）。"
    "delete_managed_automation / rollback_deployment も同様に承認待ち提案を作るだけで、承認されるまで実行されません。"
    "結果は list_managed_automations の proposals で確認してください。定義は HA 2024.10+ 記法"
    "（alias, triggers, conditions, actions, mode）で、トリガーは `trigger:`、アクションは `action:` を使います。"
    "参照できるのはカタログに載った entity_id のみで、area_id/device_id 指定は使えません。"
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
        各要素: entity_id, friendly_name, area, state, tags（include_attributes=true なら device_class, unit_of_measurement も）。"""
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
            out["proposals"] = svc.proposals.summaries()  # 承認待ち・承認・却下・失敗の状況（定義本体は含めない）
        return out

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
