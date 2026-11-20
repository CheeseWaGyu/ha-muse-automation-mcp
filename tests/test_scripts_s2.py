"""Phase S2: deploy_script / delete_managed_script（承認フロー経由）。

- ツールは deploy_enabled AND scripts_enabled のときだけ公開
- 提案だけでは HA に書かれず、承認で初めて作成され台帳に記録される
- 削除は台帳にあるものだけ。提案後の変更は changed_since_proposal で止まる
- 作成の検証失敗時はロールバックされ台帳にも残らない
- 承認 UI の一覧で種別（作成／削除／スクリプト作成…）がバッジで区別できる
"""
import json
import re
from dataclasses import replace

import httpx
import pytest

from app.ha_client import HAClient
from app.main import build_services
from app.tools import build_mcp
from app.ui import create_ui_app
from tests.conftest import good_automation, good_script, make_settings

pytestmark = pytest.mark.anyio

SCRIPT_DEPLOY_TOOLS = {"deploy_script", "delete_managed_script"}


def s2_settings(tmp_path, name="s2"):
    s = make_settings(tmp_path / name, deploy_enabled=True)
    return replace(s, scripts_enabled=True)


@pytest.fixture
def svc2(tmp_path, fake):
    s = s2_settings(tmp_path)
    ha = HAClient(s.ha_api_base, s.ha_token, transport=fake.transport())
    return build_services(s, ha), s, ha


async def _approve(svc, pid):
    rec = svc.proposals.store.get(pid)
    r = await svc.proposals.approve(pid, rec["definition_hash"], "a")
    assert r["ok"], r
    return r


# ------------------------------------------------------------------ ツール公開面
async def test_script_deploy_tools_require_both_flags(tmp_path, fake):
    for deploy, scripts, expected in (
        (False, False, False), (True, False, False), (False, True, False), (True, True, True),
    ):
        s = replace(make_settings(tmp_path / f"{deploy}{scripts}", deploy_enabled=deploy),
                    scripts_enabled=scripts)
        ha = HAClient(s.ha_api_base, s.ha_token, transport=fake.transport())
        mcp = build_mcp(s, build_services(s, ha))
        names = {t.name for t in await mcp.list_tools()}
        assert (SCRIPT_DEPLOY_TOOLS <= names) is expected, (deploy, scripts, names)
        # オートメーション側の公開条件は従来どおり deploy_enabled のみ
        assert ("deploy_automation" in names) is deploy


# ------------------------------------------------------------------ 作成フロー
async def test_propose_script_writes_nothing_then_approve_creates_and_registers(svc2, fake):
    svc, s, ha = svc2
    p = await svc.proposals.propose_script(good_script())
    assert p["status"] == "pending_approval" and p["kind"] == "script_deploy"
    assert p["script_id"].startswith("muse_")
    assert fake.script_writes() == [] and not [k for k in fake.scripts if k.startswith("muse_")]
    await _approve(svc, p["proposal_id"])
    sid = p["script_id"]
    assert sid in fake.scripts and f"script.{sid}" in fake.states
    # 台帳に記録され、管理一覧に出る
    assert svc.script_deployer.ledger_entry(sid) is not None
    lst = await svc.script_manager.list_managed()
    assert [x["script_id"] for x in lst["scripts"]] == [sid]
    assert lst["scripts"][0]["definition_changed_since_creation"] is False
    # バックアップも残る
    assert svc.script_deployer._backups.count(sid) == 1


async def test_reject_script_proposal_creates_nothing(svc2, fake):
    svc, s, ha = svc2
    p = await svc.proposals.propose_script(good_script())
    r = await svc.proposals.reject(p["proposal_id"], "今は不要", "a")
    assert r["ok"] and fake.script_writes() == []
    assert svc.proposals.summaries()[0]["kind"] == "script_deploy"


async def test_invalid_script_proposal_rejected(svc2, fake):
    svc, s, ha = svc2
    p = await svc.proposals.propose_script(good_script(sequence=[{"action": "homeassistant.restart"}]))
    assert p["status"] == "rejected" and svc.proposals.store.all() == []


async def test_deploy_verification_failure_rolls_back_and_no_ledger(svc2, fake):
    svc, s, ha = svc2
    fake.create_entity = False  # POST しても entity が出ない
    p = await svc.proposals.propose_script(good_script())
    rec = svc.proposals.store.get(p["proposal_id"])
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"]
    sid = p["script_id"]
    assert sid not in fake.scripts  # ロールバックで削除された
    assert svc.script_deployer.ledger_entry(sid) is None
    assert svc.proposals.store.get(p["proposal_id"])["status"] == "failed"


