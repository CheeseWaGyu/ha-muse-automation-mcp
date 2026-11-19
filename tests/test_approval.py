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
