"""HA REST API クライアント（用途限定）。

汎用プロキシは公開しない。公開メソッドは下記のみ:
  get_states / render_template / check_config /
  get_automation_config / create_automation_config / delete_automation_config /
  get_script_config / create_script_config / delete_script_config

オートメーション／スクリプト config への経路は muse_ ID の検証を必ず通る（非管理 ID には到達不可）。
create_*_config は内部で存在確認を行い、既存 ID への上書きを拒否する。
スクリプトの REST 経路はオートメーションと同じ形: /api/config/script/config/{id}
（設計メモ helper-script-mcp-design.md の A）。id がそのまま script.<id> エンティティになる。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from .backup import MANAGED_ID_RE

log = logging.getLogger("muse.ha")


class HAError(Exception):
    pass


class HAUnreachable(HAError):
    pass


class HAHttpError(HAError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HA returned HTTP {status}: {message}")
        self.status = status


class AlreadyExists(HAError):
    pass


class NotManaged(HAError):
    pass


def assert_managed(automation_id: str) -> None:
    if not isinstance(automation_id, str) or not MANAGED_ID_RE.match(automation_id):
        raise NotManaged(f"muse_ で始まる管理対象 ID ではありません: {automation_id!r}")


class HAClient:
    def __init__(
        self,
        api_base: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ):
        self._client = httpx.AsyncClient(
            base_url=api_base.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )
        # create 系の「不在確認 → POST」を直列化する（2026-11-20 監査対応）。
        # これが無いと、並行する 2 つの作成が両方とも不在を確認して POST する競合があり得る。
        # S2 のスクリプト deployer 側でも承認→作成→読み戻しを単一ロックで直列化する前提。
        self._create_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- 内部 ------------------------------------------------------------
    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path.lstrip("/"), **kw)
        except httpx.HTTPError as e:
            raise HAUnreachable(f"HA に接続できません: {type(e).__name__}") from e

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise HAHttpError(resp.status_code, resp.text[:300])

    # ---- 読み取り --------------------------------------------------------
    async def get_states(self) -> list[dict]:
        resp = await self._request("GET", "states")
        self._raise_for(resp)
        return resp.json()

    async def render_template(self, template: str) -> str:
        resp = await self._request("POST", "template", json={"template": template})
        self._raise_for(resp)
        return resp.text

    async def check_config(self) -> dict:
        resp = await self._request("POST", "config/core/check_config")
        self._raise_for(resp)
        return resp.json()

    # ---- オートメーション config（muse_ のみ） -----------------------------
    async def get_automation_config(self, automation_id: str) -> dict | None:
        assert_managed(automation_id)
        resp = await self._request("GET", f"config/automation/config/{automation_id}")
        if resp.status_code == 404:
            return None
        self._raise_for(resp)
        return resp.json()

    async def create_automation_config(self, automation_id: str, config: dict) -> None:
        """作成専用。既存 ID があれば AlreadyExists（HA の POST は上書き挙動のため二重防御）。"""
        assert_managed(automation_id)
        async with self._create_lock:
            if await self.get_automation_config(automation_id) is not None:
                raise AlreadyExists(f"ID は既に存在します: {automation_id}")
            resp = await self._request(
                "POST", f"config/automation/config/{automation_id}", json=config
            )
            self._raise_for(resp)

    async def delete_automation_config(self, automation_id: str) -> None:
        assert_managed(automation_id)
        resp = await self._request("DELETE", f"config/automation/config/{automation_id}")
        self._raise_for(resp)

    # ---- スクリプト config（muse_ のみ） ---------------------------------
    async def get_script_config(self, script_id: str) -> dict | None:
        assert_managed(script_id)
        resp = await self._request("GET", f"config/script/config/{script_id}")
        if resp.status_code == 404:
            return None
        self._raise_for(resp)
        return resp.json()

    async def create_script_config(self, script_id: str, config: dict) -> None:
        """作成専用。既存 ID があれば AlreadyExists（HA の POST は上書き挙動のため二重防御）。"""
        assert_managed(script_id)
        async with self._create_lock:
            if await self.get_script_config(script_id) is not None:
                raise AlreadyExists(f"ID は既に存在します: {script_id}")
            resp = await self._request(
                "POST", f"config/script/config/{script_id}", json=config
            )
            self._raise_for(resp)

    async def delete_script_config(self, script_id: str) -> None:
        assert_managed(script_id)
        resp = await self._request("DELETE", f"config/script/config/{script_id}")
        self._raise_for(resp)


def find_automation_state(states: list[dict], automation_id: str) -> dict | None:
    """属性 id が一致する automation.* の state を探す（entity_id は alias 由来のため）。"""
    for st in states:
        eid = st.get("entity_id", "")
        if eid.startswith("automation.") and (st.get("attributes") or {}).get("id") == automation_id:
            return st
    return None


def find_script_state(states: list[dict], script_id: str) -> dict | None:
    """script.<script_id> の state を探す。

    スクリプトはオートメーションと違い、config の id（オブジェクト ID）が
    そのまま entity_id の後半になる。念のため属性 id が一致するものも拾う。
    """
    for st in states:
        eid = st.get("entity_id", "")
        if not eid.startswith("script."):
            continue
        if eid == f"script.{script_id}" or (st.get("attributes") or {}).get("id") == script_id:
            return st
    return None