async def test_mcp_deploy_script_tool_only_proposes(svc2, fake, tmp_path):
    svc, s, ha = svc2
    mcp = build_mcp(s, svc)
    out = await mcp.call_tool("deploy_script", {"script": good_script()})
    assert "pending_approval" in json.dumps(out, default=str)
    assert fake.script_writes() == []
    listing = await mcp.call_tool("list_managed_scripts", {})
    assert "script_deploy" in json.dumps(listing, default=str)
    auto_listing = await mcp.call_tool("list_managed_automations", {})
    assert "script_deploy" not in json.dumps(auto_listing, default=str)


# ------------------------------------------------------------------ 削除フロー
async def _create_approved(svc):
    p = await svc.proposals.propose_script(good_script())
    await _approve(svc, p["proposal_id"])
    return p["script_id"]


async def test_delete_only_proposes_then_approve_deletes_and_unregisters(svc2, fake):
    svc, s, ha = svc2
    sid = await _create_approved(svc)
    d = await svc.proposals.propose_script_delete(sid)
    assert d["status"] == "pending_approval" and d["kind"] == "script_delete"
    assert sid in fake.scripts  # 提案だけでは消えない
    await _approve(svc, d["proposal_id"])
    assert sid not in fake.scripts and f"script.{sid}" not in fake.states
    assert svc.script_deployer.ledger_entry(sid) is None
    lst = await svc.script_manager.list_managed()
    assert lst["count"] == 0


async def test_delete_proposal_requires_ledger(svc2, fake, ha):
    svc, s, ha = svc2
    # 台帳なしで HAClient から直接作ったスクリプトは削除提案できない
    await ha.create_script_config("muse_direct", good_script())
    d = await svc.proposals.propose_script_delete("muse_direct")
    assert d["status"] == "rejected" and d["errors"][0]["code"] == "not_managed"
    assert "muse_direct" in fake.scripts
    d = await svc.proposals.propose_script_delete("my_legacy_script")
    assert d["status"] == "rejected"


async def test_delete_changed_since_proposal_fails(svc2, fake):
    svc, s, ha = svc2
    sid = await _create_approved(svc)
    d = await svc.proposals.propose_script_delete(sid)
    fake.scripts[sid]["alias"] = "changed after proposal"
    rec = svc.proposals.store.get(d["proposal_id"])
    r = await svc.proposals.approve(d["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"] and sid in fake.scripts
    assert svc.proposals.store.get(d["proposal_id"])["status"] == "failed"


# ------------------------------------------------------------------ 監査対応（2026-11-20, v0.3.1）
async def test_approve_refused_when_scripts_disabled(svc2, fake):
    """scripts_enabled を OFF にしたら、既存の pending 提案も承認・実行できない（提案は残る）。"""
    svc, s, ha = svc2
    p = await svc.proposals.propose_script(good_script())
    rec = svc.proposals.store.get(p["proposal_id"])
    svc.proposals._s = replace(s, scripts_enabled=False)
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"] and "scripts_enabled" in r["error"]
    assert fake.script_writes() == []
    assert svc.proposals.store.get(p["proposal_id"])["status"] == "pending"  # 消費されない
    svc.proposals._s = s  # フラグを戻せば承認できる
    await _approve(svc, p["proposal_id"])
    assert p["script_id"] in fake.scripts


async def test_delete_proposal_rejects_definition_changed_since_creation(svc2, fake):
    """作成後に手動変更されたスクリプトは、変更後の定義を見せて削除させるのではなく提案自体を拒否。"""
    svc, s, ha = svc2
    sid = await _create_approved(svc)
    fake.scripts[sid]["alias"] = "manually edited"
    d = await svc.proposals.propose_script_delete(sid)
    assert d["status"] == "rejected"
    assert d["errors"][0]["code"] == "definition_changed_since_creation"
    assert sid in fake.scripts


async def test_delete_checked_rejects_ledger_hash_mismatch(svc2, fake):
    """承認時照合: 提案スナップショットと一致していても、台帳ハッシュと違えば削除しない。"""
    svc, s, ha = svc2
    sid = await _create_approved(svc)
    fake.scripts[sid]["alias"] = "manually edited"
    current = await svc.script_deployer.current_definition(sid)
    r = await svc.script_deployer.delete_checked(sid, current)  # スナップショットとは一致する状態
    assert not r["deleted"] and r["errors"][0]["code"] == "definition_changed_since_creation"
    assert sid in fake.scripts


async def test_backup_stores_separated_by_resource(svc2, fake):
    """同じ ID のオートメーションとスクリプトでバックアップが混ざらない。"""
    svc, s, ha = svc2
    r = await svc.script_deployer.deploy(good_script(id="muse_same_id"))
    assert r["deployed"], r
    r = await svc.deployer.deploy(good_automation(id="muse_same_id"))
    assert r["deployed"], r
    script_rec = svc.script_deployer._backups.latest("muse_same_id")
    auto_rec = svc.deployer._backups.latest("muse_same_id")
    assert script_rec["operation"] == "script_deploy"
    assert auto_rec["operation"] == "deploy"
    assert svc.script_deployer._backups.count("muse_same_id") == 1
    assert svc.deployer._backups.count("muse_same_id") == 1


async def test_record_failure_rolls_back_creation(svc2, fake):
    """台帳登録が失敗したら、HA 上の定義をロールバックして提案を失敗にする。"""
    svc, s, ha = svc2

    class FailingLedger:
        def get(self, script_id):
            return None

        def register(self, script_id, definition):
            raise OSError("disk full")

        def unregister(self, script_id):
            pass

    svc.script_deployer._ledger = FailingLedger()
    p = await svc.proposals.propose_script(good_script())
    rec = svc.proposals.store.get(p["proposal_id"])
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"]
    result = svc.proposals.store.get(p["proposal_id"])["result"]
    assert result["errors"][0]["code"] == "record_failed"
    assert p["script_id"] not in fake.scripts  # ロールバック済み
    assert svc.proposals.store.get(p["proposal_id"])["status"] == "failed"
    # 先に保存された作成バックアップが孤立しないよう、失敗を明示する記録が最新になっている
    latest = svc.script_deployer._backups.latest(p["script_id"])
    assert latest["operation"] == "script_deploy_failed" and "ロールバック" in latest["note"]


async def test_notify_service_default_deny_for_scripts(svc2):
    """notify は高影響ドメイン（2026-11-20 監査）: カタログ掲載や同ドメイン対象でも既定拒否。"""
    svc, s, ha = svc2
    v = await svc.script_validator.validate(good_script(sequence=[
        {"action": "notify.mobile_app_x", "target": {"entity_id": "notify.mobile_app_x"},
         "data": {"message": "hi"}}]))
    assert not v.valid
    assert "service_sensitive" in {e["code"] for e in v.errors}


async def test_unregister_failure_keeps_delete_success_with_warning(svc2, fake):
    """削除後の台帳解除が失敗しても、削除自体は成功として返り、警告と監査が残る。"""
    svc, s, ha = svc2
    sid = await _create_approved(svc)
    ledger = svc.script_deployer._ledger
    orig = ledger.unregister

    def boom(script_id):
        raise OSError("ledger write failed")

    ledger.unregister = boom
    try:
        current = await svc.script_deployer.current_definition(sid)
        r = await svc.script_deployer.delete_checked(sid, current)
    finally:
        ledger.unregister = orig
    assert r["deleted"] and sid not in fake.scripts
    assert r["warnings"] and "手動で削除" in r["warnings"][0]


# ------------------------------------------------------------------ 承認 UI
def ui_client(settings, svc, ha, client=("172.30.32.2", 1234)):
    app = create_ui_app(settings, svc.proposals, ha, "")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://ui")


def csrf_and_hash(html):
    return (re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1),
            re.search(r'name="hash" value="([0-9a-f]+)"', html).group(1))


