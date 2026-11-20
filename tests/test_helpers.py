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
    v = await svc.helper_validator.validate(good_helper(), domain="template")
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


async def test_create_response_lost_not_deleted_unproven(hsvc, fake):
    """作成応答が失われた場合、実体があっても所有権を証明できないため自動削除しない（追加監査）。

    応答喪失時は作成物の item ID が分からない。同名実体が他者のものである可能性を
    排除できないため、結果は outcome_unknown として手動確認に回す。
    """
    svc, s, ha, coll = hsvc
    coll.create_error_after_apply = True
    p = await svc.proposals.propose_helper(good_helper())
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"] and rec["status"] == "failed"
    assert rec["result"]["errors"][0]["code"] == "create_outcome_unknown"
    assert rec["result"]["outcome_unknown"] is True
    assert coll.items["input_boolean"]  # 削除されず残る（所有権を証明できないため）
    assert svc.helper_deployer.ledger_entry(p["entity_id"]) is None


async def test_foreign_same_name_item_never_deleted_on_create_failure(hsvc, fake):
    """検証後に他者が同名ヘルパーを作り、こちらの作成が失敗しても、その実体は削除されない（追加監査の要求テスト）。"""
    from app.ha_client import HAError

    svc, s, ha, coll = hsvc
    p = await svc.proposals.propose_helper({"id": "muse_race"})
    orig_create = coll.create_item

    async def poisoned_create(domain, data):
        # 検証と作成の間に「別の利用者」が同名ヘルパーを作成し、その後こちらの作成が失敗する
        await orig_create(domain, {"name": "muse_race", "icon": "mdi:account"})
        raise HAError("create failed (fake)")

    coll.create_item = poisoned_create
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"]
    assert rec["result"]["errors"][0]["code"] == "create_outcome_unknown"
    survivors = [it for it in coll.items["input_boolean"] if it["name"] == "muse_race"]
    assert len(survivors) == 1 and survivors[0]["icon"] == "mdi:account"  # 他者の実体は無傷
    assert svc.helper_deployer.ledger_entry("input_boolean.muse_race") is None


async def test_foreign_swap_not_deleted_on_verify_failure(hsvc, fake):
    """作成成功後に実体が同名の別物（item ID 不一致）に替わっていたら、補償削除しない。"""
    svc, s, ha, coll = hsvc
    coll.create_entity = False  # state が出ないので検証失敗 → 補償削除フェーズに入る
    orig_create = coll.create_item

    async def swapping_create(domain, data):
        item = await orig_create(domain, data)
        coll.items[domain][0]["id"] = "foreign999"  # 実体は他者のものに替わったことにする
        return item

    coll.create_item = swapping_create
    p = await svc.proposals.propose_helper(good_helper())
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"]
    assert rec["result"]["errors"][0]["code"] == "verification_failed"
    assert rec["result"]["warnings"]  # 同名の別実体を残した旨の警告
    survivors = coll.items["input_boolean"]
    assert len(survivors) == 1 and survivors[0]["id"] == "foreign999"  # 削除されていない
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


async def test_create_response_without_id_is_unknown_not_foreign(hsvc, fake):
    """作成応答に item ID が無い場合、同名実体を foreign（他者のもの）と断定しない（監査 round 3）。

    検証失敗時の扱いは rolled_back=True ではなく outcome_unknown になり、
    実体（今回の作成物かもしれない）は自動削除されない。
    """
    svc, s, ha, coll = hsvc
    coll.create_entity = False  # state が出ないので検証失敗 → 補償判定フェーズに入る
    orig_create = coll.create_item

    async def idless_create(domain, data):
        await orig_create(domain, data)
        return {}  # 応答に id が含まれないケース

    coll.create_item = idless_create
    p = await svc.proposals.propose_helper(good_helper())
    r, rec = await _approve_result(svc, p["proposal_id"])
    assert not r["ok"]
    assert rec["result"]["errors"][0]["code"] == "verification_outcome_unknown"
    assert rec["result"]["outcome_unknown"] is True
    assert rec["result"].get("rolled_back") is not True
    assert len(coll.items["input_boolean"]) == 1  # 自動削除されていない
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


