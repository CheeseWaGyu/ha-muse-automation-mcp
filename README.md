# ha-muse-automation-mcp

Home Assistant のオートメーションを **「追加専用」** でデプロイするための専用 MCP サーバーです。
AI エージェント（Muse）は HA の長期アクセストークン（LLAT）を持たず、この MCP サーバーの Bearer トークンで
`muse_` で始まるオートメーションを**提案**するだけです。**デプロイされるのは、あなたが HA の承認画面（Ingress）で承認したときだけ**です。

- プロトコル: MCP Streamable HTTP、パス `/mcp/v1`（既定ポート `8099`）
- 公開: Tailscale Service（HTTPS）。HA の 8123 は公開しません
- 実装: Python 3.12+ / 公式 `mcp` SDK（FastMCP, **1.x 系にピン留め**）/ httpx / pydantic / PyYAML

## ツール（最大 12 個: オートメーション 7 + スクリプト 5）

| ツール | 種別 | 内容 |
|---|---|---|
| `get_entity_catalog` | 読取 | 許可リストの entity だけをフィルタして返す |
| `validate_automation` | 読取 | 検証のみ（書き込みなし） |
| `preview_deployment` | 読取 | 検証 + 正規化済み定義・ID・予測 entity_id |
| `list_managed_automations` | 読取 | ID が `muse_` で始まるものの一覧 |
| `deploy_automation` | 提案 | 検証して「承認待ちの提案」を作るだけ（HA には書き込まない） |
| `delete_managed_automation` | 提案 | `muse_` のみの「削除の承認待ち提案」を作るだけ（承認時に削除前バックアップ） |
| `rollback_deployment` | 提案 | 直近操作の取り消しの「承認待ち提案」を作るだけ |
| `validate_script` | 読取 | スクリプト定義の検証のみ（書き込みなし）。sequence はオートメーションの actions と同じ検証（カタログ＋ポリシー） |
| `preview_script` | 読取 | 検証 + 正規化済み定義・ID・確定 entity_id（`script.<id>`） |
| `list_managed_scripts` | 読取 | 管理台帳に作成記録があり config で読み戻せるスクリプトの一覧（接頭辞だけのものは `unverified_count` に計上） |
| `deploy_script` | 提案 | スクリプト作成の「承認待ち提案」を作るだけ（`scripts_enabled` + `deploy_enabled` のときだけ公開。承認時に作成され台帳に記録） |
| `delete_managed_script` | 提案 | 台帳にあるスクリプトの「削除の承認待ち提案」を作るだけ（同条件で公開。承認時に削除前バックアップ） |

**承認・却下は MCP ツールにありません**（Muse からは実行できません）。
書込系 3 ツールは `DEPLOY_ENABLED=true` のときだけ公開されます（**既定は無効**。無効時はツール一覧にも出ません）。
汎用 HA API プロキシ・任意サービス呼び出し・ファイル読み書き・シェル実行のツールはありません。

### スクリプト／ヘルパー拡張（段階導入）

ヘルパー／スクリプト作成は本アドオンの拡張として段階導入します（新規アドオンは作りません）。
Phase S1（v0.2.0、監査対応 v0.2.1）ではスクリプトの読み取り・検証のみでした。
Phase S2（v0.3.0）で作成系 `deploy_script` / `delete_managed_script` を追加しました。アドオン設定
`scripts_enabled`（環境変数 `SCRIPTS_ENABLED`、既定 false）と `deploy_enabled` の両方が ON のときだけ
公開され、どちらも承認待ち提案を作るだけです。承認時の作成は「検証 → 不在確認 → check_config → 作成 →
読み戻し照合 → entity 出現確認」を単一ロックで直列化し、失敗時は削除してロールバックします。作成成功時に
管理台帳へ記録し、削除は台帳にあるものだけ（提案時点の定義を固定し、変わっていれば実行しません）。
承認 UI の一覧・詳細では種別バッジ（作成／削除／ロールバック／スクリプト作成／スクリプト削除）が
色分け表示されます。

S2 監査対応（v0.3.2）: `notify` ドメインを高影響ドメイン（lock / alarm_control_panel と同格）にしました。
同ドメインの対象を指定しても既定では拒否され、`extra_allowed_services` への完全なサービス名の明示
（例: `notify.mobile_app_my_phone`）でのみ許可されます（ワイルドカード `notify.*` は効きません）。
あわせて、台帳登録失敗時に先行保存された作成バックアップへ失敗を明示する記録（`script_deploy_failed`）を
残すこと、削除後の台帳解除が失敗しても削除自体は成功として返し警告と監査に復旧方法を残すことを追加しました。

