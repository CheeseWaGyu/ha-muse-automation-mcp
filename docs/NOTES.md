# 割り切り・未検証・確認事項

## 検証状況
- 単体テスト 130 件はモック HA で通過（Python 3.12 / mcp 1.30.0）。
- **実機検証済み（2026-11-19〜）**: アドオンの Docker ビルド（約8分・エラーなし）、Supervisor 経由の
  REST config API でのオートメーション作成・削除、承認 UI（Ingress）の承認フロー、Tailscale Service 経由の
  MCP 接続はすべて実機で確認済みです。未検証なのはロールバックの実機実行と、スクリプト系
  （S2 まで実装済み・実機確認はアドオン v0.3.4 更新後。手順: scripts_enabled を ON にして再起動 →
  無害な通知だけのテストスクリプトで提案→承認→作成・台帳記録→削除提案→承認→削除まで）。
  - `Dockerfile` の `BUILD_FROM` 既定（`ghcr.io/home-assistant/base:latest`）と Alpine の python3 パッケージはビルドして確認してください。
  - `run.sh` の shebang（`with-contenv bashio`）はベースイメージ依存です。

## 承認 UI について（追加）
- Ingress の実機動作は確認済み（2026-11-19）。`X-Ingress-Path` によるリンク生成、Supervisor のアドレス（既定 `172.30.32.2`）での許可、
  `X-Remote-User-*` ヘッダ（承認者名）も想定どおり動作しています。
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

## 2026-11-20 S2 外部監査（第5回）への対応（v0.3.4）
- **policy のリスト型検証**: `extra_allowed_services` / `denied_services` / `extra_allowed_trigger_types` が
  リストでない値（文字列・辞書など）なら設定エラーにする。文字列は反復可能なので、従来の要素チェックだけでは
  `denied_services: "notify.*"` が文字単位に化けて拒否が無効化され得た。
- **読み込み失敗時の fail-closed 化**: YamlFile に error_value を追加し、PolicyLoader は読み込み・検証失敗時に
  `Policy(load_failed=True)` を返すようにした。この状態では check_service が全サービスを
  `policy_load_failed` で拒否し、許可トリガー種別も空になる。ファイルが無い場合は従来どおり既定ポリシー。
  カタログ側は従来から空＝全拒否で fail-closed のため変更なし。
- テスト: 型検証（3キー×文字列/辞書）、ローダーの fail-closed、検証レベルでの policy_load_failed を追加。

## 2026-11-20 S2 外部監査（第4回）への対応（v0.3.3）
1. **警告の可視化（対応）**: 台帳解除失敗などの result.warnings が利用者に届いていなかった問題に対応。
   proposals.summaries() が警告を含めて返すようになり（list_managed_scripts / list_managed_automations の
   proposals で見える）、承認 UI の詳細画面でも「注意」として警告を表示する。承認フロー経由のテストで両方を確認。
2. **同一ドメイン既定許可と外部送信（設計判断＋受け皿追加）**: 同ドメイン既定許可そのものは設計の核
   （カタログ＝所有者の許可リスト、カタログ外 entity は対象不可）として維持するが、README に信頼境界を
   明記した: カタログに外部連携を持つ統合の entity を載せることは、そのドメインのサービス実行の許可と
   同義であり、承認された HA サービス自体の外部連携まではアドオンでは防げない。その上で、所有者が
   個別に止める受け皿として policy.yaml の `denied_services`（fnmatch 可）を追加した。拒否は
   同ドメイン既定許可・extra_allowed_services・既定許可サービスのすべてに優先する（service_denied）。

## 2026-11-20 S2 外部監査（第3回）への対応（v0.3.2）
1. **notify の既定拒否（要対応）**: `notify` を SENSITIVE_SERVICE_DOMAINS に追加し、lock / alarm と同格にした。
   カタログに notify.* を載せて同ドメイン対象にしても既定では `service_sensitive` で拒否され、
   `extra_allowed_services` への完全一致の明示（例: notify.mobile_app_my_phone）でのみ許可される。
   ワイルドカード `notify.*` は効かない。回帰テスト: ポリシー単体（ワイルドカード拒否・完全一致許可）＋
   スクリプト検証レベルでの既定拒否。従来テスト2件は notify.* ワイルドカード前提だったため完全一致前提に更新。
   注意: これはオートメーション側の検証にも効く（従来 notify.* を書いていた policy.yaml は書き換えが必要）。
   ただし persistent_notification.* は DEFAULT_ALLOWED_SERVICES のまま（HA 内通知で外部送信しない）。
2. **孤立バックアップの明示（整合性）**: 台帳登録失敗時、先に保存された script_deploy バックアップの後に
   `script_deploy_failed` レコード（note 付き）を残し、履歴上で失敗とロールバックが分かるようにした。
   BackupStore.save に任意の note を追加。
