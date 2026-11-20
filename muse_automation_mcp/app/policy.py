"""サービス／トリガー種別のポリシー。既定は最小許可、追加は policy.yaml で明示した場合のみ。"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .yamlcache import YamlFile

# 既定で許可するトリガー種別。event / webhook など外部から起動しうるものは既定で拒否し、
# policy.yaml の extra_allowed_trigger_types に明示した場合のみ許可する（docs/DESIGN.md と一致）。
DEFAULT_ALLOWED_TRIGGER_TYPES = frozenset(
    {"state", "numeric_state", "time", "time_pattern", "sun", "zone", "template",
     "homeassistant", "calendar"}
)

# 対象 entity が無くても既定で許可（動作確認手順で使う無害なもの）
DEFAULT_ALLOWED_SERVICES = frozenset(
    {"persistent_notification.create", "persistent_notification.dismiss"}
)

DANGEROUS_SERVICES_EXACT = frozenset(
    {
        "homeassistant.restart", "homeassistant.stop", "homeassistant.reload_all",
        "homeassistant.reload_core_config", "homeassistant.reload_config_entry",
        "homeassistant.reload_custom_templates", "homeassistant.set_location",
        "homeassistant.check_config", "homeassistant.save_persistent_states",
    }
)
# 物理的に高影響なドメイン、および外部送信につながる notify。同ドメインでも既定では許可せず、
# policy.yaml の extra_allowed_services にサービス単位で明示した場合のみ許可する。
# （例: lock.unlock, alarm_control_panel.alarm_disarm, notify.mobile_app_my_phone。
# カタログに entity を載せただけでは許可されない。notify は 2026-11-20 監査で追加:
# 通知先によっては HA から外部へ送信できるため、README の「明示した場合のみ許可」と一致させる）
SENSITIVE_SERVICE_DOMAINS = frozenset({"lock", "alarm_control_panel", "notify"})

DANGEROUS_SERVICE_DOMAINS = frozenset(
    {"shell_command", "python_script", "pyscript", "hassio", "rest_command",
     "command_line", "script", "logger", "system_log", "recorder", "backup", "update",
     "automation", "config"}
)


@dataclass(frozen=True)
class Policy:
    extra_allowed_services: tuple[str, ...] = ()
    extra_allowed_trigger_types: frozenset[str] = frozenset()
    # 所有者が明示的に拒否するサービス（fnmatch パターン可）。許可判定より優先する:
    # 同ドメインの既定許可・extra_allowed_services・既定許可サービスのいずれよりも先に効く。
    # 外部連携を持つ統合のドメインなどを、カタログに載せたまま個別に止めるための受け皿。
    denied_services: tuple[str, ...] = ()
    # policy.yaml の読み込み・検証に失敗したときの fail-closed 状態。
    # 壊れた設定（例: denied_services を文字列で書いた）で既定許可に戻ってしまうと
    # 「拒否したつもりが通る」ため、この状態ではすべてのサービスとトリガーを拒否する。
    load_failed: bool = False

    @property
    def allowed_trigger_types(self) -> frozenset[str]:
        if self.load_failed:
            return frozenset()
        return DEFAULT_ALLOWED_TRIGGER_TYPES | self.extra_allowed_trigger_types

    def _explicit(self, service: str) -> bool:
        if service in DEFAULT_ALLOWED_SERVICES:
            return True
        return any(fnmatch.fnmatchcase(service, pat) for pat in self.extra_allowed_services)

    def _explicit_exact(self, service: str) -> bool:
        """高影響ドメイン用: ワイルドカード（* ? [）を含むパターンは一致とみなさず、
        サービス名の完全一致だけを許可する。"""
        return any(pat == service for pat in self.extra_allowed_services)

    def check_service(self, service: str, target_domains: set[str]) -> tuple[bool, str, str]:
        """(許可?, エラーコード, 理由)"""
        domain = service.split(".", 1)[0]
        if self.load_failed:
            return (
                False,
                "policy_load_failed",
                "policy.yaml を読み込めなかったため、すべてのサービスを拒否しています"
                "（設定ファイルの形式を直してください）",
            )
        if any(fnmatch.fnmatchcase(service, pat) for pat in self.denied_services):
            return (
                False,
                "service_denied",
                f"policy.yaml の denied_services で明示的に拒否されています: {service}",
            )
        if domain in SENSITIVE_SERVICE_DOMAINS:
            # ワイルドカードで広げられないよう、完全一致の明示だけを先に判定する
            if self._explicit_exact(service):
                return True, "", ""
            return (
                False,
                "service_sensitive",
                f"高影響／外部送信につながるドメインのため既定で禁止されています: {service}"
                "（カタログへの掲載だけでは許可されません。必要なら policy.yaml の "
                "extra_allowed_services にワイルドカードなしでサービス単位で明示追加してください）",
            )
        if self._explicit(service):
            return True, "", ""
        if service in DANGEROUS_SERVICES_EXACT or domain in DANGEROUS_SERVICE_DOMAINS:
            return (
                False,
                "service_dangerous",
                f"危険なサービスのため既定で禁止されています: {service}"
                "（必要なら policy.yaml の extra_allowed_services に明示追加）",
            )
        if not target_domains:
            return (
                False,
                "service_not_allowed",
                f"{service} は対象 entity を明示していないため許可されません"
                "（既定では entity_id を明示した同一ドメインのサービスのみ許可）",
            )
        if target_domains != {domain}:
            return (
                False,
                "service_not_allowed",
                f"{service} の対象 entity のドメイン {sorted(target_domains)} と"
                f"サービスのドメイン {domain!r} が一致しません"
                "（policy.yaml の extra_allowed_services に追加すれば許可）",
            )
        return True, "", ""


def _str_list(raw: dict, key: str) -> list:
    """リスト形式を厳密に検証する。文字列は反復可能なので、要素チェックだけでは
    `denied_services: "notify.*"` のような書き間違いが文字単位に化けて拒否が無効化される。
    リストでなければ設定エラーにする（2026-11-20 監査対応）。"""
    v = raw.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(
            f"policy.yaml の {key} はリストで指定してください"
            "（1件だけでも [\"...\"] の形式が必要です。文字列のままでは設定エラーになります）")
    if not all(isinstance(x, str) for x in v):
        raise ValueError(f"policy.yaml の {key} の要素は文字列にしてください")
    return v


def _parse(raw) -> Policy:
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        raise ValueError("policy.yaml はマッピングである必要があります")
    return Policy(extra_allowed_services=tuple(_str_list(raw, "extra_allowed_services")),
                  extra_allowed_trigger_types=frozenset(_str_list(raw, "extra_allowed_trigger_types")),
                  denied_services=tuple(_str_list(raw, "denied_services")))


class PolicyLoader:
    def __init__(self, path: Path):
        # ファイルが無い場合は既定ポリシー（Policy()）、読み込み・検証に失敗した場合は
        # 既定許可へ戻さず全拒否の fail-closed ポリシーにする（2026-11-20 監査対応）
        self._file = YamlFile(path, _parse, Policy(),
                              error_value=Policy(load_failed=True))

    def get(self) -> Policy:
        return self._file.get()
