"""Phase S1: スクリプト list / validate / preview（読み取り・検証のみ）。

作成系ツールはまだ存在しないこと、検証がカタログ／ポリシーで fail-closed であること、
HAClient のスクリプト経路が muse_ 限定＋create-only であることを確認する。
"""
import pytest

from app.ha_client import AlreadyExists, HAClient, NotManaged
from app.main import build_services
from app.tools import build_mcp
from tests.conftest import good_script, make_settings

pytestmark = pytest.mark.anyio


# ------------------------------------------------------------------ validate
async def test_validate_good_script(svc, fake):
    v = await svc.script_validator.validate(good_script())
    assert v.valid, v.errors
    assert v.script_id.startswith("muse_evening_notify_")
    assert v.referenced_entities == ["light.living_ceiling"]
    assert v.normalized["alias"] == "Evening notify"
    assert fake.script_writes() == [] and fake.writes() == []


async def test_validate_legacy_service_normalized(svc):
    v = await svc.script_validator.validate(good_script(
        sequence=[{"service": "light.turn_on", "target": {"entity_id": "light.living_ceiling"}}]))
    assert v.valid, v.errors
    assert any(w["code"] == "legacy_normalized" for w in v.warnings)
    assert v.normalized["sequence"][0]["action"] == "light.turn_on"


async def test_validate_rejects_unknown_key_and_empty_sequence(svc):
    v = await svc.script_validator.validate(good_script(triggers=[{"trigger": "state"}]))
    assert not v.valid and any(e["code"] == "schema" for e in v.errors)
    v = await svc.script_validator.validate(good_script(sequence=[]))
    assert not v.valid


async def test_validate_rejects_non_catalog_entity(svc):
    v = await svc.script_validator.validate(good_script(
        sequence=[{"action": "light.turn_on", "target": {"entity_id": "light.secret_bedroom"}}]))
    assert not v.valid
    assert any(e["code"] == "entity_not_allowed" for e in v.errors)


async def test_validate_rejects_dangerous_and_sensitive_services(svc):
    v = await svc.script_validator.validate(good_script(
        sequence=[{"action": "homeassistant.restart"}]))
    assert not v.valid and any(e["code"] == "service_dangerous" for e in v.errors)
    # script.* は既定ポリシーで危険ドメイン（他スクリプトの呼び出しは明示許可が必要）
    v = await svc.script_validator.validate(good_script(
        sequence=[{"action": "script.turn_on", "target": {"entity_id": "light.living_ceiling"}}]))
    assert not v.valid


async def test_validate_rejects_dynamic_entity_and_indirect_target(svc):
    v = await svc.script_validator.validate(good_script(
        sequence=[{"action": "light.turn_on", "target": {"entity_id": "{{ states('light.living_ceiling') }}"}}]))
    assert not v.valid and any(e["code"] == "dynamic_entity" for e in v.errors)
    v = await svc.script_validator.validate(good_script(
        sequence=[{"action": "light.turn_on", "target": {"area_id": "living"}}]))
    assert not v.valid and any(e["code"] == "indirect_target" for e in v.errors)


async def test_validate_existing_id_and_existing_entity_rejected(svc, fake):
    fake.scripts["muse_taken_script"] = {"alias": "taken", "sequence": [{"action": "persistent_notification.create", "data": {"title": "t", "message": "m"}}]}
    v = await svc.script_validator.validate(good_script(id="muse_taken_script"))
    assert not v.valid and any(e["code"] == "id_exists" for e in v.errors)
    # config が無くても script.<id> entity が既にあれば重複
    fake.states["script.muse_ghost_script"] = {"state": "off", "attributes": {"friendly_name": "ghost"}}
    v = await svc.script_validator.validate(good_script(id="muse_ghost_script"))
    assert not v.valid and any(e["code"] == "id_exists" for e in v.errors)
    v = await svc.script_validator.validate(good_script(id="not_managed"))
    assert not v.valid and any(e["code"] == "invalid_id" for e in v.errors)


