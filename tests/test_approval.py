import json
import re
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.tools import build_mcp
from app.ui import create_ui_app
from tests.conftest import TOKEN, good_automation, make_settings

pytestmark = pytest.mark.anyio


def ui_client(settings, svc, ha, client=("172.30.32.2", 1234), approver=""):
    app = create_ui_app(settings, svc.proposals, ha, approver)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://ui")


def csrf_and_hash(html):
    return (re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1),
            re.search(r'name="hash" value="([0-9a-f]+)"', html).group(1))


# ---------------------------------------------------------------- MCP 側は提案だけ
async def test_propose_writes_nothing_to_ha(svc, fake):
    r = await svc.proposals.propose(good_automation())
    assert r["status"] == "pending_approval" and r["proposal_id"].startswith("p_")
    assert fake.writes() == []
    assert not [k for k in fake.automations if k.startswith("muse_")]


async def test_invalid_proposal_not_created(svc, fake):
    r = await svc.proposals.propose(good_automation(actions=[{"action": "homeassistant.restart"}]))
    assert r["status"] == "rejected" and svc.proposals.store.all() == []


async def test_mcp_tool_deploy_only_proposes_and_no_approve_tool(tmp_path, fake, svc, settings):
    mcp = build_mcp(settings, svc)
    names = {t.name for t in await mcp.list_tools()}
    assert len(names) == 7 and not any("approve" in n or "reject" in n for n in names)
    await mcp.call_tool("deploy_automation", {"automation": good_automation()})
    assert fake.writes() == []
    assert len(svc.proposals.store.all()) == 1
    listing = await mcp.call_tool("list_managed_automations", {})
    text = json.dumps(listing, default=str)
    assert "pending" in text and "proposals" in text


async def test_pending_limit(svc, settings):
    svc.proposals._s = replace(settings, max_pending_proposals=1)
    assert (await svc.proposals.propose(good_automation()))["status"] == "pending_approval"
    r = await svc.proposals.propose(good_automation(alias="second"))
    assert r["errors"][0]["code"] == "too_many_pending"


# ---------------------------------------------------------------- 承認 UI
async def test_ui_rejects_non_supervisor_clients(settings, svc, ha):
    async with ui_client(settings, svc, ha, client=("172.30.33.9", 1)) as c:
        assert (await c.get("/")).status_code == 403
    async with ui_client(settings, svc, ha) as c:
        assert (await c.get("/")).status_code == 200


async def test_mcp_token_does_not_open_ui_and_ui_is_not_on_mcp_app(settings, svc, ha):
    async with ui_client(settings, svc, ha, client=("203.0.113.5", 1)) as c:
        r = await c.get("/", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 403
    from starlette.testclient import TestClient
    with TestClient(create_app(settings, ha, svc)) as mc:
        h = {"Authorization": f"Bearer {TOKEN}"}
        assert mc.get("/p/p_000000000000", headers=h).status_code == 404  # MCP アプリに承認ルートは無い


async def test_standalone_basic_auth(settings, svc, ha):
    s = replace(settings, mode="standalone")
    async with ui_client(s, svc, ha, approver="a" * 40) as c:
        assert (await c.get("/")).status_code == 401
        assert (await c.get("/", auth=("x", "wrong" * 8))).status_code == 401
        assert (await c.get("/", auth=("x", "a" * 40))).status_code == 200


async def test_approve_flow_deploys_exact_reviewed_definition(settings, svc, ha, fake):
    p = await svc.proposals.propose(good_automation())
    pid, aid = p["proposal_id"], p["automation_id"]
    async with ui_client(settings, svc, ha) as c:
        assert "Presence notify" in (await c.get("/")).text
        page = (await c.get(f"/p/{pid}", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})).text
        assert "persistent_notification.create" in page and "binary_sensor.living_presence" in page
        assert '/api/hassio_ingress/abc/p/' in page          # Ingress 配下のリンク
        csrf, h = csrf_and_hash(page)
        assert fake.writes() == []                            # 表示だけでは書き込まない
        # CSRF 不正
        r = await c.post(f"/p/{pid}/approve", data={"csrf": "bad", "hash": h})
        assert r.status_code == 403 and fake.writes() == []
        # ハッシュ不一致（画面と保存内容が違う）
        r = await c.post(f"/p/{pid}/approve", data={"csrf": csrf, "hash": "0" * 64})
        assert "一致しません" in r.text and fake.writes() == []
        # 正規の承認
        r = await c.post(f"/p/{pid}/approve", data={"csrf": csrf, "hash": h},
                         headers={"X-Remote-User-Name": "taro"})
        assert r.status_code == 303
    assert aid in fake.automations and f"automation.{aid}" in fake.states
    rec = svc.proposals.store.get(pid)
    assert rec["status"] == "approved" and rec["decided_by"] == "taro"
    assert fake.automations["1700000000000"] == {"id": "1700000000000", "alias": "legacy"}
    # 二重承認は不可
    r = await svc.proposals.approve(pid, rec["definition_hash"], "x")
    assert not r["ok"]


