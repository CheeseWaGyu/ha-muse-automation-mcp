"""承認 UI（HA Ingress）。サーバー側レンダリングのみ・JavaScript なし。

アクセス制御:
  - アドオン: Supervisor（Ingress プロキシ）のアドレスからの接続だけ許可。HA がログイン済みユーザー
    （panel_admin: true で管理者のみ）を認証してから転送する。MCP の Bearer トークンでは入れない。
  - 単体: localhost 待受 + 承認用トークンによる HTTP Basic 認証（MCP トークンとは別）。
CSRF: 提案ごとの HMAC トークンをフォームに埋め込み、POST で検証する。
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import secrets
from urllib.parse import parse_qs

import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import Settings
from .ha_client import HAClient, HAError
from .proposals import PROPOSAL_ID_RE, ProposalService

log = logging.getLogger("muse.ui")

STATUS_LABEL = {"pending": "承認待ち", "approved": "承認・デプロイ済み", "rejected": "却下",
                "failed": "承認したがデプロイ失敗", "expired": "期限切れ"}

CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--card:#f4f4f6;--bd:#d8d8de;
--ok:#1a7f37;--ng:#c62828;--warn:#9a6700;--pri:#0b6bcb}
@media (prefers-color-scheme:dark){:root{--bg:#18181b;--fg:#ececf0;--mut:#9a9aa3;--card:#232328;--bd:#3a3a42;
--ok:#4cc26a;--ng:#ff7b72;--warn:#e3b341;--pri:#58a6ff}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:860px}
h1{font-size:1.25rem;margin:.2em 0 .8em}h2{font-size:1.05rem;margin:1.2em 0 .4em}
a{color:var(--pri)}.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin:10px 0}
.mut{color:var(--mut);font-size:.85rem}.badge{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--bd);font-size:.8rem}
.pending{color:var(--warn)}.approved{color:var(--ok)}.failed,.rejected,.expired{color:var(--ng)}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:10px;overflow-x:auto;font-size:.82rem;white-space:pre-wrap;word-break:break-word}
button{font:inherit;padding:10px 18px;border-radius:8px;border:1px solid var(--bd);cursor:pointer;margin:4px 8px 4px 0}
button.ok{background:var(--ok);color:#fff;border-color:var(--ok)}button.ng{background:transparent;color:var(--ng);border-color:var(--ng)}
input[type=text]{font:inherit;width:100%;box-sizing:border-box;padding:8px;border-radius:8px;border:1px solid var(--bd);background:var(--bg);color:var(--fg)}
ul{padding-left:1.2em}.err{color:var(--ng)}
"""


