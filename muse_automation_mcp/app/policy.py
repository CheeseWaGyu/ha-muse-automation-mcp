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
# 物理的に高影響なドメイン。同ドメインでも既定では許可せず、
# policy.yaml の extra_allowed_services にサービス単位で明示した場合のみ許可する。
# （例: lock.unlock, alarm_control_panel.alarm_disarm。カタログに entity を載せただけでは許可されない）
SENSITIVE_SERVICE_DOMAINS = frozenset({"lock", "alarm_control_panel"})

DANGEROUS_SERVICE_DOMAINS = frozenset(
    {"shell_command", "python_script", "pyscript", "hassio", "rest_command",
     "command_line", "script", "logger", "system_log", "recorder", "backup", "update",
     "automation", "config"}
)


@dataclass(frozen=True)
class Policy:
    extra_allowed_services: tuple[str, ...] = ()
    extra_allowed_trigger_types: frozenset[str] = frozenset()

    @property
    def allowed_trigger_types(self) -> frozenset[str]:
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
        if domain in SENSITIVE_SERVICE_DOMAINS:
            # ワイルドカードで広げられないよう、完全一致の明示だけを先に判定する
            if self._explicit_exact(service):
                return True, "", ""
            return (
                False,
                "service_sensitive",
                f"物理的に影響の大きいドメインのため既定で禁止されています: {service}"
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


def _parse(raw) -> Policy:
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        raise ValueError("policy.yaml はマッピングである必要があります")
    svc = raw.get("extra_allowed_services") or []
    trg = raw.get("extra_allowed_trigger_types") or []
    if not all(isinstance(x, str) for x in [*svc, *trg]):
        raise ValueError("policy.yaml のリスト要素は文字列にしてください")
    return Policy(tuple(svc), frozenset(trg))


class PolicyLoader:
    def __init__(self, path: Path):
        self._file = YamlFile(path, _parse, Policy())

    def get(self) -> Policy:
        return self._file.get()