3. **unregister 失敗の扱い（整合性）**: 削除後の台帳解除が失敗しても承認処理を例外終了させず、削除は成功で
   返し、警告（手動で managed_scripts.json から削除）と監査イベント
   `script_ledger_unregister_failed`（復旧方法付き）を残す。proposals の result にも warnings を含めた。

## 2026-11-20 S2 外部監査への対応（v0.3.1）
1. **承認時のフラグ再確認（重要）**: `scripts_enabled` が OFF の間は、既存の pending なスクリプト提案も
   承認・実行できないようにした。提案は消費せず pending のまま残る（再有効化すれば承認可能、期限で失効）。
2. **削除時の台帳ハッシュ照合（重要）**: 削除提案時と承認時（ScriptDeployer.delete_checked）の双方で、
   現在定義が台帳の作成時ハッシュと一致するか確認する。不一致は `definition_changed_since_creation` で拒否。
   作成後に手動変更されたスクリプトを「変更後の定義を見せて削除」することはできない。台帳再登録による
   引き継ぎ手順は未実装（今後の課題として明示）。
3. **バックアップのリソース別分離（要修正）**: スクリプト用 BackupStore を `backups/script/<id>/` に分離。
   オートメーションは既存実機バックアップとの互換のため `backups/<id>/` のまま移さない。
4. **記録失敗のロールバック（要修正）**: 作成後のバックアップ保存・台帳登録をトランザクションに含め、
   失敗時は HA 定義を削除してロールバックする。ロールバック不可の場合は監査ログに復旧情報
   （script_id / entity_id / manual_delete_required）を残し、エラーにも明記する。削除前のバックアップ
   保存失敗時も、削除を実行せず `record_failed` で止める。

## 2026-11-20 Phase S2 実装（v0.3.0）
- `deploy_script` / `delete_managed_script` を追加（`scripts_enabled` + `deploy_enabled` の両方 ON で公開、
  いずれも承認待ち提案のみ）。承認種別は `script_deploy` / `script_delete`。
- 作成は ScriptDeployer が単一ロックで直列化（検証→不在確認→check_config→作成→読み戻し→entity 出現確認、
  失敗時ロールバック）。成功時に管理台帳へ記録し、バックアップも保存する。
- 削除は台帳にあるものだけ。提案時点の定義を固定し、照合と削除を同じロックの中で行う
  （`changed_since_proposal`）。削除成功時に台帳からも外す。
- 承認 UI: 一覧・詳細に種別バッジ（作成=緑／削除=赤／ロールバック=黄／スクリプト作成=青／
  スクリプト削除=赤・破線）を追加し、追加と削除の履歴が一覧で区別できるようにした。
  スクリプト作成の詳細は sequence と定義全体、削除は対象名詞を「スクリプト」にして表示する。
- テンプレートを含むスクリプト提案では、承認画面に警告（warnings）が表示される（従来の仕組みをそのまま使用）。

## 2026-11-20 外部監査（S1）への対応（v0.2.1）
1. **管理対象判定の所有確認（要対応）**: `list_managed_scripts` が接頭辞だけで管理対象とみなしていた問題と、
   コメント（config が読めないものは含めない）と実装の不一致を修正。管理台帳 `managed_scripts.json`
   （S2 の作成成功時に記録）＋ config 読み戻し ＋ states 存在の 3 点がそろったものだけを管理対象とし、
   接頭辞だけのものは `unverified_count` に計上する。定義ハッシュが食い違う場合は
   `definition_changed_since_creation` フラグで返す。S2 の削除・復元は台帳＋照合を書き込み条件にする。
2. **テンプレート検証の位置づけ（要対応）**: 静的解析は安全境界ではないことを README と preview の注記に明記。
   S2 の承認画面でも警告 `template_partial_analysis` を明示する前提で承認する運用とする。
3. **ID 上限の不統一（要対応）**: 検証器 100 文字 vs 管理判定 85 文字を、全体最大 100 文字
   （`backup.MANAGED_ID_RE` = `muse_` ＋ 95 文字）に統一。86〜100 文字の ID が重複確認で NotManaged になり
   `ha_unreachable` と誤分類されていた問題も解消。境界値テスト追加。
4. **create-only の並行競合（S2 前に確認）**: HAClient の create 系（不在確認→POST）をロックで直列化し、
   並行作成テストを追加（同 ID の同時作成は 1 件だけ成功し POST も 1 回だけ）。S2 の deployer でも
   承認→作成→読み戻し→ロールバックを単一ロックで直列化する。
5. **S2 未実装（設計進捗）**: 指摘どおり未実装が正しい現状認識。`deploy_script` / `delete_managed_script`、
   スクリプト用承認種別、ヘルパー作成は S2 / H1 以降で、上記 1〜4 の前提を満たしてから有効化する。

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
