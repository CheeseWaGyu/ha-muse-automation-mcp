"""Phase H1: input_boolean ヘルパー（検証・提案・承認フロー・台帳・UI）。

- 読み取り/検証系は常時公開、作成系は deploy_enabled AND helpers_enabled のときだけ
- 提案だけでは作成されず、承認で初めて作成され台帳に記録される
- 削除は台帳にあるものだけ。作成後の変更は台帳ハッシュ照合で拒否
- 承認時は helpers_enabled を再確認する
"""
import json
import re
from dataclasses import replace

import httpx
import pytest

from app.ha_client import HAClient
from app.helpers import websocket_url
from app.main import build_services
from app.tools import build_mcp
from app.ui import create_ui_app
from tests.conftest import FakeHelperCollection, good_helper, make_settings

pytestmark = pytest.mark.anyio

HELPER_DEPLOY_TOOLS = {"deploy_helper", "delete_managed_helper"}


def h_settings(tmp_path, name="h"):
    s = make_settings(tmp_path / name, deploy_enabled=True)
    return replace(s, helpers_enabled=True)


@pytest.fixture
def hsvc(tmp_path, fake):
    s = h_settings(tmp_path)
    ha = HAClient(s.ha_api_base, s.ha_token, transport=fake.transport())
    coll = FakeHelperCollection(fake)
    return build_services(s, ha, helper_collection=coll), s, ha, coll


async def _approve(svc, pid):
    rec = svc.proposals.store.get(pid)
    r = await svc.proposals.approve(pid, rec["definition_hash"], "a")
    assert r["ok"], r
    return r


def test_websocket_url_derivation():
    assert websocket_url("http://supervisor/core/api") == "ws://supervisor/core/websocket"
    assert websocket_url("https://ha.example:8123/api/") == "wss://ha.example:8123/websocket"


# ------------------------------------------------------------------ 検証
async def test_validate_helper_ok_and_preview(hsvc):
    svc, s, ha, coll = hsvc
    v = await svc.helper_validator.validate(good_helper())
    assert v.valid and v.helper_id.startswith("muse_helper_")
    assert v.entity_id == f"input_boolean.{v.helper_id}"
    p = await svc.helper_manager.preview({"id": "muse_my_flag", "icon": "mdi:flag"})
    assert p["valid"] and p["predicted_entity_id"] == "input_boolean.muse_my_flag"


async def test_validate_helper_rejects_bad_input(hsvc):
    svc, s, ha, coll = hsvc
    for bad, code in [
        ({"name": "x"}, "unknown_keys"),
        ({"id": "my_flag"}, "invalid_id"),
        ({"id": "muse_ok", "icon": "not-an-icon"}, "invalid_icon"),
        ({"id": "muse_ok", "icon": "mdi:Bad Icon"}, "invalid_icon"),
    ]:
        v = await svc.helper_validator.validate(bad)
        assert not v.valid and code in {e["code"] for e in v.errors}, (bad, v.errors)
    v = await svc.helper_validator.validate(good_helper(), domain="input_number")
    assert not v.valid and v.errors[0]["code"] == "helper_domain_not_supported"


async def test_validate_helper_duplicate_rejected(hsvc, fake):
    svc, s, ha, coll = hsvc
    await coll.create_item("input_boolean", {"name": "muse_dup"})
    v = await svc.helper_validator.validate({"id": "muse_dup"})
    assert not v.valid and "already_exists" in {e["code"] for e in v.errors}


# ------------------------------------------------------------------ ツール公開面
async def test_helper_deploy_tools_require_both_flags(tmp_path, fake):
    for deploy, helpers, expected in (
        (False, False, False), (True, False, False), (False, True, False), (True, True, True),
    ):
        s = replace(make_settings(tmp_path / f"{deploy}{helpers}", deploy_enabled=deploy),
                    helpers_enabled=helpers)
        ha = HAClient(s.ha_api_base, s.ha_token, transport=fake.transport())
        svc = build_services(s, ha, helper_collection=FakeHelperCollection(fake))
        names = {t.name for t in await build_mcp(s, svc).list_tools()}
        assert (HELPER_DEPLOY_TOOLS <= names) is expected, (deploy, helpers)
        assert {"validate_helper", "preview_helper", "list_managed_helpers"} <= names


# ------------------------------------------------------------------ 作成/削除フロー
async def _create_approved(svc):
    p = await svc.proposals.propose_helper(good_helper())
    assert p["status"] == "pending_approval" and p["kind"] == "helper_deploy"
    await _approve(svc, p["proposal_id"])
    return p["entity_id"]


async def test_propose_writes_nothing_then_approve_creates_and_registers(hsvc, fake):
    svc, s, ha, coll = hsvc
    p = await svc.proposals.propose_helper(good_helper())
    assert coll.items == {} and "entity_id" in p
    eid = await _create_approved_entity(svc, p)
    assert eid in fake.states
    assert svc.helper_deployer.ledger_entry(eid) is not None
    lst = await svc.helper_manager.list_managed()
    assert [x["entity_id"] for x in lst["helpers"]] == [eid]
    assert lst["helpers"][0]["definition_changed_since_creation"] is False
    assert svc.helper_deployer._backups.count(eid.split(".", 1)[1]) == 1


