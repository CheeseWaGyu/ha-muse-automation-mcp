"""MCP 側の Bearer トークン認証。

- トークンは env MCP_TOKEN > トークンファイル > 自動生成（32 バイト乱数, 0600 で保存）。
- ログには「生成した」という事実のみ出し、トークン実物は出さない。
- 比較は hmac.compare_digest（定数時間）。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from pathlib import Path

from .config import Settings

log = logging.getLogger("muse.auth")
MIN_TOKEN_LEN = 32


class TokenError(Exception):
    pass


def _write_new_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")


def load_or_create_secret(path: Path, label: str, env_value: str = "") -> str:
    if env_value:
        if len(env_value) < MIN_TOKEN_LEN:
            raise TokenError(f"{label} は {MIN_TOKEN_LEN} 文字以上にしてください")
        log.info("%s は環境変数から読み込みました", label)
        return env_value
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < MIN_TOKEN_LEN:
            raise TokenError(f"{path} の {label} が短すぎます。削除して再起動すると再生成されます")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.info("%s を既存ファイルから読み込みました", label)
        return token
    token = secrets.token_urlsafe(32)  # 32 バイト = 256bit
    _write_new_secret(path, token)
    log.info("%s を新規生成しました（保存先: %s）", label, path)
    return token


def load_or_create_token(settings: Settings) -> str:
    return load_or_create_secret(settings.token_file, "MCP トークン", settings.mcp_token_env)


def load_or_create_approver_token(settings: Settings) -> str:
    """単体形態の承認 UI 用パスワード。MCP トークンとは必ず別物にする。"""
    token = load_or_create_secret(settings.approver_token_file, "承認 UI トークン")
    if hmac.compare_digest(token.encode(), load_or_create_token(settings).encode()):
        raise TokenError("承認 UI トークンと MCP トークンが同一です。承認用ファイルを削除して再生成してください")
    return token


def verify_bearer(header_value: str | None, token: str) -> bool:
    if not header_value:
        return False
    scheme, _, supplied = header_value.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.strip().encode("utf-8"), token.encode("utf-8"))


class BearerAuthMiddleware:
    """純 ASGI ミドルウェア。lifespan はそのまま通し、HTTP は全パスで認証する。"""

    def __init__(self, app, token: str):
        if len(token) < MIN_TOKEN_LEN:
            raise TokenError("トークンが短すぎます")
        self.app = app
        self._token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            supplied = headers.get(b"authorization", b"").decode("latin-1") or None
            if verify_bearer(supplied, self._token):
                await self.app(scope, receive, send)
                return
            client = (scope.get("client") or ("?", 0))[0]
            log.warning("認証失敗: client=%s path=%s", client, scope.get("path"))
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="muse-automation-mcp"'),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        # websocket 等は拒否
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
