"""オートメーション定義の検証（fail-closed）。HA への書き込みは一切しない。

検証内容:
  - HA 2024.10+ 記法へ正規化（旧記法は警告つきで受け付け）
  - 上位キーのスキーマ（未知キーは拒否）
  - トリガー種別／条件種別／アクション種別の許可リスト
  - 参照されるすべての entity_id が (a) HA に実在 (b) カタログ許可リストにある
  - area_id / device_id 等による間接指定、テンプレートでの動的 entity 指定は拒否
  - サービスのポリシー（policy.py）
  - ID 形式（^muse_[a-z0-9_]+$）と既存 ID との重複
"""
from __future__ import annotations

import copy
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from .catalog import ENTITY_ID_RE, CatalogService
from .ha_client import HAClient, HAError
from .policy import PolicyLoader

ID_RE = re.compile(r"^muse_[a-z0-9_]+$")
MAX_ID_LEN = 100
SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

ALLOWED_CONDITIONS = {"state", "numeric_state", "template", "time", "sun", "zone",
                      "trigger", "and", "or", "not"}
ACTION_KINDS = ("action", "service", "delay", "wait_template", "wait_for_trigger", "condition",
                "choose", "if", "repeat", "sequence", "parallel", "variables", "stop",
                "set_conversation_response")
INDIRECT_TARGET_KEYS = {"device_id", "area_id", "floor_id", "label_id", "config_entry_id"}

TEMPLATE_BLOCK_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
ENTITY_FUNCS = ("states", "is_state", "is_state_attr", "state_attr", "has_value",
                "is_hidden_entity", "expand", "state_translated")
ENUMERATING_FUNCS = ("area_entities", "area_devices", "device_entities", "integration_entities",
                     "label_entities", "floor_entities", "config_entry_entities", "label_devices")
FUNC_CALL_RE = re.compile(
    r"\b(" + "|".join(ENTITY_FUNCS) + r")\s*\(\s*(?:([\"'])([^\"']*)\2\s*(?=[,)]))?"
)
ENUM_CALL_RE = re.compile(r"\b(" + "|".join(ENUMERATING_FUNCS) + r")\b")
FUNC_NAME_AS_STRING_RE = re.compile(r"[\"'](" + "|".join(ENTITY_FUNCS) + r")[\"']")
STATES_ATTR_RE = re.compile(r"\bstates\.([a-z0-9_]+)(?:\.([a-z0-9_]+))?")
BARE_STATES_RE = re.compile(r"\bstates\b(?!\s*[.(])")
QUOTED_LITERAL_RE = re.compile(r"[\"']([a-z0-9_]+\.[a-z0-9_]+)[\"']")


def slugify_ascii(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:max_len].strip("_") or "automation"


