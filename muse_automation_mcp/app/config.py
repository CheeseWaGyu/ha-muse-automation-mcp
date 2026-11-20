"""設定の読み込み。

優先順位: 環境変数 > /data/options.json（アドオン） > 既定値。
HA トークンはソース・設定ファイル・ログに平文で残さない。
- アドオン形態: SUPERVISOR_TOKEN（Supervisor が環境変数で注入）
- 単体形態: HA_TOKEN（環境変数）または HA_TOKEN_FILE（権限 0600 のファイル）
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ADDON_OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_CORE_API = "http://supervisor/core/api"


class ConfigError(Exception):
    pass


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def read_secret_file(path: Path) -> str:
    """0600 相当（グループ/その他に権限なし）のシークレットファイルだけ読む。"""
    try:
        st = path.stat()
    except FileNotFoundError as e:
        raise ConfigError(f"シークレットファイルが見つかりません: {path}") from e
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"シークレットファイルの権限が緩すぎます（0600 にしてください）: {path}"
        )
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Settings:
    mode: str  # "addon" | "standalone"
    ha_api_base: str  # 例: http://supervisor/core/api / http://homeassistant:8123/api
    ha_token: str = ""  # repr に出さない（下の __repr__ 参照）
    host: str = "0.0.0.0"
    port: int = 8099
    mcp_path: str = "/mcp/v1"
    data_dir: Path = Path("/data")
    config_dir: Path = Path("/config")
    catalog_path: Path = Path("/config/entity_catalog.yaml")
    policy_path: Path = Path("/config/policy.yaml")
    token_file: Path = Path("/config/mcp_token")
    mcp_token_env: str = ""  # 環境変数 MCP_TOKEN で明示指定する場合
    deploy_enabled: bool = False
    # ヘルパー／スクリプト拡張の段階フラグ（設計メモ helper-script-mcp-design.md）。
    # S1（スクリプト list/validate/preview）はフラグ OFF でも公開する。
    # scripts_enabled は S2 の deploy_script / delete_managed_script の公開条件に、
    # helpers_enabled は H1 以降のヘルパー作成系の公開条件に使う（いずれも deploy_enabled も必要）。
    scripts_enabled: bool = False
    helpers_enabled: bool = False
    backup_generations: int = 20
    log_level: str = "info"
    entity_wait_seconds: float = 10.0
    entity_wait_interval: float = 0.5
    # 承認 UI（Ingress）
    ui_host: str = "0.0.0.0"
    ui_port: int = 8100
    ui_allowed_clients: tuple[str, ...] = ("172.30.32.2",)  # Supervisor（Ingress プロキシ）
    approver_token_file: Path = Path("/config/approver_token")  # 単体形態の Basic 認証用
    proposal_ttl_hours: float = 24.0
    max_pending_proposals: int = 20

    def __repr__(self) -> str:  # トークンをうっかり出力しない
        return (
            f"Settings(mode={self.mode!r}, ha_api_base={self.ha_api_base!r}, "
            f"host={self.host!r}, port={self.port}, mcp_path={self.mcp_path!r}, "
            f"deploy_enabled={self.deploy_enabled}, scripts_enabled={self.scripts_enabled}, "
            f"helpers_enabled={self.helpers_enabled})"
        )

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.log"

    @property
    def proposals_dir(self) -> Path:
        return self.data_dir / "proposals"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_settings(
    env: Mapping[str, str] | None = None,
    options_path: Path = ADDON_OPTIONS_PATH,
) -> Settings:
    env = os.environ if env is None else env

    options: dict = {}
    if options_path.exists():
        try:
            options = json.loads(options_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"options.json を読めません: {e}") from e

    supervisor_token = env.get("SUPERVISOR_TOKEN", "")
    explicit_ha = bool(env.get("HA_URL") or env.get("HA_TOKEN") or env.get("HA_TOKEN_FILE"))

    if supervisor_token and not explicit_ha:
        mode = "addon"
        api_base = SUPERVISOR_CORE_API
        ha_token = supervisor_token
        default_data, default_config = "/data", "/config"
    else:
        mode = "standalone"
        ha_url = env.get("HA_URL", "").rstrip("/")
        if not ha_url:
            raise ConfigError("単体形態では HA_URL（例: http://homeassistant:8123）が必須です")
        api_base = ha_url + "/api"
        if env.get("HA_TOKEN"):
            ha_token = env["HA_TOKEN"].strip()
        elif env.get("HA_TOKEN_FILE"):
            ha_token = read_secret_file(Path(env["HA_TOKEN_FILE"]))
        else:
            raise ConfigError("単体形態では HA_TOKEN または HA_TOKEN_FILE（0600）が必須です")
        default_data, default_config = "./data", "./config"
    if not ha_token:
        raise ConfigError("HA へ接続するためのトークンが空です")

    data_dir = Path(env.get("DATA_DIR", default_data))
    config_dir = Path(env.get("CONFIG_DIR", default_config))

    def pick(env_key: str, opt_key: str, default):
        if env_key in env:
            return env[env_key]
        return options.get(opt_key, default)

    try:
        generations = int(pick("BACKUP_GENERATIONS", "backup_generations", 20))
        port = int(env.get("PORT", 8099))
        ui_port = int(env.get("UI_PORT", 8100))
        ttl_hours = float(pick("PROPOSAL_TTL_HOURS", "proposal_ttl_hours", 24))
        max_pending = int(env.get("MAX_PENDING_PROPOSALS", 20))
    except ValueError as e:
        raise ConfigError(f"数値設定が不正です: {e}") from e
    if generations < 1:
        raise ConfigError("backup_generations は 1 以上にしてください")

    if ui_port == port:
        raise ConfigError("UI_PORT と PORT は別にしてください（承認 UI と MCP は別ポート）")
    if ttl_hours <= 0 or max_pending < 1:
        raise ConfigError("proposal_ttl_hours / MAX_PENDING_PROPOSALS が不正です")

    mcp_path = env.get("MCP_PATH", "/mcp/v1")
    if not mcp_path.startswith("/"):
        raise ConfigError("MCP_PATH は / で始めてください")

    return Settings(
        mode=mode,
        ha_api_base=api_base,
        ha_token=ha_token,
        host=env.get("HOST", "0.0.0.0"),
        port=port,
        mcp_path=mcp_path,
        data_dir=data_dir,
        config_dir=config_dir,
        catalog_path=Path(env.get("CATALOG_PATH", config_dir / "entity_catalog.yaml")),
        policy_path=Path(env.get("POLICY_PATH", config_dir / "policy.yaml")),
        token_file=Path(env.get("MCP_TOKEN_FILE", config_dir / "mcp_token")),
        mcp_token_env=env.get("MCP_TOKEN", "").strip(),
        deploy_enabled=_as_bool(pick("DEPLOY_ENABLED", "deploy_enabled", False)),
        scripts_enabled=_as_bool(pick("SCRIPTS_ENABLED", "scripts_enabled", False)),
        helpers_enabled=_as_bool(pick("HELPERS_ENABLED", "helpers_enabled", False)),
        backup_generations=generations,
        log_level=str(pick("LOG_LEVEL", "log_level", "info")).lower(),
        # 単体形態では承認 UI は localhost のみ + Basic 認証。アドオンでは Supervisor からのみ受ける
        ui_host=env.get("UI_HOST", "0.0.0.0" if mode == "addon" else "127.0.0.1"),
        ui_port=ui_port,
        ui_allowed_clients=tuple(
            x.strip() for x in env.get("UI_ALLOWED_CLIENTS", "172.30.32.2").split(",") if x.strip()
        ),
        approver_token_file=Path(env.get("APPROVER_TOKEN_FILE", config_dir / "approver_token")),
        proposal_ttl_hours=ttl_hours,
        max_pending_proposals=max_pending,
    )