def _esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _page(title: str, body: str, base: str) -> HTMLResponse:
    doc = (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{_esc(title)}</title><style>{CSS}</style></head><body>'
           f'<p class="mut"><a href="{_esc(base)}/">← 一覧</a></p>{body}</body></html>')
    return HTMLResponse(doc, headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                   "form-action 'self'; frame-ancestors 'self'; base-uri 'none'",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer"})


class UIAuthMiddleware:
    def __init__(self, app, *, mode: str, allowed_clients: tuple[str, ...], approver_token: str = ""):
        self.app, self.mode = app, mode
        self.allowed, self.approver_token = allowed_clients, approver_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("", 0))[0]
        if self.mode == "addon":
            ok = client in self.allowed  # Ingress 経由（Supervisor）以外は拒否
            resp = Response("Forbidden", status_code=403)
        else:
            import base64
            hdr = dict(scope.get("headers", [])).get(b"authorization", b"").decode("latin-1")
            ok = False
            if hdr.lower().startswith("basic "):
                try:
                    _, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                    ok = hmac.compare_digest(pw.encode(), self.approver_token.encode())
                except Exception:
                    ok = False
            resp = Response("Unauthorized", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="muse-approval"'})
        if not ok:
            log.warning("承認 UI への不正アクセスを拒否: client=%s", client)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_ui_app(settings: Settings, proposals: ProposalService, ha: HAClient,
                  approver_token: str = "") -> Starlette:
    csrf_secret = secrets.token_bytes(32)  # プロセスごとに変わる（再起動後は画面の再読み込みが必要）

    def csrf(pid: str) -> str:
        return hmac.new(csrf_secret, pid.encode(), hashlib.sha256).hexdigest()

    def base_of(req: Request) -> str:
        raw = req.headers.get("x-ingress-path", "").rstrip("/")
        # 想定外の文字は無視（Ingress は /api/hassio_ingress/<token> を付ける）
        return raw if raw.replace("/", "").replace("_", "").replace("-", "").isalnum() else ""

    def actor_of(req: Request) -> str:
        h = req.headers
        return h.get("x-remote-user-display-name") or h.get("x-remote-user-name") \
            or h.get("x-remote-user-id") or "approver"

    async def form(req: Request) -> dict[str, str]:
        data = parse_qs((await req.body()).decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[0] for k, v in data.items()}

    async def index(req: Request) -> Response:
        base = base_of(req)
        recs = proposals.store.all()[:50]
        pend = [r for r in recs if r["status"] == "pending"]
        rest = [r for r in recs if r["status"] != "pending"]

        def row(r: dict) -> str:
            return (f'<div class="card"><a href="{_esc(base)}/p/{_esc(r["proposal_id"])}">'
                    f'<b>{_esc(r["alias"])}</b></a> '
                    f'<span class="badge {_esc(r["status"])}">{_esc(STATUS_LABEL.get(r["status"], r["status"]))}</span>'
                    f'<div class="mut">{_esc(r["automation_id"])} ・ 提案 {_esc(r["created_at"][:16])}'
                    f'{" ・ 期限 " + _esc(r["expires_at"][:16]) if r["status"] == "pending" else ""}</div></div>')

        body = "<h1>Muse オートメーション承認</h1>"
        body += f"<h2>承認待ち（{len(pend)}）</h2>" + ("".join(map(row, pend)) or '<p class="mut">ありません</p>')
        body += "<h2>履歴</h2>" + ("".join(map(row, rest)) or '<p class="mut">ありません</p>')
        return _page("Muse 承認", body, base)

    async def detail(req: Request) -> Response:
        base, pid = base_of(req), req.path_params["pid"]
        rec = proposals.store.get(pid) if PROPOSAL_ID_RE.match(pid) else None
        if rec is None:
            return _page("not found", "<h1>提案が見つかりません</h1>", base)
        names: dict[str, tuple] = {}
        try:
            for st in await ha.get_states():
                if st["entity_id"] in rec["referenced_entities"]:
                    names[st["entity_id"]] = (st.get("attributes", {}).get("friendly_name"), st.get("state"))
        except HAError:
            pass
        ents = "".join(
            f'<li><code>{_esc(e)}</code> {_esc(names.get(e, ("", ""))[0])} '
            f'<span class="mut">現在: {_esc(names.get(e, ("", "?"))[1])}</span></li>'
            for e in rec["referenced_entities"]) or "<li>なし</li>"
        d = rec["definition"]
        kind = rec.get("kind", "deploy")
        kind_label = {"deploy": "作成", "delete": "削除", "rollback": "ロールバック"}.get(kind, kind)
        yml = lambda v: _esc(yaml.safe_dump(v, allow_unicode=True, sort_keys=False).rstrip())
        warns = "".join(f'<li>{_esc(w["message"])}</li>' for w in rec["warnings"])
        body = (f'<h1>{_esc(rec["alias"])}</h1>'
                f'<p><span class="badge {_esc(rec["status"])}">{_esc(STATUS_LABEL.get(rec["status"]))}</span> '
                f'<span class="badge">{_esc(kind_label)}</span> '
                f'<span class="mut">{_esc(rec["automation_id"])} ・ 提案 {_esc(rec["created_at"][:16])} ・ '
                f'期限 {_esc(rec["expires_at"][:16])}</span></p>'
                f'<p>{_esc(d.get("description"))}</p>'
                f'<h2>関係するエンティティ</h2><ul>{ents}</ul>'
                + (f'<h2>警告</h2><ul>{warns}</ul>' if warns else ""))
        if kind == "deploy":
            body += (
                f'<h2>トリガー（いつ）</h2><pre>{yml(d["triggers"])}</pre>'
                + (f'<h2>条件</h2><pre>{yml(d["conditions"])}</pre>' if d.get("conditions") else "")
                + f'<h2>アクション（何をする）</h2><pre>{yml(d["actions"])}</pre>'
                + f'<h2>定義全体（承認対象の全フィールド）</h2><pre>{yml(d)}</pre>'
                + f'<p class="mut">モード: {_esc(d.get("mode"))} ・ 内容ハッシュ: <code>{_esc(rec["definition_hash"][:16])}…</code></p>')
            approve_text, approve_button = "この内容で HA にオートメーションを作成し、有効にします。", "承認してデプロイ"
        elif kind == "delete":
            body += (
                '<h2>削除対象の定義（提案時点）</h2>'
                '<p class="err">承認すると、このオートメーションを削除します。削除前にバックアップを取ります。'
                '提案後に定義が変わっていた場合は削除しません。</p>'
                + f'<pre>{yml(d)}</pre>'
                + f'<p class="mut">内容ハッシュ: <code>{_esc(rec["definition_hash"][:16])}…</code></p>')
            approve_text, approve_button = "このオートメーションを削除します。", "承認して削除"
        else:  # rollback
            payload = rec.get("payload") or {}
            target = payload.get("target")
            effect = ("直近の操作を取り消し、このオートメーションを削除します（作成前に戻します）。"
                        if target is None else
                        "直近の操作を取り消し、下記の定義で再作成します（既に存在する場合は実行しません）。")
            body += (
                '<h2>ロールバック内容</h2>'
                + f'<p class="err">{_esc(effect)} 対象バックアップ: {_esc(payload.get("backup_timestamp"))} '
                f'（{_esc(payload.get("backup_operation"))}）提案後に新しい操作があった場合は実行しません。</p>'
                + (f'<h2>復元する定義</h2><pre>{yml(target)}</pre>' if target is not None else "")
                + f'<p class="mut">内容ハッシュ: <code>{_esc(rec["definition_hash"][:16])}…</code></p>')
            approve_text, approve_button = "このロールバックを実行します。", "承認してロールバック"
        if rec["status"] == "pending":
            body += (
                f'<div class="card"><form method="post" action="{_esc(base)}/p/{_esc(pid)}/approve">'
                f'<input type="hidden" name="csrf" value="{csrf(pid)}">'
                f'<input type="hidden" name="hash" value="{_esc(rec["definition_hash"])}">'
                f'<p>{_esc(approve_text)}</p>'
                f'<button class="ok" type="submit">{_esc(approve_button)}</button></form></div>'
                f'<div class="card"><form method="post" action="{_esc(base)}/p/{_esc(pid)}/reject">'
                f'<input type="hidden" name="csrf" value="{csrf(pid)}">'
                f'<input type="text" name="reason" maxlength="500" placeholder="却下の理由（任意。Muse に伝わります）">'
                f'<button class="ng" type="submit">却下</button></form></div>')
        elif rec["status"] == "approved" and rec.get("result"):
            if kind == "delete":
                body += '<p class="approved">削除済み</p>'
            elif kind == "rollback":
                body += f'<p class="approved">ロールバック済み: {_esc(rec["result"].get("result"))}</p>'
            else:
                body += f'<p class="approved">作成済み: <code>{_esc(rec["result"].get("entity_id"))}</code></p>'
        elif rec["status"] == "failed" and rec.get("result"):
            body += "<h2>失敗の内容</h2><pre>" + yml(rec["result"].get("errors")) + "</pre>"
        elif rec["status"] == "rejected" and rec.get("reason"):
            body += f'<p>却下理由: {_esc(rec["reason"])}</p>'
        return _page(rec["alias"], body, base)

    async def decide(req: Request, action: str) -> Response:
        base, pid = base_of(req), req.path_params["pid"]
        f = await form(req)
        if not PROPOSAL_ID_RE.match(pid) or not hmac.compare_digest(f.get("csrf", "").encode(), csrf(pid).encode()):
            return Response("CSRF check failed. Reload the page.", status_code=403)
        if action == "approve":
            r = await proposals.approve(pid, f.get("hash", ""), actor_of(req))
        else:
            r = await proposals.reject(pid, f.get("reason", ""), actor_of(req))
        if r.get("error"):
            return _page("error", f'<h1 class="err">{_esc(r["error"])}</h1>', base)
        return RedirectResponse(f"{base}/p/{pid}", status_code=303)

    async def approve(req: Request) -> Response:
        return await decide(req, "approve")

    async def reject(req: Request) -> Response:
        return await decide(req, "reject")

    app = Starlette(routes=[
        Route("/", index),
        Route("/p/{pid}", detail),
        Route("/p/{pid}/approve", approve, methods=["POST"]),
        Route("/p/{pid}/reject", reject, methods=["POST"]),
    ])
    app.add_middleware(UIAuthMiddleware, mode=settings.mode,
                       allowed_clients=settings.ui_allowed_clients, approver_token=approver_token)
    return app