async def test_reject_flow_and_visible_to_muse(settings, svc, ha, fake):
    p = await svc.proposals.propose(good_automation())
    pid = p["proposal_id"]
    async with ui_client(settings, svc, ha) as c:
        csrf, _ = csrf_and_hash((await c.get(f"/p/{pid}")).text)
        r = await c.post(f"/p/{pid}/reject", data={"csrf": csrf, "reason": "夜は通知いらない"})
        assert r.status_code == 303
    assert fake.writes() == []
    s = svc.proposals.summaries()[0]
    assert s["status"] == "rejected" and s["reason"] == "夜は通知いらない"
    assert not (await svc.proposals.approve(pid, "x", "a"))["ok"]


async def test_approval_revalidates_and_failure_is_recorded(settings, svc, ha, fake):
    p = await svc.proposals.propose(good_automation())
    del fake.states["binary_sensor.living_presence"]       # 承認までの間に entity が消えた
    rec = svc.proposals.store.get(p["proposal_id"])
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"] and fake.writes() == []
    assert svc.proposals.store.get(p["proposal_id"])["status"] == "failed"


async def test_expired_cannot_be_approved(settings, svc, ha, fake):
    p = await svc.proposals.propose(good_automation())
    path = svc.proposals.store._path(p["proposal_id"])
    rec = json.loads(path.read_text())
    rec["expires_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(rec))
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"] and "expired" in r["error"] and fake.writes() == []


async def test_html_is_escaped(settings, svc, ha):
    p = await svc.proposals.propose(good_automation(alias="<script>alert(1)</script>"))
    async with ui_client(settings, svc, ha) as c:
        for url in ("/", f"/p/{p['proposal_id']}"):
            r = await c.get(url)
            assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text
            assert "script-src" not in r.headers["content-security-policy"]
            assert r.headers["content-security-policy"].startswith("default-src 'none'")


async def test_invalid_proposal_id_path(settings, svc, ha):
    async with ui_client(settings, svc, ha) as c:
        r = await c.get("/p/..%2f..%2fetc")
        assert r.status_code == 404 or "見つかりません" in r.text
        assert "見つかりません" in (await c.get("/p/p_zzzzzzzzzzzz")).text


async def test_ui_detail_shows_all_definition_fields(settings, svc, ha):
    """承認画面に、要約表示の対象外フィールド（variables / max 等）も含め定義全体が出ること（監査指摘 2026-11-19）。"""
    a = good_automation(mode="queued", max=3, variables={"threshold": 42},
                        trigger_variables={"src": "test"})
    p = await svc.proposals.propose(a)
    async with ui_client(settings, svc, ha) as c:
        page = (await c.get(f"/p/{p['proposal_id']}")).text
    assert "定義全体" in page and "threshold" in page and "trigger_variables" in page and "max:" in page


async def _deploy_approved(svc):
    p = await svc.proposals.propose(good_automation())
    rec = svc.proposals.store.get(p["proposal_id"])
    r = await svc.proposals.approve(p["proposal_id"], rec["definition_hash"], "a")
    assert r["ok"]
    return p["automation_id"]


async def test_delete_only_proposes_then_approve_deletes(svc, fake):
    """削除は提案だけでは実行されず、承認後にのみ削除される（運用指摘 2026-11-19）。"""
    aid = await _deploy_approved(svc)
    assert aid in fake.automations
    d = await svc.proposals.propose_delete(aid)
    assert d["status"] == "pending_approval" and d["kind"] == "delete"
    assert aid in fake.automations  # 提案だけでは消えない
    rec = svc.proposals.store.get(d["proposal_id"])
    r = await svc.proposals.approve(d["proposal_id"], rec["definition_hash"], "a")
    assert r["ok"] and aid not in fake.automations
    assert svc.proposals.summaries()[0]["kind"] == "delete"


async def test_delete_reject_keeps_automation(svc, fake):
    aid = await _deploy_approved(svc)
    d = await svc.proposals.propose_delete(aid)
    r = await svc.proposals.reject(d["proposal_id"], "まだ使う", "a")
    assert r["ok"] and aid in fake.automations


async def test_delete_changed_since_proposal_fails(svc, fake):
    aid = await _deploy_approved(svc)
    d = await svc.proposals.propose_delete(aid)
    fake.automations[aid]["alias"] = "changed after proposal"
    rec = svc.proposals.store.get(d["proposal_id"])
    r = await svc.proposals.approve(d["proposal_id"], rec["definition_hash"], "a")
    assert not r["ok"] and aid in fake.automations
    assert svc.proposals.store.get(d["proposal_id"])["status"] == "failed"


async def test_delete_not_managed_or_missing_rejected(svc):
    r = await svc.proposals.propose_delete("1700000000000")
    assert r["status"] == "rejected"
    r2 = await svc.proposals.propose_delete("muse_missing_one")
    assert r2["status"] == "rejected" and r2["errors"][0]["code"] == "not_found"


async def test_rollback_only_proposes_then_cycles(svc, fake):
    """ロールバックも承認制。deploy取消（削除）→ delete取消（復元）まで承認経由で進む。"""
    aid = await _deploy_approved(svc)
    rb = await svc.proposals.propose_rollback(aid)
    assert rb["status"] == "pending_approval" and rb["kind"] == "rollback"
    assert aid in fake.automations  # 提案だけでは戻らない
    rec = svc.proposals.store.get(rb["proposal_id"])
    r = await svc.proposals.approve(rb["proposal_id"], rec["definition_hash"], "a")
    assert r["ok"] and aid not in fake.automations  # deploy の取消 = 削除
    # 直近は rollback(delete) 記録。その取消は「削除の取消」ではなく rollback 自体の取消になるため、
    # ここでは delete 経由の復元ではなく、バックアップが無いケースの拒否を確認する
    rb2 = await svc.proposals.propose_rollback(aid)
    assert rb2["status"] == "pending_approval"  # rollback 記録（before=定義あり）が直近
    rec2 = svc.proposals.store.get(rb2["proposal_id"])
    r2 = await svc.proposals.approve(rb2["proposal_id"], rec2["definition_hash"], "a")
    assert r2["ok"] and aid in fake.automations  # 定義が復元される


async def test_rollback_no_backup_rejected(svc):
    r = await svc.proposals.propose_rollback("muse_never_existed")
    assert r["status"] == "rejected" and r["errors"][0]["code"] == "no_backup"


async def test_mcp_delete_rollback_tools_only_propose(tmp_path, fake, svc, settings):
    aid = await _deploy_approved(svc)
    mcp = build_mcp(settings, svc)
    writes_before = len(fake.writes())
    out = await mcp.call_tool("delete_managed_automation", {"automation_id": aid})
    assert "pending_approval" in json.dumps(out, default=str)
    assert aid in fake.automations and len(fake.writes()) == writes_before


async def test_ui_delete_proposal_page_and_approve(settings, svc, ha, fake):
    aid = await _deploy_approved(svc)
    d = await svc.proposals.propose_delete(aid)
    pid = d["proposal_id"]
    async with ui_client(settings, svc, ha) as c:
        page = (await c.get(f"/p/{pid}")).text
        assert "削除" in page and "承認して削除" in page and aid in page
        csrf, h = csrf_and_hash(page)
        r = await c.post(f"/p/{pid}/approve", data={"csrf": csrf, "hash": h})
        assert r.status_code == 303
    assert aid not in fake.automations
    async with ui_client(settings, svc, ha) as c:
        page = (await c.get(f"/p/{pid}")).text
    assert "削除済み" in page
