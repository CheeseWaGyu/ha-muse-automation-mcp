"""エントリポイント: python -m app.main"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import sys
from pathlib import Path

import uvicorn

from .audit import AuditLog
from .auth import (BearerAuthMiddleware, TokenError, load_or_create_approver_token,
                   load_or_create_token)
from .backup import BackupStore
from .catalog import CatalogService
from .config import ConfigError, Settings, load_settings
from .deployer import Deployer
from .ha_client import HAClient
from .policy import PolicyLoader
from .proposals import ProposalService
from .tools import Services, build_mcp
from .ui import create_ui_app
from .validation import AutomationValidator

log = logging.getLogger("muse")


def build_services(settings: Settings, ha: HAClient) -> Services:
    catalog = CatalogService(settings.catalog_path, ha)
    validator = AutomationValidator(ha, catalog, PolicyLoader(settings.policy_path))
    deployer = Deployer(settings, ha, validator, AuditLog(settings.audit_path),
                        BackupStore(settings.backup_dir, settings.backup_generations))
    proposals = ProposalService(settings, validator, deployer, deployer._audit)
    return Services(catalog, deployer, validator, proposals)


def create_app(settings: Settings, ha: HAClient | None = None, svc: Services | None = None):
    """MCP 用アプリ（Bearer 認証）。承認 UI は別ポート・別アプリ（create_ui_app）。"""
    token = load_or_create_token(settings)
    ha = ha or HAClient(settings.ha_api_base, settings.ha_token)
    mcp = build_mcp(settings, svc or build_services(settings, ha))
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


class _Server(uvicorn.Server):
    """シグナルは main 側でまとめて処理する（複数サーバー同時起動のため）。"""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def _serve_all(servers: list[_Server]) -> None:
    loop = asyncio.get_running_loop()

    def stop() -> None:
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    await asyncio.gather(*(s.serve() for s in servers))


def _copy_samples(settings: Settings) -> None:
    """編集用のサンプルを設定ディレクトリへ置く（本番ファイルは作らない＝許可リストは空のまま）。"""
    here = Path(__file__).resolve().parent.parent
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("entity_catalog.sample.yaml", "policy.sample.yaml"):
        src, dst = here / name, settings.config_dir / name
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _copy_samples(settings)
    ha = HAClient(settings.ha_api_base, settings.ha_token)
    svc = build_services(settings, ha)
    try:
        app = create_app(settings, ha, svc)
        approver = load_or_create_approver_token(settings) \
            if settings.deploy_enabled and settings.mode != "addon" else ""
    except TokenError as e:
        log.error("%s", e)
        sys.exit(2)
    log.info("起動: %s / deploy_enabled=%s / path=%s", settings, settings.deploy_enabled, settings.mcp_path)
    if not settings.catalog_path.exists():
        log.warning("%s がありません。許可リストは空（すべて拒否）です。サンプルをコピーして編集してください",
                    settings.catalog_path)

    def cfg(a, host, port):
        return uvicorn.Config(a, host=host, port=port, log_level="warning", access_log=False)

    servers = [_Server(cfg(app, settings.host, settings.port))]
    if settings.deploy_enabled:  # 承認 UI はデプロイ有効時のみ（別ポート・別認証）
        ui = create_ui_app(settings, svc.proposals, ha, approver)
        servers.append(_Server(cfg(ui, settings.ui_host, settings.ui_port)))
        log.info("承認 UI: %s:%s（%s）", settings.ui_host, settings.ui_port,
                 "Ingress" if settings.mode == "addon" else "Basic 認証・承認用トークン")
    asyncio.run(_serve_all(servers))


if __name__ == "__main__":
    main()