async def _create_approved_entity(svc, p):
    await _approve(svc, p["proposal_id"])
    return p["entity_id"]


async def test_helper_delete_flow(hsvc, fake):
    svc, s, ha, coll = hsvc
    eid = await _create_approved(svc)
    d = await svc.proposals.propose_helper_delete(eid)
    assert d["status"] == "pending_approval" and d["kind"] == "helper_delete"
    assert eid in fake.states  # 提案だけでは消えない
    await _approve(svc, d["proposal_id"])
    assert eid not in fake.states
    assert svc.helper_deployer.ledger_entry(eid) is None
    assert (await svc.helper_manager.list_managed())["count"] == 0


async def test_helper_delete_requires_ledger_and_hash(hsvc, fake):
    svc, s, ha, coll = hsvc
    # 台帳なし（直接作ったもの）は削除提案できない
    await coll.create_item("input_boolean", {"name": "muse_direct"})
    d = await svc.proposals.propose_helper_delete("input_boolean.muse_direct")
    assert d["status"] == "rejected" and d["errors"][0]["code"] == "not_managed"
    # 作成後に変更されたものも拒否
    eid = await _create_approved(svc)
    target = [it for it in coll.items["input_boolean"]
              if it["name"] == eid.split(".", 1)[1]][0]
    target["icon"] = "mdi:changed"
    d = await svc.proposals.propose_helper_delete(eid)
    assert d["status"] == "rejected"
    assert d["errors"][0]["code"] == "definition_changed_since_creation"
    assert eid in fake.states


async def test_approve_refused_when_helpers_disabled(hsvc, fake):
    svc, s, ha, coll = hsvc
    p = await svc.proposals.propose_helper(good_helper())
    rec = svc.proposals.store.get(p["proposal_id"])
    svc.proposals._s = replace(s, helpers_enabled=False)
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"] and "helpers_enabled" in r["error"]
    assert coll.items == {}
    assert svc.proposals.store.get(p["proposal_id"])["status"] == "pending"
    svc.proposals._s = s
    await _approve(svc, p["proposal_id"])
    assert p["entity_id"] in fake.states


async def test_create_verification_failure_rolls_back(hsvc, fake):
    svc, s, ha, coll = hsvc
    coll.create_entity = False  # 作成しても state が出ない
    p = await svc.proposals.propose_helper(good_helper())
    rec = svc.proposals.store.get(p["proposal_id"])
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"]
    assert coll.items.get("input_boolean") in (None, [])  # ロールバックで消えた
    assert svc.helper_deployer.ledger_entry(p["entity_id"]) is None


# ------------------------------------------------------------------ 監査対応: 曖昧ケース
async def _approve_result(svc, pid):
    rec = svc.proposals.store.get(pid)
    r = await svc.proposals.approve(pid, rec["definition_hash"], "a")
    return r, svc.proposals.store.get(pid)


async def test_create_response_lost_rolls_back(hsvc, fake):
    """作成は反映されたが応答が失われた → 再照会で実体を確認し補償削除する（P1）。"""
    svc, s, ha, coll = hsvc
    coll.create_error_after_apply = True
    p = await svc.proposals.propose_helper(good_helper())
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"] and rec["status"] == "failed"
    assert rec["result"]["errors"][0]["code"] == "create_failed_rolled_back"
    assert coll.items.get("input_boolean") in (None, [])
    assert p["entity_id"] not in fake.states
    assert svc.helper_deployer.ledger_entry(p["entity_id"]) is None


async def test_create_orphan_when_compensation_fails(hsvc, fake):
    """補償削除もできない → 台帳外で残ることを orphaned として明示する（P1）。"""
    svc, s, ha, coll = hsvc
    coll.create_error_after_apply = True
    coll.delete_error = True
    p = await svc.proposals.propose_helper(good_helper())
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"]
    assert rec["result"]["errors"][0]["code"] == "create_ambiguous_orphaned"
    assert rec["result"]["entity_id"] == p["entity_id"]
    assert coll.items["input_boolean"]  # 実体は残っている（=orphan が可視化されている）
    assert svc.helper_deployer.ledger_entry(p["entity_id"]) is None


async def test_create_outcome_unknown_when_recheck_fails(hsvc, fake):
    """再照会も失敗 → 成功/失敗を断定せず outcome_unknown（P1）。"""
    svc, s, ha, coll = hsvc
    orig_list = coll.list_items
    calls = {"n": 0}

    async def flaky(domain):
        calls["n"] += 1
        if calls["n"] > 2:  # 提案時検証(1) + 承認時検証(2) までは通し、再照会(3〜)を落とす
            from app.ha_client import HAError
            raise HAError("list down (fake)")
        return await orig_list(domain)

    coll.list_items = flaky
    coll.create_error_after_apply = True
    p = await svc.proposals.propose_helper(good_helper())
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"]
    assert rec["result"]["errors"][0]["code"] == "create_outcome_unknown"
    assert rec["result"]["outcome_unknown"] is True