async def test_ui_index_distinguishes_kinds(svc2, fake):
    svc, s, ha = svc2
    sid = await _create_approved(svc)
    await svc.proposals.propose_script_delete(sid)
    await svc.proposals.propose(good_automation())
    await svc.proposals.propose_script(good_script(alias="Another script"))
    async with ui_client(s, svc, ha) as c:
        index = (await c.get("/")).text
    # 一覧の各行に種別バッジ（ラベル＋種別クラス）が出て、作成と削除が区別できる
    assert 'k-script_delete' in index and "スクリプト削除" in index
    assert 'k-script_deploy' in index and "スクリプト作成" in index
    assert 'k-deploy' in index and ">作成<" in index


async def test_ui_script_deploy_detail_and_approve(svc2, fake):
    svc, s, ha = svc2
    p = await svc.proposals.propose_script(good_script())
    async with ui_client(s, svc, ha) as c:
        page = (await c.get(f"/p/{p['proposal_id']}")).text
        assert "スクリプト作成" in page and "実行内容（sequence）" in page
        assert "light.turn_on" in page and "承認してスクリプトを作成" in page
        csrf, h = csrf_and_hash(page)
        r = await c.post(f"/p/{p['proposal_id']}/approve", data={"csrf": csrf, "hash": h})
        assert r.status_code == 303
        page = (await c.get(f"/p/{p['proposal_id']}")).text
    assert p["script_id"] in fake.scripts
    assert "作成済み" in page and f"script.{p['script_id']}" in page


async def test_ui_script_delete_detail_wording(svc2, fake):
    svc, s, ha = svc2
    sid = await _create_approved(svc)
    d = await svc.proposals.propose_script_delete(sid)
    async with ui_client(s, svc, ha) as c:
        page = (await c.get(f"/p/{d['proposal_id']}")).text
    assert "スクリプト削除" in page and "このスクリプトを削除します" in page