class _Top(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    alias: str = Field(min_length=1, max_length=200)
    description: str | None = None
    triggers: list[Any] = Field(min_length=1)
    conditions: list[Any] = Field(default_factory=list)
    actions: list[Any] = Field(min_length=1)
    mode: Literal["single", "restart", "queued", "parallel"] = "single"
    max: int | None = Field(default=None, ge=1, le=100)
    max_exceeded: Literal["silent", "critical", "fatal", "error", "warning", "info", "debug"] | None = None
    variables: dict[str, Any] | None = None
    trigger_variables: dict[str, Any] | None = None


@dataclass
class ValidationResult:
    valid: bool
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    automation_id: str | None = None
    normalized: dict | None = None  # HA へ送る定義（id を含まない）
    referenced_entities: list[str] = field(default_factory=list)
    id_supplied: bool = False

    def public(self) -> dict:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


class _Ctx:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.entities: set[str] = set()
        self.has_template = False
        self.template_literal_candidates: set[str] = set()

    def err(self, code: str, path: str, message: str) -> None:
        self.errors.append({"code": code, "path": path, "message": message})

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append({"code": code, "path": path, "message": message})


def _as_list(v: Any) -> Any:
    return v if isinstance(v, list) else [v]


# --------------------------------------------------------------------------- 正規化
def _norm_trigger_item(t: Any, path: str, ctx: _Ctx) -> Any:
    if isinstance(t, dict) and "platform" in t:
        if "trigger" in t:
            ctx.err("legacy_conflict", path, "`platform` と `trigger` が両方あります")
        else:
            t = {("trigger" if k == "platform" else k): v for k, v in t.items()}
            ctx.warn("legacy_normalized", path, "旧記法 `platform` を `trigger` に正規化しました")
    return t


def _norm_actions(steps: Any, path: str, ctx: _Ctx) -> Any:
    if not isinstance(steps, list):
        return steps
    out = []
    for i, s in enumerate(steps):
        p = f"{path}[{i}]"
        if isinstance(s, dict):
            s = dict(s)
            if "service" in s:
                if "action" in s:
                    ctx.err("legacy_conflict", p, "`service` と `action` が両方あります")
                else:
                    s = {("action" if k == "service" else k): v for k, v in s.items()}
                    ctx.warn("legacy_normalized", p, "旧記法 `service` を `action` に正規化しました")
            if "data_template" in s:
                ctx.err("legacy_unsupported", p, "`data_template` は未対応です。`data` を使ってください")
            for key in ("sequence", "then", "else", "default"):
                if key in s:
                    s[key] = _norm_actions(_as_list(s[key]), f"{p}.{key}", ctx)
            if isinstance(s.get("choose"), list):
                s["choose"] = [
                    {**o, "sequence": _norm_actions(_as_list(o["sequence"]), f"{p}.choose", ctx)}
                    if isinstance(o, dict) and "sequence" in o else o
                    for o in s["choose"]
                ]
            if isinstance(s.get("repeat"), dict) and "sequence" in s["repeat"]:
                s["repeat"] = {**s["repeat"],
                               "sequence": _norm_actions(_as_list(s["repeat"]["sequence"]), f"{p}.repeat", ctx)}
            if isinstance(s.get("parallel"), list):
                s["parallel"] = _norm_actions(s["parallel"], f"{p}.parallel", ctx)
        out.append(s)
    return out


def normalize_legacy(auto: dict, ctx: _Ctx) -> dict:
    auto = dict(auto)
    for old, new in (("trigger", "triggers"), ("condition", "conditions"), ("action", "actions")):
        if old in auto:
            if new in auto:
                ctx.err("legacy_conflict", old, f"`{old}` と `{new}` が両方あります")
            else:
                auto[new] = auto.pop(old)
                ctx.warn("legacy_normalized", old, f"旧記法 `{old}` を `{new}` に正規化しました")
    for key in ("triggers", "conditions", "actions"):
        if key in auto and auto[key] is not None:
            auto[key] = _as_list(auto[key])
    if isinstance(auto.get("triggers"), list):
        auto["triggers"] = [_norm_trigger_item(t, f"triggers[{i}]", ctx)
                            for i, t in enumerate(auto["triggers"])]
    if isinstance(auto.get("actions"), list):
        auto["actions"] = _norm_actions(auto["actions"], "actions", ctx)
    return auto


# --------------------------------------------------------------------------- 走査
def _entity_refs_from_value(value: Any, path: str, ctx: _Ctx) -> list[str]:
    """entity_id フィールドの値から ID を取り出す。取れない／不正なら検証エラー。"""
    out: list[str] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, str):
            ctx.err("invalid_entity_id", path, f"entity_id は文字列（またはそのリスト）である必要があります: {item!r}")
            continue
        if "{{" in item or "{%" in item:
            ctx.err("dynamic_entity", path, "テンプレートによる entity_id 指定は検証できないため禁止です")
            continue
        for part in (p.strip() for p in item.split(",")):
            if part.lower() in {"all", "none", ""}:
                ctx.err("invalid_entity_id", path, f"`{part or '(空)'}` による全体指定は禁止です。entity_id を明示してください")
            elif not ENTITY_ID_RE.match(part):
                ctx.err("invalid_entity_id", path, f"entity_id の形式が不正です: {part!r}")
            else:
                out.append(part)
    ctx.entities.update(out)
    return out