S2 監査対応（v0.3.1）: 承認時にも `scripts_enabled` を再確認し、OFF の間はスクリプト提案を実行しません
（提案は消費されず、再有効化すれば承認できます）。スクリプト削除は、提案時・承認時の双方で現在定義を
台帳の作成時ハッシュと照合し、作成後に手動変更された定義は削除を拒否します（引き継ぎ＝台帳再登録は未実装）。
バックアップはリソース別（オートメーション: `backups/<id>/` 従来どおり、スクリプト: `backups/script/<id>/`）。
作成後のバックアップ／台帳保存が失敗した場合は HA 上の定義をロールバックし、できない場合は
復旧情報（script_id・手動削除が必要な旨）を監査ログとエラーに残します。
ヘルパー作成系は Phase H1 以降で `helpers_enabled`（既定 false）が条件になります。
作成したヘルパー／スクリプトは `entity_catalog.yaml` に自動追加されません（カタログ入りは所有者の判断）。

2026-11-20 の外部監査への対応（v0.2.1）:
- **管理対象の判定**: `script.muse_*` という接頭辞だけを根拠にしません。管理台帳（`managed_scripts.json`、
  S2 の作成成功時に記録）＋ config API での読み戻し ＋ states の存在がそろったものだけを管理対象とし、
  接頭辞だけ一致するものは `unverified_count` として件数だけ返します。定義が作成後に変わったものは
  `definition_changed_since_creation` フラグで知らせます（S2 の削除・復元はこの照合で書き込みを止めます）。
- **ID 上限の統一**: 検証器・管理判定とも全体最大 100 文字（`muse_` ＋ 95 文字）に統一し、境界値テストを追加。
- **作成の排他制御**: HAClient の create 系（不在確認→POST）をロックで直列化しました。S2 の deployer でも
  承認→作成→読み戻し→ロールバックを単一ロックで直列化します。
- **テンプレート方針**: テンプレートの entity 参照チェックは静的解析（ベストエフォート）であり安全境界では
  ありません。preview はその旨の注記を返し、S2 の承認画面でも警告（`template_partial_analysis`）を
  明示する前提で承認する運用とします。

## ディレクトリ構成

```
.
├── repository.yaml              # アドオンリポジトリ定義
├── muse_automation_mcp/         # ← HA アドオン本体（単体起動も同じコード）
│   ├── config.yaml / Dockerfile / run.sh
│   ├── requirements.txt         # 依存はピン留め
│   ├── entity_catalog.sample.yaml / policy.sample.yaml
│   └── app/                     # config, auth, ha_client, catalog, policy, validation, deployer, proposals, scripts, ui, backup, audit, tools, main
├── tests/                       # モック HA を使う単体テスト（122 件）
└── docs/                        # DESIGN.md / VERIFICATION.md / NOTES.md
```

## インストール

### A. HA アドオン（推奨）

1. このリポジトリを GitHub に push します（`repository.yaml` と `config.yaml` は CheeseWaGyu 用に設定済み）。
2. HA: 設定 → アドオン → アドオンストア → 右上「…」→ リポジトリ → このリポジトリの URL を追加。
3. 「Muse Automation MCP」をインストールして起動します。HA Core へは Supervisor 経由
   （`http://supervisor/core/api`、`SUPERVISOR_TOKEN`）でアクセスするため **LLAT は不要**です。
4. アドオン専用ディレクトリ（ホスト側 `/addon_configs/<リポジトリハッシュ>_muse_automation_mcp/`、
   コンテナ内 `/config`）に `entity_catalog.yaml` を作成します（下記「カタログ許可リスト」）。

### B. 単体常駐（フォールバック）

HA 外のマシン（Raspberry Pi 等）で、同じコードを環境変数で切り替えて動かします。

```bash
cd muse_automation_mcp
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# HA 専用ユーザーの LLAT を 0600 のファイルに置く（環境変数 HA_TOKEN でも可）
install -m 600 /dev/null ./ha_token && vi ./ha_token

export HA_URL=http://homeassistant:8123
export HA_TOKEN_FILE=$PWD/ha_token
export CONFIG_DIR=$PWD/config DATA_DIR=$PWD/data
mkdir -p config && cp entity_catalog.sample.yaml config/entity_catalog.yaml   # 実 ID に編集
python -m app.main
```

> **注意（通信の暗号化）**: 単体形態では HA トークンを `HA_URL` へ Bearer で送ります。上記の
> `http://homeassistant:8123` のような平文 HTTP は、HA と同一ホスト上、または Tailscale 等の
> 暗号化された閉域ネットワーク内でのみ使ってください。信頼できないネットワークを経由する場合は
> `https://` のエンドポイントを使ってください（起動時にも警告ログが出ます）。

systemd 例（`/etc/systemd/system/muse-automation-mcp.service`）:

```ini
[Service]
WorkingDirectory=/opt/ha-muse-automation-mcp/muse_automation_mcp
Environment=HA_URL=http://homeassistant:8123 HA_TOKEN_FILE=/etc/muse/ha_token
Environment=CONFIG_DIR=/var/lib/muse/config DATA_DIR=/var/lib/muse/data
ExecStart=/opt/ha-muse-automation-mcp/.venv/bin/python -m app.main
Restart=on-failure
User=muse
```

主な環境変数: `HA_URL` `HA_TOKEN` `HA_TOKEN_FILE` `MCP_TOKEN` `MCP_TOKEN_FILE` `PORT`(8099) `MCP_PATH`(/mcp/v1)
`UI_HOST` `UI_PORT`(8100) `APPROVER_TOKEN_FILE` `PROPOSAL_TTL_HOURS`(24) `MAX_PENDING_PROPOSALS`(20)
`CONFIG_DIR` `DATA_DIR` `CATALOG_PATH` `POLICY_PATH` `DEPLOY_ENABLED` `BACKUP_GENERATIONS`(20) `LOG_LEVEL`。
アドオンでは `deploy_enabled` / `backup_generations` / `log_level` を設定画面から変更できます（環境変数が優先）。

## 承認フロー（Ingress）

```
Muse: deploy_automation ─▶ 検証 OK なら「承認待ち提案」を保存（HA には何も書かない）
あなた: HA サイドバー「Muse 承認」─▶ 内容（トリガー / 条件 / アクション / 関係 entity）を確認
        ├ 承認 ─▶ 再検証 → 不在確認 → check_config → 作成 → 読み戻し確認 → 完了
        └ 却下（理由は任意）─▶ 何も作られない
Muse: list_managed_automations の `proposals` で承認待ち / 承認済み / 却下（理由つき）/ 失敗を確認
```

- **承認 UI は MCP とは別ポート（8100）・別アプリ**です。アドオンでは `ports` に出さず Ingress 経由のみで、
  Supervisor（172.30.32.2）以外からの接続は 403。HA にログインした管理者（`panel_admin: true`）だけがサイドバーに表示されます。
  MCP の Bearer トークンでは入れません。
- **削除・ロールバックも同じ承認制です**。`delete_managed_automation` / `rollback_deployment` も提案を作るだけで、
  承認されるまで実行されません。削除提案は提案時点の定義を、ロールバック提案は対象バックアップを固定し、
  承認時にどちらかが変わっていれば実行しません。ロールバックでは、現在の定義が対象バックアップの `after`
  （その操作直後の定義）と一致することも、照合と実行を同じ排他制御の中で確認します。提案後に HA 上で
  手動編集された定義が、承認で削除されることはありません。
- 提案は不変です。承認ボタンは「あなたが見た内容のハッシュ」を送り、保存済みの提案と一致する場合だけ進みます。
  承認時にも検証を**やり直す**ため、承認までの間に entity が消える等の変化があれば失敗します（失敗は履歴に残り、Muse にも見えます）。
- 提案の有効期限は既定 24 時間（`proposal_ttl_hours`）。期限切れは承認できません。承認待ちの上限は 20 件。
- フォームには CSRF トークンを埋め込み、画面は JavaScript なし・CSP 付きです。承認者名（HA ユーザー）は監査ログに残ります。
- 単体形態では、承認 UI は `127.0.0.1:8100` で待ち受け、HTTP Basic 認証（ユーザー名は任意、パスワードは
  `$CONFIG_DIR/approver_token`。MCP トークンとは別に自動生成）で保護します。リモートから使う場合は SSH ポートフォワード等で転送してください。
- 承認 UI は `DEPLOY_ENABLED=true` のときだけ起動します。

## Tailscale Service として公開する

公開するのは MCP のポート（8099）だけです。承認 UI（8100）は Tailscale に出しません。
本サーバーは「指定ポートで HTTP を待ち受ける」だけです。HTTPS 化は Tailscale 側で行います。
Music Assistant の FastMCP Server と同じ手順で、転送先だけ本サーバーのポートにします。

1. Tailscale 管理画面で Service（例: `svc:ha-automation`）を作成します。
2. サーバーが動くホストで、その Service を `http://127.0.0.1:8099`（アドオンならホストの 8099）へ転送します
   （例: `tailscale serve --service=svc:ha-automation --https=443 http://127.0.0.1:8099`。
   コマンド形式は Tailscale のバージョンで異なるため、Music Assistant で使った手順に合わせてください）。
3. MCP クライアント（Muse）には次を設定します。
   - URL: `https://ha-automation.<tailnet名>.ts.net/mcp/v1`
   - ヘッダ: `Authorization: Bearer <MCP トークン>`

