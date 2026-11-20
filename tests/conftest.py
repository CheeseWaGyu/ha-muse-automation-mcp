import json
import re
from pathlib import Path

import httpx
import pytest

from app.auth import BearerAuthMiddleware  # noqa: F401
from app.config import Settings
from app.ha_client import HAClient
from app.main import build_services

TOKEN = "t" * 40

CATALOG_YAML = """
entities:
  - entity_id: light.living_ceiling
    description: リビングの天井照明。オン／オフできる。
    area: リビング
    tags: [lighting]
  - entity_id: sensor.living_temp
    description: "  リビングの室温センサー（前後の空白は除去される）  "
    tags: [climate]
  - entity_id: binary_sensor.living_presence
    tags: [presence]
  - entity_id: light.ghost          # 許可リストにあるが HA に存在しない
"""


class FakeHA:
    """最小限の HA REST モック。呼び出しを全部記録する。"""

    def __init__(self):
        self.states = {
            "light.living_ceiling": {"state": "on", "attributes": {"friendly_name": "Living Ceiling"}},
            "light.secret_bedroom": {"state": "off", "attributes": {"friendly_name": "Secret"}},
            "sensor.living_temp": {"state": "21.5", "attributes": {
                "friendly_name": "Temp", "device_class": "temperature", "unit_of_measurement": "°C"}},
            "binary_sensor.living_presence": {"state": "off", "attributes": {
                "friendly_name": "Presence", "device_class": "occupancy"}},
            "switch.outlet": {"state": "off", "attributes": {"friendly_name": "Outlet"}},
            # 非管理の既存オートメーション
            "automation.my_legacy": {"state": "on", "attributes": {"id": "1700000000000", "friendly_name": "legacy"}},
        }
        self.automations = {"1700000000000": {"id": "1700000000000", "alias": "legacy"}}
        self.areas = {"sensor.living_temp": "リビング"}
        self.check_result = {"result": "valid", "errors": None}
        self.create_entity = True      # False: POST しても entity が出ない
        self.mangle_on_store = False   # True: 保存内容を改変（読み戻し不一致）
        self.calls: list[tuple[str, str]] = []

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        assert path.startswith("/api/"), path
        path = path[len("/api/"):]
        self.calls.append((req.method, path))
        m = req.method
        if path == "states" and m == "GET":
            return httpx.Response(200, json=[{"entity_id": k, **v} for k, v in self.states.items()])
        if path == "template" and m == "POST":
            tpl = json.loads(req.content)["template"]
            ids = re.findall(r'"([a-z0-9_]+\.[a-z0-9_]+)"', tpl)
            return httpx.Response(200, text=json.dumps({i: self.areas.get(i) for i in ids}))
        if path == "config/core/check_config" and m == "POST":
            return httpx.Response(200, json=self.check_result)
        mm = re.fullmatch(r"config/automation/config/(.+)", path)
        if mm:
            aid = mm.group(1)
            if m == "GET":
                if aid in self.automations:
                    return httpx.Response(200, json=self.automations[aid])
                return httpx.Response(404, json={"message": "Resource not found"})
            if m == "POST":
                body = json.loads(req.content)
                stored = {"id": aid, **body}
                if self.mangle_on_store:
                    stored["alias"] = "MANGLED"
                self.automations[aid] = stored
                if self.create_entity:
                    self.states[f"automation.{aid}"] = {"state": "on", "attributes": {
                        "id": aid, "friendly_name": body.get("alias")}}
                return httpx.Response(200, json={"result": "ok"})
            if m == "DELETE":
                if aid not in self.automations:
                    return httpx.Response(404, json={"message": "not found"})
                del self.automations[aid]
                self.states.pop(f"automation.{aid}", None)
                return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(404, json={"message": f"unhandled {m} {path}"})

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "DELETE") and c[1].startswith("config/automation")]


def make_settings(tmp_path: Path, deploy_enabled=True) -> Settings:
    cfg, data = tmp_path / "config", tmp_path / "data"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "entity_catalog.yaml").write_text(CATALOG_YAML, encoding="utf-8")
    return Settings(
        mode="addon", ha_api_base="http://ha.test/api", ha_token="x" * 20,
        data_dir=data, config_dir=cfg,
        catalog_path=cfg / "entity_catalog.yaml", policy_path=cfg / "policy.yaml",
        token_file=cfg / "mcp_token", mcp_token_env=TOKEN,
        deploy_enabled=deploy_enabled, backup_generations=3,
        entity_wait_seconds=0, entity_wait_interval=0.01,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake():
    return FakeHA()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def ha(fake, settings):
    return HAClient(settings.ha_api_base, settings.ha_token, transport=fake.transport())


@pytest.fixture
def svc(settings, ha):
    return build_services(settings, ha)


def good_automation(**over):
    a = {
        "alias": "Presence notify",
        "description": "harmless test",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.living_presence", "to": "on"}],
        "actions": [{"action": "persistent_notification.create",
                     "data": {"title": "muse test", "message": "presence detected"}}],
    }
    a.update(over)
    return a
