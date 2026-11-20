"""カタログ許可リスト（YAML）と、HA の状態・area との照合。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .ha_client import HAClient, HAError
from .yamlcache import YamlFile

log = logging.getLogger("muse.catalog")
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


MAX_DESCRIPTION_LEN = 1000


@dataclass(frozen=True)
class AllowEntry:
    entity_id: str
    area: str | None = None
    tags: tuple[str, ...] = ()
    # YAML コメントは読み手（Muse）に届かないため、「この entity が具体的に何か」を
    # 自然言語で渡す説明はコメントではなく値として持つ。get_catalog がそのまま返す。
    description: str | None = None


def _parse(raw) -> dict[str, AllowEntry]:
    if raw is None:
        return {}
    items = raw.get("entities") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError("entity_catalog.yaml は `entities:` リストを持つ必要があります")
    out: dict[str, AllowEntry] = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("entity_id"), str):
            log.warning("entities[%d] を無視: entity_id が必要です", i)
            continue
        eid = it["entity_id"].strip()
        if not ENTITY_ID_RE.match(eid):
            log.warning("entities[%d] を無視: entity_id の形式が不正: %r", i, eid)
            continue
        area = it.get("area")
        tags = it.get("tags") or []
        if not isinstance(tags, list):
            tags = [tags]
        description = it.get("description")
        if description is not None and not isinstance(description, str):
            log.warning("entities[%d] を部分的に無視: description は文字列である必要があります", i)
            description = None
        if isinstance(description, str):
            description = description.strip()
            if len(description) > MAX_DESCRIPTION_LEN:
                log.warning("entities[%d] の description を %d 文字に切り詰めました", i, MAX_DESCRIPTION_LEN)
                description = description[:MAX_DESCRIPTION_LEN]
            description = description or None
        out[eid] = AllowEntry(eid, str(area) if area else None, tuple(str(t) for t in tags), description)
    return out


class CatalogService:
    def __init__(self, path: Path, ha: HAClient):
        self._file = YamlFile(path, _parse, {})
        self._ha = ha

    def allowlist(self) -> dict[str, AllowEntry]:
        return self._file.get()

    async def _lookup_areas(self, entity_ids: list[str]) -> dict[str, str | None]:
        """REST の template API で area 名を引く（ID は検証済みの安全な文字だけ）。"""
        if not entity_ids:
            return {}
        ids_literal = ",".join(f'"{e}"' for e in entity_ids)
        tpl = (
            "{" + "{% for e in [" + ids_literal + "] %}"
            '"{{ e }}": {{ area_name(e) | tojson }}{% if not loop.last %},{% endif %}'
            "{% endfor %}" + "}"
        )
        try:
            return json.loads(await self._ha.render_template(tpl))
        except (HAError, ValueError) as e:
            log.warning("area の取得に失敗（YAML の area のみ使用）: %s", e)
            return {}

    async def get_catalog(
        self,
        domains: list[str] | None = None,
        areas: list[str] | None = None,
        tags: list[str] | None = None,
        include_attributes: bool = True,
    ) -> dict:
        allow = self.allowlist()
        if not allow:
            return {"count": 0, "entities": []}
        states = {s["entity_id"]: s for s in await self._ha.get_states()}
        present = [e for e in allow if e in states]  # 許可リスト ∩ HA 実在
        reg_areas = await self._lookup_areas(present)

        dset = {d.lower() for d in domains} if domains else None
        aset = {a.lower() for a in areas} if areas else None
        tset = {t.lower() for t in tags} if tags else None

        entities = []
        for eid in present:
            entry, st = allow[eid], states[eid]
            attrs = st.get("attributes") or {}
            area = entry.area or reg_areas.get(eid)  # YAML の手動指定を優先
            if dset and eid.split(".", 1)[0] not in dset:
                continue
            if aset and (area or "").lower() not in aset:
                continue
            if tset and not ({t.lower() for t in entry.tags} & tset):
                continue
            state_value = st.get("state")
            if eid.split(".", 1)[0] == "input_text":
                # input_text の値は秘密情報になり得る（mode=password 等）ため返さない。
                # list_managed_helpers 側のマスクと同方針（2026-11-20 H2 再監査 P1:
                # カタログ経由の迂回を塞ぐ）
                state_value = "***" if state_value is not None else None
            item = {
                "entity_id": eid,
                "friendly_name": attrs.get("friendly_name"),
                "description": entry.description,
                "area": area,
                "state": state_value,
                "tags": list(entry.tags),
            }
            if include_attributes:
                item["device_class"] = attrs.get("device_class")
                item["unit_of_measurement"] = attrs.get("unit_of_measurement")
            entities.append(item)
        return {"count": len(entities), "entities": entities}