def _scan_template(s: str, path: str, ctx: _Ctx) -> None:
    blocks = TEMPLATE_BLOCK_RE.findall(s)
    if not blocks:
        return
    ctx.has_template = True
    for b in blocks:
        for m in FUNC_CALL_RE.finditer(b):
            func, _q, lit = m.group(1), m.group(2), m.group(3)
            if lit is None:
                ctx.err("dynamic_entity", path, f"テンプレート内の {func}() の引数が文字列リテラルではないため検証できません")
            elif not ENTITY_ID_RE.match(lit):
                ctx.err("dynamic_entity", path, f"テンプレート内の {func}({lit!r}) は有効な entity_id ではありません（動的指定は禁止）")
            else:
                ctx.entities.add(lit)
        for m in ENUM_CALL_RE.finditer(b):
            ctx.err("dynamic_entity", path, f"テンプレート内の {m.group(1)}() は entity を列挙するため禁止です")
        for m in FUNC_NAME_AS_STRING_RE.finditer(b):
            ctx.err("dynamic_entity", path, f"テンプレート内で関数名 {m.group(1)!r} を文字列で渡す書き方は検証できないため禁止です")
        for m in STATES_ATTR_RE.finditer(b):
            if m.group(2) is None:
                ctx.err("dynamic_entity", path, f"テンプレート内の states.{m.group(1)} はドメイン全体を参照するため禁止です")
            else:
                ctx.entities.add(f"{m.group(1)}.{m.group(2)}")
        if BARE_STATES_RE.search(b):
            ctx.err("dynamic_entity", path, "テンプレート内で `states` を直接列挙・添字参照する書き方は禁止です")
        for m in QUOTED_LITERAL_RE.finditer(b):  # 既知ドメインのものだけ後段で参照扱い
            ctx.template_literal_candidates.add(m.group(1))


def _scan(node: Any, path: str, ctx: _Ctx) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if k in INDIRECT_TARGET_KEYS:
                ctx.err("indirect_target", p,
                        f"`{k}` による間接指定は許可リストを検証できないため禁止です。entity_id を明示してください")
            elif k == "entity_id":
                _entity_refs_from_value(v, p, ctx)
            elif k == "zone" and isinstance(v, str):
                _entity_refs_from_value(v, p, ctx)
            elif k in ("above", "below") and isinstance(v, str) and not TEMPLATE_BLOCK_RE.search(v):
                try:
                    float(v)
                except ValueError:
                    _entity_refs_from_value(v, p, ctx)
            _scan(v, p, ctx)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan(v, f"{path}[{i}]", ctx)
    elif isinstance(node, str):
        _scan_template(node, path, ctx)


