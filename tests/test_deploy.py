import json

import pytest

from app.ha_client import AlreadyExists, HAClient, NotManaged
from app.main import build_services, create_app
from app.tools import build_mcp
from tests.conftest import good_automation, make_settings

pytestmark = pytest.mark.anyio


async def test_deploy_success_flow(svc, fake, settings):
    r = await svc.deployer.deploy(good_automation())
    assert r["deployed"], r
    aid = r["automation_id"]
    assert aid in fake.automations and r["entity_id"] == f"automation.{aid}"
    assert [s["step"] for s in r["steps"]] == ["validate", "check_id_absent", "check_config",
                                               "create", "readback", "entity_appears"]
    # 既存（非管理）は無傷、かつ一度も参照されていない
    assert fake.automations["1700000000000"] == {"id": "1700000000000", "alias": "legacy"}
    assert not any("1700000000000" in p for _, p in fake.calls)
    # 監査ログ + バックアップ
    lines = [json.loads(l) for l in settings.audit_path.read_text().splitlines()]
    assert lines[-1]["operation"] == "deploy" and lines[-1]["result"] == "success"
    assert svc.deployer._backups.count(aid) == 1


async def test_create_only_existing_id_always_fails(svc, fake):
    fake.automations["muse_taken"] = {"id": "muse_taken", "alias": "original"}
    r = await svc.deployer.deploy(good_automation(id="muse_taken", alias="overwrite attempt"))
    assert not r["deployed"]
    assert fake.automations["muse_taken"]["alias"] == "original"
    assert fake.writes() == []  # POST 自体が一度も飛んでいない


async def test_client_layer_refuses_overwrite_and_nonmanaged(ha, fake):
    fake.automations["muse_taken"] = {"id": "muse_taken", "alias": "original"}
    with pytest.raises(AlreadyExists):
        await ha.create_automation_config("muse_taken", {"alias": "x"})
    for bad in ("1700000000000", "muse", "automation.x", "../x"):
        with pytest.raises(NotManaged):
            await ha.get_automation_config(bad)
        with pytest.raises(NotManaged):
            await ha.create_automation_config(bad, {})
        with pytest.raises(NotManaged):
            await ha.delete_automation_config(bad)
    assert fake.writes() == []


async def test_check_config_invalid_aborts(svc, fake):
    fake.check_result = {"result": "invalid", "errors": "boom"}
    r = await svc.deployer.deploy(good_automation())
    assert not r["deployed"] and r["errors"][0]["code"] == "check_config_failed"
    assert fake.writes() == []


async def test_invalid_definition_never_reaches_ha_write(svc, fake):
    r = await svc.deployer.deploy(good_automation(actions=[{"action": "homeassistant.restart"}]))
    assert not r["deployed"] and fake.writes() == []


async def test_entity_missing_after_create_rolls_back(svc, fake):
    fake.create_entity = False
    r = await svc.deployer.deploy(good_automation())
    assert not r["deployed"] and r["rolled_back"] is True
    assert r["errors"][0]["code"] == "verification_failed"
    assert not [k for k in fake.automations if k.startswith("muse_")]


async def test_readback_mismatch_rolls_back(svc, fake):
    fake.mangle_on_store = True
    r = await svc.deployer.deploy(good_automation())
    assert not r["deployed"] and r["rolled_back"] is True
    assert not [k for k in fake.automations if k.startswith("muse_")]


async def test_delete_only_managed_with_backup(svc, fake):
    aid = (await svc.deployer.deploy(good_automation()))["automation_id"]
    bad = await svc.deployer.delete("1700000000000")
    assert not bad["deleted"] and "1700000000000" in fake.automations
    ok = await svc.deployer.delete(aid)
    assert ok["deleted"] and aid not in fake.automations
    rec = svc.deployer._backups.latest(aid)
    assert rec["operation"] == "delete" and rec["before"]["alias"] == "Presence notify"
    assert not any(m == "DELETE" and "1700000000000" in p for m, p in fake.calls)


async def test_rollback_deploy_then_undo_delete(svc, fake):
    aid = (await svc.deployer.deploy(good_automation()))["automation_id"]
    r = await svc.deployer.rollback(aid)                 # deploy を取り消す = 存在しない状態へ
    assert r["rolled_back"] and aid not in fake.automations
    r = await svc.deployer.rollback(aid)                 # さらに取り消す = 復元
    assert r["rolled_back"] and r["result"] == "restored" and aid in fake.automations
    d = await svc.deployer.delete(aid)
    assert d["deleted"]
    r = await svc.deployer.rollback(aid)                 # delete を取り消す
    assert r["rolled_back"] and aid in fake.automations


async def test_rollback_restore_refuses_overwrite(svc, fake):
    aid = (await svc.deployer.deploy(good_automation()))["automation_id"]
    await svc.deployer.delete(aid)
    fake.automations[aid] = {"id": aid, "alias": "recreated elsewhere"}
    r = await svc.deployer.rollback(aid)
    assert not r["rolled_back"] and fake.automations[aid]["alias"] == "recreated elsewhere"


async def test_rollback_nonmanaged_or_no_backup(svc):
    assert not (await svc.deployer.rollback("1700000000000"))["rolled_back"]
    assert not (await svc.deployer.rollback("muse_nothing"))["rolled_back"]


async def test_backup_generations_pruned(svc):
    aid = (await svc.deployer.deploy(good_automation(id="muse_gen")))["automation_id"]
    for _ in range(4):
        await svc.deployer.rollback(aid)
    assert svc.deployer._backups.count(aid) == 3  # settings.backup_generations


async def test_list_managed_only_muse(svc, fake):
    aid = (await svc.deployer.deploy(good_automation()))["automation_id"]
    r = await svc.deployer.list_managed()
    assert [a["automation_id"] for a in r["automations"]] == [aid]


async def test_preview_does_not_write(svc, fake):
    r = await svc.deployer.preview(good_automation())
    assert r["valid"] and r["automation_id"].startswith("muse_")
    assert r["normalized_definition"]["id"] == r["automation_id"]
    assert r["predicted_entity_id"] == "automation.presence_notify"
    assert fake.writes() == []


# ------------------------------------------------------------------ ツール公開面
READ_TOOLS = {"get_entity_catalog", "validate_automation", "preview_deployment", "list_managed_automations",
              # Phase S1: スクリプト読み取り・検証系はフラグ OFF でも常時公開
              "validate_script", "preview_script", "list_managed_scripts"}
DEPLOY_TOOLS = {"deploy_automation", "delete_managed_automation", "rollback_deployment"}


async def test_tools_exposed_by_flag(tmp_path, fake):
    for enabled, expected in ((False, READ_TOOLS), (True, READ_TOOLS | DEPLOY_TOOLS)):
        s = make_settings(tmp_path / str(enabled), deploy_enabled=enabled)
        ha = HAClient(s.ha_api_base, s.ha_token, transport=fake.transport())
        mcp = build_mcp(s, build_services(s, ha))
        assert {t.name for t in await mcp.list_tools()} == expected


async def test_deploy_disabled_is_default(monkeypatch, tmp_path):
    from app.config import load_settings
    s = load_settings({"HA_URL": "http://h:8123", "HA_TOKEN": "x" * 20}, tmp_path / "none.json")
    assert s.deploy_enabled is False
    s = load_settings({"HA_URL": "http://h:8123", "HA_TOKEN": "x" * 20, "DEPLOY_ENABLED": "true"},
                      tmp_path / "none.json")
    assert s.deploy_enabled is True
    assert "x" * 20 not in repr(s)