パスは転送時にそのまま渡されます（サーバー側は `/mcp/v1` で受けます。変える場合は `MCP_PATH`）。

## MCP トークンの確認・再生成

- 起動時にトークンが未設定なら、32 バイトの乱数を生成し **権限 0600** で `mcp_token` ファイルに保存します。
  ログには「生成した」という事実とファイルの場所だけを出し、トークン実物は出しません。
- 保存先: アドオン `/config/mcp_token`（ホスト側 `/addon_configs/<...>_muse_automation_mcp/mcp_token`）、
  単体 `$CONFIG_DIR/mcp_token`（`MCP_TOKEN_FILE` で変更可）。
- **確認**: アドオンは File editor / Samba / SSH アドオンで上記ファイルを開きます。単体は `cat "$CONFIG_DIR/mcp_token"`。
- **再生成（ローテーション）**: サーバーを停止 → `mcp_token` を削除 → 起動（新しいトークンが生成される）→
  Muse 側のトークンを更新。旧トークンは即座に無効になります。
- 環境変数 `MCP_TOKEN`（32 文字以上）で明示指定することもできます（その場合ファイルは使われません）。
- 認証は全リクエスト・全パスで `Authorization: Bearer` を検証し、不一致・欠落は 401（定数時間比較）。

## カタログ許可リスト

`entity_catalog.yaml`（編集は再起動なしで次のリクエストから反映）:

```yaml
entities:
  - entity_id: light.living_room_ceiling   # 必須
    area: リビング                          # 任意（HA で area が取れない場合の手動付与／上書き）
    tags: [lighting]                       # 任意
```

- ここに載っていない entity は、HA に実在してもカタログに出ず、オートメーションからの参照も **すべて検証エラー**です。
- ファイルが無い／空／壊れている場合は許可リストが空として扱われ、検証はすべて失敗します（fail-closed）。
- `entity_catalog.sample.yaml` の ID は **実在しないダミー** です。起動時に設定ディレクトリへサンプルが自動コピーされます。

## ポリシー（任意: `policy.yaml`）

既定の許可: 対象 entity と同じドメインのサービス（`light.turn_on` で light を操作等）と
`persistent_notification.create/dismiss`。それ以外（`homeassistant.turn_on`、`script.*` 等）や
`webhook` / `event` トリガーは、`policy.yaml` の `extra_allowed_services` / `extra_allowed_trigger_types` に
**明示した場合のみ**許可されます。また `lock.*` / `alarm_control_panel.*`（物理的に影響が大きい）と
`notify.*`（通知先によっては HA から外部へ送信できるため）は、同ドメインでも既定では許可されず、
`extra_allowed_services` にサービス単位（例: `lock.unlock`、`notify.mobile_app_my_phone`）で明示した場合のみ許可されます。
この3ドメインではワイルドカード（`lock.*`、`notify.*` 等）は効きません。完全なサービス名だけが有効です。
カタログに lock / alarm / notify の entity を載せただけでは許可されません。`cover.*`（カーテン等）は同ドメイン許可のままです。
`homeassistant.restart/stop`、`shell_command.*` などは既定で禁止です。
詳細は `policy.sample.yaml`。

## DEPLOY_ENABLED の切り替え

- アドオン: 設定画面の `deploy_enabled` を ON → 再起動。
- 単体: `export DEPLOY_ENABLED=true`（または `false`）→ 再起動。
- 既定 `false`。無効時は `deploy_automation` / `delete_managed_automation` / `rollback_deployment` がツール一覧に出ません。
  まず読取系だけで運用し、問題なければ段階的に有効化してください。

## 定義の書き方

HA 2024.10+ 記法: `alias`（必須）, `description`, `triggers`, `conditions`, `actions`, `mode`。
**旧記法（`trigger`/`condition`/`action`、`platform`、`service`）は警告つきで正規化して受け付けます**
（`warnings` に `legacy_normalized` が出ます）。新旧が混在して矛盾する場合はエラーです。
ID はサーバーが `muse_<alias の slug>_<ランダム 6 桁>` で生成します。指定する場合は `id: muse_xxx`（`^muse_[a-z0-9_]+$`、全体で最大 100 文字。検証器と管理対象判定で共通の上限です）。

## テスト

```bash
pip install -r requirements-dev.txt
pytest -q          # 122 件。認証 / カタログ / 検証 / create-only / ロールバック / ツール公開面 / 承認フロー
```

## 関連ドキュメント

- [docs/DESIGN.md](docs/DESIGN.md) 設計概要と解釈
- [docs/VERIFICATION.md](docs/VERIFICATION.md) 実機での動作確認手順
- [docs/NOTES.md](docs/NOTES.md) 割り切り事項・利用者への確認事項
