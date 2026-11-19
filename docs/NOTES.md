# 割り切り・未検証・確認事項

## 検証状況
- 単体テスト 86 件はモック HA で通過（Python 3.12.3 / mcp 1.30.0）。
- **実機の HA / Supervisor / Tailscale / HA アドオンの Docker ビルドは未検証**です。`docs/VERIFICATION.md` で確認してください。
  - `Dockerfile` の `BUILD_FROM` 既定（`ghcr.io/home-assistant/base:latest`）と Alpine の python3 パッケージはビルドして確認してください。
  - `run.sh` の shebang（`with-contenv bashio`）はベースイメージ依存です。

## 承認 UI について（追加）
- **Ingress の実機動作は未検証**です。`X-Ingress-Path` によるリンク生成、Supervisor のアドレス（既定 `172.30.32.2`）での許可、
  `X-Remote-User-*` ヘッダ（承認者名）は HA の仕様に基づく実装で、環境差があれば `UI_ALLOWED_CLIENTS` や表示を調整してください。
- 承認の通知（スマホへのプッシュ等）は未実装です。承認待ちはサイドバーの画面を開いて確認します。
- CSRF トークンはプロセス再起動で無効になります（承認画面の再読み込みで復旧）。
- 承認ボタンは「作成」の承認です。作成後のオートメーションの動作内容を HA が実行する際の個別承認はありません。
- 削除とロールバックも承認制です（2026-11-19 対応。下記参照）。提案時点の定義／バックアップを固定し、承認時に変わっていれば実行しません。

## 実装しなかったこと・割り切り
- **更新（既存オートメーションの編集）は未実装**（create-only 要件のため）。変更したい場合は削除→再作成。
- **テンプレート解析は静的・ベストエフォート**。`states('固定ID')` 等は検出して許可リスト照合しますが、複雑な記述は
  検出漏れの可能性があるため警告 `template_partial_analysis` を返し、動的指定（変数連結など）は拒否します。
- **`GET /api/states` は全 entity を返します**（HA の仕様）。サーバー内で許可リスト／`muse_` に絞って返しており、
  それ以外は返却・記録しません。ただし HA からの取得自体は全件です。
- `check_config` は「デプロイ前の現状確認」であり、新しい定義の検証ではありません（定義自体は HA の config API が POST 時に検証）。
- 作成後の entity 出現待ちは最大 10 秒（`entity_wait_seconds`）。重い環境では誤ってロールバックする可能性があります。
- 読み戻し一致は `id` を除いた正規化 JSON の比較。HA が保存時に定義を加工する版では不一致→ロールバックになります
  （その場合は `VERIFICATION.md` 手順 9 で `readback` が失敗するので知らせてください）。
- サービスの既定は「同一ドメイン」ですが、`lock.*` / `alarm_control_panel.*` は例外で既定拒否です（2026-11-19 対応）。`policy.yaml` の `extra_allowed_services` にサービス単位で明示した場合のみ許可され、カタログ掲載だけでは許可されません。`cover.open_cover` 等は同ドメイン許可のままなので、カーテン以外の cover（ガレージ等）を載せる際は選別が防御の要です。
- `notify.*`、`homeassistant.turn_on/off` は既定禁止（`policy.yaml` で追加）。
- ステートレス HTTP（セッション無し）・JSON 応答で動作。SSE ストリーミング通知は使いません。
- レート制限・同時接続制限は未実装（Tailscale 内利用を前提）。
- DNS rebinding 保護は無効化（Tailscale の Host 名を通すため）。Bearer 認証が前提です。
- 依存は `mcp==1.30.0`（1.x）にピン留め。mcp 2.x では `FastMCP` が `MCPServer` に変わっており **そのままでは動きません**。

## 利用者に確認が必要な事項
1. 実際の HA / Supervisor 環境で、アドオンの `homeassistant_api: true` による `http://supervisor/core/api/config/...` が使えるか（`/api/config/automation/config/*` が通るか）。
2. Tailscale Service への転送コマンドと、Music Assistant 側と同じ HTTPS ポート（443 か）。
3. `entity_catalog.yaml` に載せる entity の範囲（特に lock / cover / alarm など物理影響の大きいもの）。
4. 通知等に `notify.*` を使うか（`policy.yaml` へ追加するか）。
5. `repository.yaml` / `config.yaml` の GitHub URL・maintainer、ライセンスの選定（LICENSE は未同梱）。
6. HA の最低バージョン（2024.10 以降の記法前提。実機が 2024.10 未満なら要相談）。
7. 日本語 alias を使う場合の entity_id（HA の変換結果）に問題がないか。

## 2026-11-19 外部監査への対応（コミット履歴参照）
1. **空の状態一覧で許可リスト検証が飛ぶ問題を修正**: `validation.py` の照合を `if states:` の外に出し、
   状態一覧が空の場合は全参照 entity が「存在しない」として fail-closed に倒れるようにした。回帰テスト追加。
2. **event トリガーを既定拒否に統一**: `policy.py` の既定許可から `event` を外し、DESIGN.md の記述と一致させた。
   許可する場合は `policy.yaml` の `extra_allowed_trigger_types` に明示する。テスト追加。
3. **承認画面に定義全体を表示**: 提案詳細に「定義全体（承認対象の全フィールド）」として正規化済み定義の
   YAML 全文を追加し、`variables` / `trigger_variables` / `max` 等も承認者が確認できるようにした。テスト追加。
4. **単体形態の平文 HTTP への注意**: README に警告を追記し、単体形態＋ `http://` の場合は起動時にも警告ログを出すようにした。
   （アドオン形態は Supervisor 経由のため対象外。）

## 2026-11-19 運用指摘への対応（削除・ロールバック承認制／高影響ドメイン）
1. **削除・ロールバックを承認制に変更**: `delete_managed_automation` / `rollback_deployment` は直接実行せず、
   deploy と同じ「承認待ち提案」を作るだけにした。削除提案は提案時点の定義を、ロールバック提案は対象バックアップ
   （timestamp）を固定し、承認時にどちらかが変わっていれば `changed_since_proposal` で実行しない。
   承認 UI は種別（作成／削除／ロールバック）ごとに内容とボタンを表示する。回帰テスト追加（全 82 件通過）。
2. **lock / alarm_control_panel を既定拒否**: 同ドメイン許可の例外とし、`policy.yaml` の
   `extra_allowed_services` にサービス単位で明示した場合のみ許可（エラーコード `service_sensitive`）。
   cover はカーテン用途のため同ドメイン許可のまま据え置き。

## 2026-11-19 再精査への対応（ロールバック照合／ワイルドカード）
1. **ロールバック承認時の現在定義照合を追加（重要）**: 従来は対象バックアップの timestamp だけを照合していたため、
   timestamp が変わらないまま HA 上で手動編集された定義が、承認で削除される可能性があった。
   `Deployer.rollback_checked` を追加し、timestamp 照合・現在定義とバックアップ `after` の照合・実行を
   すべて deployer の同じ排他ロックの中で行うようにした。不一致は `changed_since_proposal` で実行しない。
   削除も同じ理由で `delete_checked` に寄せ、照合と削除を同一ロック内に統一した。回帰テスト追加。
2. **高影響ドメインでワイルドカードを無効化（中）**: `lock` / `alarm_control_panel` では
   `extra_allowed_services` のパターン一致を使わず、サービス名の完全一致だけを許可する。
   `lock.*` や `alarm_control_panel.*`、`lock.u*` 等では許可されなくなった（非高影響ドメインのワイルドカードは従来どおり）。