# ------------------------------------------------------------------ Phase H2
H2_VALID = [
    ("input_number", {"id": "muse_num"},
     {"id": "muse_num", "min": 0, "max": 100, "step": 1, "mode": "slider"}),
    ("input_number", {"id": "muse_num2", "min": 0.5, "max": 9.5, "step": 0.5,
                      "initial": 2.0, "unit_of_measurement": "℃", "mode": "box"},
     {"id": "muse_num2", "min": 0.5, "max": 9.5, "step": 0.5, "mode": "box",
      "initial": 2, "unit_of_measurement": "℃"}),
    ("input_text", {"id": "muse_txt"},
     {"id": "muse_txt", "min": 0, "max": 100, "mode": "text"}),
    ("input_text", {"id": "muse_txt2", "max": 10, "initial": "abc", "pattern": "^a+$"},
     {"id": "muse_txt2", "min": 0, "max": 10, "mode": "text",
      "pattern": "^a+$", "initial": "abc"}),
    ("input_select", {"id": "muse_sel", "options": ["a", "b"], "initial": "b"},
     {"id": "muse_sel", "options": ["a", "b"], "initial": "b"}),
    ("input_datetime", {"id": "muse_dt", "has_date": True, "initial": "2026-01-05"},
     {"id": "muse_dt", "has_date": True, "has_time": False, "initial": "2026-01-05"}),
    ("input_datetime", {"id": "muse_dt2", "has_time": True, "initial": "07:30:00"},
     {"id": "muse_dt2", "has_date": False, "has_time": True, "initial": "07:30:00"}),
    ("counter", {"id": "muse_cnt"},
     {"id": "muse_cnt", "initial": 0, "step": 1, "restore": True}),
    ("counter", {"id": "muse_cnt2", "initial": 5, "minimum": 0, "maximum": 10,
                 "restore": False},
     {"id": "muse_cnt2", "initial": 5, "step": 1, "restore": False,
      "minimum": 0, "maximum": 10}),
    ("timer", {"id": "muse_tmr", "duration": "0:05:00"},
     {"id": "muse_tmr", "duration": "0:05:00", "restore": False}),
    ("schedule", {"id": "muse_sch",
                  "mon": [{"from": "07:00:00", "to": "08:00:00"}],
                  "wed": [{"from": "22:00:00", "to": "23:30:00"},
                          {"from": "06:00:00", "to": "06:30:00"}]},
     {"id": "muse_sch",
      "mon": [{"from": "07:00:00", "to": "08:00:00"}],
      "wed": [{"from": "06:00:00", "to": "06:30:00"},
              {"from": "22:00:00", "to": "23:30:00"}]}),
]


async def test_h2_domains_validate_and_canonical_roundtrip(hsvc):
    from muse_automation_mcp.app.helpers import _from_item, helper_definition_hash
    svc, s, ha, coll = hsvc
    for domain, definition, expected_normalized in H2_VALID:
        v = await svc.helper_validator.validate(definition, domain)
        assert v.valid, (domain, definition, v.errors)
        assert v.normalized == expected_normalized, (domain, v.normalized)
        assert v.entity_id == f"{domain}.{definition['id']}"
        # HA の実体（作成ペイロードをそのまま保存した形）から読み戻しても定義が一致する
        item = {"name": definition["id"],
                **{k: val for k, val in v.normalized.items() if k != "id"}}
        back = _from_item(domain, item)
        assert back == v.normalized, (domain, back, v.normalized)
        assert helper_definition_hash(v.entity_id, back) == \
            helper_definition_hash(v.entity_id, v.normalized)


H2_INVALID = [
    ("input_number", {"id": "muse_x", "min": 10, "max": 5}, "invalid_range"),
    ("input_number", {"id": "muse_x", "step": 0}, "invalid_field"),
    ("input_number", {"id": "muse_x", "initial": 101}, "invalid_range"),
    ("input_number", {"id": "muse_x", "options": ["a"]}, "unknown_keys"),
    ("input_text", {"id": "muse_x", "max": 0}, "invalid_range"),
    ("input_text", {"id": "muse_x", "pattern": "["}, "invalid_field"),
    ("input_text", {"id": "muse_x", "max": 3, "initial": "abcd"}, "invalid_range"),
    ("input_select", {"id": "muse_x"}, "missing_field"),
    ("input_select", {"id": "muse_x", "options": []}, "invalid_field"),
    ("input_select", {"id": "muse_x", "options": ["a", "a"]}, "invalid_field"),
    ("input_select", {"id": "muse_x", "options": ["a"], "initial": "b"}, "invalid_range"),
    ("input_datetime", {"id": "muse_x"}, "invalid_field"),
    ("input_datetime", {"id": "muse_x", "has_date": True, "initial": "2026-1-5"},
     "invalid_field"),
    ("input_datetime", {"id": "muse_x", "has_time": True, "initial": "7:30"}, "invalid_field"),
    ("counter", {"id": "muse_x", "step": 0}, "invalid_field"),
    ("counter", {"id": "muse_x", "initial": 11, "maximum": 10}, "invalid_range"),
    ("counter", {"id": "muse_x", "initial": 1.5}, "invalid_field"),
    ("timer", {"id": "muse_x"}, "missing_field"),
    ("timer", {"id": "muse_x", "duration": "00:00:00"}, "invalid_range"),
    ("timer", {"id": "muse_x", "duration": "5:00"}, "invalid_field"),
    ("schedule", {"id": "muse_x"}, "missing_field"),
    ("schedule", {"id": "muse_x", "mon": [{"from": "08:00:00", "to": "07:00:00"}]},
     "invalid_range"),
    ("schedule", {"id": "muse_x", "mon": [{"from": "07:00:00", "to": "09:00:00"},
                                          {"from": "08:00:00", "to": "10:00:00"}]},
     "invalid_range"),
    ("schedule", {"id": "muse_x", "mon": [{"from": "7:00", "to": "08:00:00"}]},
     "invalid_field"),
]