async def test_verify_state_error_rolls_back(hsvc, fake):
    """作成後の状態確認で通信エラー → 例外で抜けず補償削除まで行う（P1）。"""
    from app.ha_client import HAError

    class BoomHA:
        async def get_states(self):
            raise HAError("states down (fake)")

    svc, s, ha, coll = hsvc
    svc.helper_deployer._ha = BoomHA()
    p = await svc.proposals.propose_helper(good_helper())
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"]
    assert rec["result"]["errors"][0]["code"] == "verification_failed"
    assert coll.items.get("input_boolean") in (None, [])
    assert svc.helper_deployer.ledger_entry(p["entity_id"]) is None


async def test_delete_response_lost_but_deleted(hsvc, fake):
    """削除は反映されたが応答が失われた → 実体の消失で成功と確定し台帳も解除（P2）。"""
    svc, s, ha, coll = hsvc
    eid = await _create_approved(svc)
    coll.delete_error_after_apply = True
    d = await svc.proposals.propose_helper_delete(eid)
    r, rec = await _approve_result(svc, d["proposal_id"])
    assert r["ok"] and rec["result"]["deleted"] is True
    assert rec["result"]["warnings"]  # 応答不明だった旨が残る
    assert eid not in fake.states
    assert svc.helper_deployer.ledger_entry(eid) is None


async def test_delete_outcome_unknown_when_verify_fails(hsvc, fake):
    """削除後の確認ができない → 失敗と即断せず outcome_unknown、台帳は残す（P2）。"""
    from app.ha_client import HAError

    svc, s, ha, coll = hsvc
    eid = await _create_approved(svc)
    d = await svc.proposals.propose_helper_delete(eid)
    orig_list, orig_delete = coll.list_items, coll.delete_item
    state = {"deleted": False}

    async def list_after_delete(domain):
        if state["deleted"]:
            raise HAError("verify down (fake)")
        return await orig_list(domain)

    async def delete_and_mark(domain, item_id):
        r = await orig_delete(domain, item_id)
        state["deleted"] = True
        return r

    coll.list_items, coll.delete_item = list_after_delete, delete_and_mark
    r, rec = await _approve_result(svc, d["proposal_id"])
    assert not r["ok"] and rec["status"] == "failed"
    assert rec["result"]["outcome_unknown"] is True
    assert rec["result"]["errors"][0]["code"] == "delete_outcome_unknown"
    assert svc.helper_deployer.ledger_entry(eid) is not None  # 台帳は触らない
    assert eid not in fake.states  # 実体は消えている（=不明として扱う理由）


async def test_list_managed_surfaces_collection_errors(hsvc, fake):
    """一覧取得の失敗を 0件と区別して errors で返す（P2）。"""
    svc, s, ha, coll = hsvc
    coll.list_error = True
    out = await svc.helper_manager.list_managed()
    assert out["count"] == 0 and out.get("errors")
    assert out["errors"][0]["domain"] == "input_boolean"


async def test_proposals_split_by_resource(hsvc, fake):
    svc, s, ha, coll = hsvc
    eid = await _create_approved(svc)
    mcp = build_mcp(s, svc)
    helpers = await mcp.call_tool("list_managed_helpers", {})
    assert "helper_deploy" in json.dumps(helpers, default=str)
    autos = await mcp.call_tool("list_managed_automations", {})
    assert "helper_deploy" not in json.dumps(autos, default=str)
    scripts = await mcp.call_tool("list_managed_scripts", {})
    assert "helper_deploy" not in json.dumps(scripts, default=str)


# ------------------------------------------------------------------ 承認 UI
def ui_client(settings, svc, ha, client=("172.30.32.2", 1234)):
    app = create_ui_app(settings, svc.proposals, ha, "")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://ui")


def csrf_and_hash(html):
    return (re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1),
            re.search(r'name="hash" value="([0-9a-f]+)"', html).group(1))


async def test_ui_helper_kinds_and_approve(hsvc, fake):
    svc, s, ha, coll = hsvc
    p = await svc.proposals.propose_helper(good_helper())
    async with ui_client(s, svc, ha) as c:
        index = (await c.get("/")).text
        assert "k-helper_deploy" in index and "ヘルパー作成" in index
        page = (await c.get(f"/p/{p['proposal_id']}")).text
        assert "作成されるヘルパー" in page and "input_boolean." in page
        assert "承認してヘルパーを作成" in page
        csrf, h = csrf_and_hash(page)
        r = await c.post(f"/p/{p['proposal_id']}/approve", data={"csrf": csrf, "hash": h})
        assert r.status_code == 303
        page = (await c.get(f"/p/{p['proposal_id']}")).text
    assert p["entity_id"] in fake.states
    assert "作成済み" in page