# --------------------------------------------------------------------------- 構造
class AutomationValidator:
    def __init__(self, ha: HAClient, catalog: CatalogService, policy: PolicyLoader):
        self._ha = ha
        self._catalog = catalog
        self._policy = policy

    # ---- 構造チェック -------------------------------------------------------
    def _check_triggers(self, triggers: Any, path: str, ctx: _Ctx) -> None:
        allowed = self._policy.get().allowed_trigger_types
        for i, t in enumerate(_as_list(triggers)):
            p = f"{path}[{i}]"
            if not isinstance(t, dict) or not isinstance(t.get("trigger"), str):
                ctx.err("invalid_trigger", p, "トリガーは `trigger:` キー（種別）を持つオブジェクトである必要があります")
            elif t["trigger"] not in allowed:
                ctx.err("trigger_not_allowed", p,
                        f"トリガー種別 {t['trigger']!r} は許可されていません（許可: {sorted(allowed)}）")

    def _check_conditions(self, conds: Any, path: str, ctx: _Ctx) -> None:
        for i, c in enumerate(_as_list(conds)):
            p = f"{path}[{i}]"
            if isinstance(c, str):
                continue  # テンプレート短縮記法（_scan で解析済み）
            if not isinstance(c, dict) or not isinstance(c.get("condition"), str):
                ctx.err("invalid_condition", p, "条件は `condition:` キーを持つオブジェクト（またはテンプレート文字列）である必要があります")
                continue
            if c["condition"] not in ALLOWED_CONDITIONS:
                ctx.err("condition_not_allowed", p, f"条件種別 {c['condition']!r} は許可されていません")
            elif c["condition"] in ("and", "or", "not"):
                self._check_conditions(c.get("conditions", []), f"{p}.conditions", ctx)

    def _call_targets(self, step: dict, path: str) -> set[str]:
        """アクション呼び出しの対象 entity（ドメイン判定用）。エラー報告は _scan 側。"""
        found: list[Any] = []
        tgt = step.get("target")
        if isinstance(tgt, dict) and "entity_id" in tgt:
            found.append(tgt["entity_id"])
        if "entity_id" in step:
            found.append(step["entity_id"])
        data = step.get("data")
        if isinstance(data, dict) and "entity_id" in data:
            found.append(data["entity_id"])
        out: set[str] = set()
        for f in found:
            for item in _as_list(f):
                if isinstance(item, str):
                    out.update(p.strip() for p in item.split(",") if ENTITY_ID_RE.match(p.strip()))
        return out

    def _check_actions(self, steps: Any, path: str, ctx: _Ctx) -> None:
        if not isinstance(steps, list):
            ctx.err("invalid_action", path, "アクションはリストである必要があります")
            return
        for i, step in enumerate(steps):
            p = f"{path}[{i}]"
            if not isinstance(step, dict):
                ctx.err("invalid_action", p, "アクションはオブジェクトである必要があります")
                continue
            kinds = [k for k in ACTION_KINDS if k in step]
            if not kinds:
                ctx.err("action_not_allowed", p,
                        "未対応のアクション種別です（event / scene / device アクション等は禁止）。"
                        f"許可: {list(ACTION_KINDS)}")
                continue
            if len(kinds) > 1:
                ctx.err("invalid_action", p, f"1 つのステップに複数の種別があります: {kinds}")
                continue
            kind = kinds[0]
            if kind == "action":
                svc = step["action"]
                if not isinstance(svc, str) or not SERVICE_RE.match(svc):
                    ctx.err("invalid_service", p, f"サービス名は `domain.service` 形式の固定文字列にしてください: {svc!r}")
                    continue
                ok, code, reason = self._policy.get().check_service(
                    svc, {e.split(".", 1)[0] for e in self._call_targets(step, p)}
                )
                if not ok:
                    ctx.err(code, p, reason)
            elif kind == "choose":
                opts = step["choose"]
                if not isinstance(opts, list):
                    ctx.err("invalid_action", p, "choose はリストである必要があります")
                    continue
                for j, o in enumerate(opts):
                    if not isinstance(o, dict):
                        ctx.err("invalid_action", f"{p}.choose[{j}]", "オブジェクトが必要です")
                        continue
                    self._check_conditions(o.get("conditions", []), f"{p}.choose[{j}].conditions", ctx)
                    self._check_actions(_as_list(o.get("sequence", [])), f"{p}.choose[{j}].sequence", ctx)
                if "default" in step:
                    self._check_actions(_as_list(step["default"]), f"{p}.default", ctx)
            elif kind == "if":
                self._check_conditions(step["if"], f"{p}.if", ctx)
                self._check_actions(_as_list(step.get("then", [])), f"{p}.then", ctx)
                if "else" in step:
                    self._check_actions(_as_list(step["else"]), f"{p}.else", ctx)
            elif kind == "repeat":
                rep = step["repeat"]
                if not isinstance(rep, dict):
                    ctx.err("invalid_action", p, "repeat はオブジェクトである必要があります")
                    continue
                for ck in ("while", "until"):
                    if ck in rep:
                        self._check_conditions(rep[ck], f"{p}.repeat.{ck}", ctx)
                self._check_actions(_as_list(rep.get("sequence", [])), f"{p}.repeat.sequence", ctx)
            elif kind == "sequence":
                self._check_actions(_as_list(step["sequence"]), f"{p}.sequence", ctx)
            elif kind == "parallel":
                for j, item in enumerate(_as_list(step["parallel"])):
                    if isinstance(item, dict) and "sequence" in item and not any(k in item for k in ACTION_KINDS if k != "sequence"):
                        self._check_actions(_as_list(item["sequence"]), f"{p}.parallel[{j}].sequence", ctx)
                    else:
                        self._check_actions([item], f"{p}.parallel[{j}]", ctx)
            elif kind == "wait_for_trigger":
                self._check_triggers(step["wait_for_trigger"], f"{p}.wait_for_trigger", ctx)
            elif kind == "condition":
                self._check_conditions([step], p, ctx)

    # ---- 本体 ---------------------------------------------------------------
    async def validate(self, automation: Any) -> ValidationResult:
        ctx = _Ctx()
        if not isinstance(automation, dict):
            ctx.err("invalid_type", "", "automation はオブジェクトである必要があります")
            return ValidationResult(False, ctx.errors, ctx.warnings)

        auto = normalize_legacy(copy.deepcopy(automation), ctx)

        try:
            top = _Top.model_validate(auto)
        except PydanticValidationError as e:
            for er in e.errors():
                loc = ".".join(str(x) for x in er["loc"])
                ctx.err("schema", loc, er["msg"] if er["type"] != "extra_forbidden"
                        else f"未対応のキーです: {loc}")
            return ValidationResult(False, ctx.errors, ctx.warnings)

        self._check_triggers(top.triggers, "triggers", ctx)
        self._check_conditions(top.conditions, "conditions", ctx)
        self._check_actions(top.actions, "actions", ctx)

        body = {k: v for k, v in top.model_dump(exclude_none=True).items() if k != "id"}
        if not body.get("conditions"):
            body.pop("conditions", None)
        _scan(body, "", ctx)

        # ID
        supplied = top.id
        automation_id: str | None = None
        if supplied is not None:
            if not ID_RE.match(supplied) or len(supplied) > MAX_ID_LEN:
                ctx.err("invalid_id", "id", f"ID は ^muse_[a-z0-9_]+$（{MAX_ID_LEN} 文字以内）である必要があります: {supplied!r}")
            else:
                automation_id = supplied

        # entity の実在 + 許可リスト
        allow = self._catalog.allowlist()
        try:
            states = await self._ha.get_states()
        except HAError as e:
            ctx.err("ha_unreachable", "", f"HA の状態を取得できず、検証を完了できません（fail-closed）: {e}")
            states = []
        existing = {s["entity_id"] for s in states}
        known_domains = {e.split(".", 1)[0] for e in existing}
        for lit in ctx.template_literal_candidates:
            if lit.split(".", 1)[0] in known_domains:
                ctx.entities.add(lit)
        if not allow:
            ctx.err("catalog_empty", "", "カタログ許可リストが空（または未設定）のため、すべての参照が拒否されます")
        # 状態一覧が空でも照合は省略しない（空 = 全 entity が「存在しない」として fail-closed）
        for eid in sorted(ctx.entities):
            if eid not in allow:
                ctx.err("entity_not_allowed", eid, f"{eid} はカタログ許可リストにありません")
            elif eid not in existing:
                ctx.err("entity_not_found", eid, f"{eid} は HA に存在しません")

        if ctx.has_template:
            ctx.warn("template_partial_analysis", "",
                     "テンプレートは静的解析で entity 参照を検出しています。複雑な記述は検出漏れの可能性があるため、"
                     "可能な限り固定の entity_id を使ってください")

        # 重複チェック（生成 ID も衝突確認）
        try:
            if automation_id is not None:
                if await self._ha.get_automation_config(automation_id) is not None:
                    ctx.err("id_exists", "id", f"ID {automation_id} は既に存在します（上書きはできません）")
            elif supplied is None:
                base = f"muse_{slugify_ascii(top.alias)}"
                for _ in range(5):
                    cand = f"{base}_{secrets.token_hex(3)}"
                    if await self._ha.get_automation_config(cand) is None:
                        automation_id = cand
                        break
                else:
                    ctx.err("id_collision", "id", "一意な ID を生成できませんでした")
        except HAError as e:
            ctx.err("ha_unreachable", "id", f"既存 ID の確認に失敗しました: {e}")

        return ValidationResult(
            valid=not ctx.errors,
            errors=ctx.errors,
            warnings=ctx.warnings,
            automation_id=automation_id,
            normalized=body if not ctx.errors else None,
            referenced_entities=sorted(ctx.entities),
            id_supplied=supplied is not None,
        )