async def test_h2_domains_reject_bad_fields(hsvc):
    svc, s, ha, coll = hsvc
    for domain, definition, code in H2_INVALID:
        v = await svc.helper_validator.validate(definition, domain)
        assert not v.valid and code in {e["code"] for e in v.errors}, \
            (domain, definition, v.errors)


async def test_h2_input_number_full_flow(hsvc, fake):
    svc, s, ha, coll = hsvc
    p = await svc.proposals.propose_helper(
        {"id": "muse_num", "min": 0, "max": 10, "initial": 3}, domain="input_number")
    assert p["status"] == "pending_approval" and p["kind"] == "helper_deploy"
    assert coll.items == {}
    await _approve(svc, p["proposal_id"])
    eid = "input_number.muse_num"
    assert eid in fake.states
    assert svc.helper_deployer.ledger_entry(eid) is not None
    item = coll.items["input_number"][0]
    assert item["min"] == 0 and item["max"] == 10 and item["initial"] == 3
    lst = await svc.helper_manager.list_managed()
    row = [x for x in lst["helpers"] if x["entity_id"] == eid]
    assert row and row[0]["definition_changed_since_creation"] is False
    d = await svc.proposals.propose_helper_delete(eid)
    assert d["status"] == "pending_approval"
    await _approve(svc, d["proposal_id"])
    assert eid not in fake.states
    assert svc.helper_deployer.ledger_entry(eid) is None


async def test_h2_schedule_full_flow(hsvc, fake):
    svc, s, ha, coll = hsvc
    p = await svc.proposals.propose_helper(
        {"id": "muse_sch", "fri": [{"from": "18:00:00", "to": "19:00:00"}]},
        domain="schedule")
    assert p["status"] == "pending_approval"
    await _approve(svc, p["proposal_id"])
    eid = "schedule.muse_sch"
    assert eid in fake.states
    lst = await svc.helper_manager.list_managed()
    row = [x for x in lst["helpers"] if x["entity_id"] == eid]
    assert row and row[0]["definition_changed_since_creation"] is False


# ------------------------------------------------------------------ H2 監査対応
async def test_h2_audit_schedule_malformed_returns_errors_not_exception(hsvc):
    """P2: 不正な schedule 定義でも例外にならず検証エラーを返す。"""
    svc, s, ha, coll = hsvc
    for bad in ({"id": "muse_x", "mon": [{"to": "09:00:00"}]},
                {"id": "muse_x", "mon": [{"from": "07:00:00"}]},
                {"id": "muse_x", "mon": "07:00"},
                {"id": "muse_x", "mon": [42]}):
        v = await svc.helper_validator.validate(bad, "schedule")
        assert not v.valid and v.normalized is None, (bad, v)
        p = await svc.helper_manager.preview(bad, "schedule")
        assert p["valid"] is False and p["errors"], bad


async def test_h2_audit_input_text_state_masked_in_list(hsvc, fake):
    """P1: list_managed_helpers は input_text の現在値を返さない（boolean は返す）。"""
    import json as _json
    svc, s, ha, coll = hsvc
    p = await svc.proposals.propose_helper(
        {"id": "muse_secret", "mode": "password"}, domain="input_text")
    await _approve(svc, p["proposal_id"])
    eid = "input_text.muse_secret"
    fake.states[eid] = {"state": "p@ssw0rd-value", "attributes": {}}
    bool_eid = await _create_approved(svc)
    lst = await svc.helper_manager.list_managed()
    assert "p@ssw0rd-value" not in _json.dumps(lst, ensure_ascii=False)
    row = [x for x in lst["helpers"] if x["entity_id"] == eid]
    assert row and row[0]["state"] == "***"
    brow = [x for x in lst["helpers"] if x["entity_id"] == bool_eid]
    assert brow and brow[0]["state"] == "off"  # input_boolean は従来どおり見える


