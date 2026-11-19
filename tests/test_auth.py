import os
import stat

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import BearerAuthMiddleware, TokenError, load_or_create_token, verify_bearer
from app.main import create_app
from tests.conftest import TOKEN, make_settings

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "t", "version": "0"}}}
HDRS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_verify_bearer():
    assert verify_bearer(f"Bearer {TOKEN}", TOKEN)
    assert verify_bearer(f"bearer {TOKEN}", TOKEN)
    assert not verify_bearer(None, TOKEN)
    assert not verify_bearer("", TOKEN)
    assert not verify_bearer(f"Bearer {TOKEN}x", TOKEN)
    assert not verify_bearer(f"Basic {TOKEN}", TOKEN)
    assert not verify_bearer(TOKEN, TOKEN)


def test_middleware_401_and_200():
    async def ok(request):
        return PlainTextResponse("ok")

    app = BearerAuthMiddleware(Starlette(routes=[Route("/x", ok)]), TOKEN)
    c = TestClient(app)
    assert c.get("/x").status_code == 401
    assert c.get("/x").headers["www-authenticate"].startswith("Bearer")
    assert c.get("/x", headers={"Authorization": "Bearer wrong" + "t" * 40}).status_code == 401
    assert c.get("/nonexistent").status_code == 401  # 全パス認証
    assert c.get("/x", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_real_mcp_endpoint_auth(tmp_path, ha):
    app = create_app(make_settings(tmp_path), ha)
    with TestClient(app) as c:
        assert c.post("/mcp/v1", json=INIT, headers=HDRS).status_code == 401
        bad = {**HDRS, "Authorization": "Bearer " + "z" * 40}
        assert c.post("/mcp/v1", json=INIT, headers=bad).status_code == 401
        good = {**HDRS, "Authorization": f"Bearer {TOKEN}"}
        r = c.post("/mcp/v1", json=INIT, headers=good)
        assert r.status_code == 200
        assert r.json()["result"]["serverInfo"]["name"] == "ha-automation"


def test_token_generated_0600_and_not_logged(tmp_path, caplog):
    s = make_settings(tmp_path)
    s = type(s)(**{**s.__dict__, "mcp_token_env": ""})
    caplog.set_level("DEBUG")
    token = load_or_create_token(s)
    assert len(token) >= 32
    mode = stat.S_IMODE(os.stat(s.token_file).st_mode)
    assert mode == 0o600
    assert token not in caplog.text  # 実物は出さない
    assert load_or_create_token(s) == token  # 再起動で保持


def test_short_token_rejected(tmp_path):
    s = make_settings(tmp_path)
    s = type(s)(**{**s.__dict__, "mcp_token_env": "short"})
    with pytest.raises(TokenError):
        load_or_create_token(s)