async def test_validate_fields_and_mode(svc):
    v = await svc.script_validator.validate(good_script(
        mode="queued", max=3,
        fields={"message": {"name": "Message", "selector": {"text": None}}},
        variables={"x": 1}))
    assert v.valid, v.errors
    assert v.normalized["fields"]["message"]["name"] == "Message"
    v = await svc.script_validator.validate(good_script(fields={"bad": "not-a-dict"}))
    assert not v.valid


# ------------------------------------------------------------------ preview / list
async def test_preview_does_not_write(svc, fake):
    r = await svc.script_manager.preview(good_script())
    assert r["valid"] and r["script_id"].startswith("muse_")
    assert r["normalized_definition"]["id"] == r["script_id"]
    assert r["predicted_entity_id"] == f"script.{r['script_id']}"
    assert fake.script_writes() == [] and fake.writes() == []
    bad = await svc.script_manager.preview(good_script(sequence=[{"action": "homeassistant.restart"}]))
    assert not bad["valid"] and "script_id" not in bad


async def test_list_managed_only_muse_scripts(svc, fake, ha):
    # 管理対象を 1 件、HAClient 経由で作成（S2 の土台経路も兼ねて確認）
    await ha.create_script_config("muse_evening_notify_test", good_script())
    r = await svc.script_manager.list_managed()
    assert [s["script_id"] for s in r["scripts"]] == ["muse_evening_notify_test"]
    assert r["scripts"][0]["entity_id"] == "script.muse_evening_notify_test"
    assert r["scripts"][0]["alias"] == "Evening notify"
    assert r["count"] == 1  # script.my_legacy_script は含まれない


# ------------------------------------------------------------------ HAClient 経路
async def test_client_layer_script_create_only_and_managed(ha, fake):
    await ha.create_script_config("muse_one", good_script())
    assert "muse_one" in fake.scripts
    with pytest.raises(AlreadyExists):
        await ha.create_script_config("muse_one", good_script())
    for bad in ("my_legacy_script", "muse", "script.x", "../x"):
        with pytest.raises(NotManaged):
            await ha.get_script_config(bad)
        with pytest.raises(NotManaged):
            await ha.create_script_config(bad, {})
        with pytest.raises(NotManaged):
            await ha.delete_script_config(bad)
    # 非管理の既存スクリプトには一切触れていない
    assert fake.scripts["my_legacy_script"]["alias"] == "Legacy Script"
    assert not any("my_legacy_script" in p for _, p in fake.calls)


# ------------------------------------------------------------------ ツール公開面・フラグ
SCRIPT_READ_TOOLS = {"validate_script", "preview_script", "list_managed_scripts"}


async def test_script_read_tools_always_exposed(tmp_path, fake):
    for deploy in (False, True):
        s = make_settings(tmp_path / f"d{deploy}", deploy_enabled=deploy)
        ha = HAClient(s.ha_api_base, s.ha_token, transport=fake.transport())
        mcp = build_mcp(s, build_services(s, ha))
        names = {t.name for t in await mcp.list_tools()}
        assert SCRIPT_READ_TOOLS <= names
        # S1 では作成系スクリプトツールは存在しない
        assert not any(n in names for n in ("deploy_script", "delete_managed_script",
                                            "deploy_helper", "delete_managed_helper"))
        out = await mcp.call_tool("validate_script", {"script": good_script()})
        assert "true" in str(out).lower() or "valid" in str(out)


async def test_script_flags_default_off_and_parse(monkeypatch, tmp_path):
    from app.config import load_settings
    s = load_settings({"HA_URL": "http://h:8123", "HA_TOKEN": "x" * 20}, tmp_path / "none.json")
    assert s.scripts_enabled is False and s.helpers_enabled is False
    s = load_settings({"HA_URL": "http://h:8123", "HA_TOKEN": "x" * 20,
                       "SCRIPTS_ENABLED": "true", "HELPERS_ENABLED": "true"},
                      tmp_path / "none.json")
    assert s.scripts_enabled is True and s.helpers_enabled is True