async def test_h2_audit_pattern_length_capped(hsvc):
    svc, s, ha, coll = hsvc
    v = await svc.helper_validator.validate(
        {"id": "muse_x", "pattern": "a" * 201}, "input_text")
    assert not v.valid and "invalid_field" in {e["code"] for e in v.errors}


# ------------------------------------------------------------------ H2 再監査対応
async def test_h2_reaudit_pattern_structural_rejection(hsvc):
    """P2: 入れ子の量指定子と後方参照は長さ以下でも拒否。通常パターンは通る。"""
    svc, s, ha, coll = hsvc
    for bad in ("(a+)+$", "(.*)*", "(a|b+)+", "(a)\\1", "(x{2})*$"):
        v = await svc.helper_validator.validate({"id": "muse_x", "pattern": bad}, "input_text")
        assert not v.valid, (bad, v.errors)
    for ok in (r"^\d{3}$", "^[a-z]+$", "^(on|off)$", "^.{0,10}$"):
        v = await svc.helper_validator.validate({"id": "muse_x", "pattern": ok}, "input_text")
        assert v.valid, (ok, v.errors)


def test_h2_reaudit_canonical_never_raises_on_weird_items():
    """P2 低: 想定外の実体形状でも _from_item が例外にならない。"""
    from muse_automation_mcp.app.helpers import _from_item
    assert _from_item("schedule", {"name": "muse_x", "mon": 5}) == {"id": "muse_x"}
    assert _from_item("schedule", {"name": "muse_x", "mon": "abc"}) == {"id": "muse_x"}
    out = _from_item("schedule", {"name": "muse_x", "mon": [
        {"from": 1, "to": "09:00:00"},
        {"from": "07:00:00", "to": "08:00:00"}]})
    assert out == {"id": "muse_x", "mon": [{"from": "07:00:00", "to": "08:00:00"}]}
    assert _from_item("input_select", {"name": "muse_x", "options": 5}) == \
        {"id": "muse_x", "options": []}


# ------------------------------------------------------------------ H2 再監査（2回目）対応
async def test_h2_reaudit2_pattern_safe_subset(hsvc):
    """P2: グループへの量指定子は曖昧な選択肢・先読みも含め拒否。原子への量指定子は可。"""
    svc, s, ha, coll = hsvc
    for bad in ("^(a|aa)+$", "(ab)+$", "(?=a)a", "(a)(b)\\2", "^(a|a)*$"):
        v = await svc.helper_validator.validate({"id": "muse_x", "pattern": bad}, "input_text")
        assert not v.valid, (bad, v.errors)
    for ok in ("^(?:on|off)$", "^a{2,4}$", r"^\d{2}:\d{2}$", "^[A-Z][a-z]*$"):
        v = await svc.helper_validator.validate({"id": "muse_x", "pattern": ok}, "input_text")
        assert v.valid, (ok, v.errors)


async def test_h2_reaudit2_pattern_parser_unavailable_fails_closed(hsvc, monkeypatch):
    """P2: 構造検査のパーサーが使えない環境では pattern を受け付けない（fail-closed）。"""
    import importlib
    svc, s, ha, coll = hsvc
    helpers_mod = importlib.import_module(type(svc.helper_validator).__module__)
    monkeypatch.setattr(helpers_mod, "_sre_parser", lambda: None)
    v = await svc.helper_validator.validate({"id": "muse_x", "pattern": "^abc$"}, "input_text")
    assert not v.valid and "invalid_field" in {e["code"] for e in v.errors}


async def test_h2_reaudit2_ui_masks_referenced_input_text(hsvc, fake):
    """補足: 承認 UI の参照先 state 表示でも input_text の値は出ない。"""
    from tests.conftest import good_automation
    svc, s, ha, coll = hsvc
    fake.states["input_text.muse_note"] = {
        "state": "ui-secret-value", "attributes": {"friendly_name": "Note"}}
    s.catalog_path.write_text(
        "entities:\n  - entity_id: input_text.muse_note\n", encoding="utf-8")
    p = await svc.proposals.propose(good_automation(
        triggers=[{"trigger": "state", "entity_id": "input_text.muse_note", "to": "x"}]))
    assert p["status"] == "pending_approval", p
    async with ui_client(s, svc, ha) as c:
        page = (await c.get(f"/p/{p['proposal_id']}")).text
    assert "ui-secret-value" not in page
    assert "***" in page
