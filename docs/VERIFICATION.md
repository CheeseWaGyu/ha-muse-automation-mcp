# 動作確認手順書

前提: サーバー起動済み、Tailscale Service 経由で到達でき、`entity_catalog.yaml` に実在のセンサー（例 `sensor.xxx`）を 1 件以上登録済み。
Muse（または MCP 対応クライアント）から以下のツールを呼びます。`<SENSOR>` は許可リストに載せた実在 entity_id に置換。

## 0. 事前確認（既存オートメーションのスナップショット）
1. HA: 設定 → オートメーションとシーン で、既存オートメーションの件数と名前を控える（スクリーンショット可）。
2. `list_managed_automations` → `count: 0`（まだ `muse_` は無い）。
3. `get_entity_catalog` → 許可リストのものだけが返り、許可していない entity が **含まれない** こと。

## 1. 読取系（DEPLOY_ENABLED=false のまま）
4. ツール一覧に `deploy_automation` / `delete_managed_automation` / `rollback_deployment` が **無い** ことを確認。
5. `validate_automation` に下記を渡し `valid: true`:
```json
{"alias": "Muse 動作確認",
 "triggers": [{"trigger": "state", "entity_id": "<SENSOR>"}],
 "actions": [{"action": "persistent_notification.create",
              "data": {"title": "Muse test", "message": "sensor changed"}}]}
```
6. 拒否されること（いずれも `valid: false`）:
   - 許可リスト外の entity を指定 → `entity_not_allowed`
   - `{"action": "homeassistant.restart"}` → `service_dangerous`
   - `"id": "automation_x"` → `invalid_id`
7. `preview_deployment` → `automation_id` / `predicted_entity_id` / 正規化済み定義が返り、HA 画面に **何も増えない**。

## 2. 提案 → 承認 → デプロイ（DEPLOY_ENABLED=true にして再起動）
8. ツール一覧に書込系 3 つが現れ、**承認・却下に相当するツールが無い**ことを確認。HA サイドバーに「Muse 承認」が出る（管理者）。
9. 手順 5 の定義で `deploy_automation` → `status: pending_approval` と `proposal_id`。**この時点では HA にオートメーションが増えていない**ことを確認（HA 画面と `list_managed_automations` の `automations`）。
10. 「Muse 承認」を開く → 承認待ちに表示され、詳細でトリガー / アクション / 関係エンティティが読める。
11. まず **却下** を試す（理由入力）→ 何も作られず、`list_managed_automations` の `proposals` に `rejected` と理由が出る。
12. 再度 `deploy_automation` → 「承認してデプロイ」を押す。承認後、詳細に「作成済み: automation.xxx」が出る。
13. HA 画面: オートメーション一覧に「Muse 動作確認」が現れる。**既存オートメーションが無傷**（手順 0 の件数・名前・有効/無効が同じ）。
14. `<SENSOR>` の状態を変化させる（または手動で「実行」）→ 通知に "Muse test" が出る。
15. `list_managed_automations` → `automations` に今のものだけ、`proposals` に `approved` が出る。
16. **create-only の確認**: 同じ `id` を指定して `deploy_automation` → 提案段階で `id_exists` により拒否される。
17. **アクセス制御**: MCP の URL に承認画面のパス（`/p/...`）が無い（404/401）こと。MCP トークンでは承認 UI に入れないこと。

## 3. 削除・ロールバック
18. `delete_managed_automation` に **既存の非管理オートメーションの ID**（例: `1700000000000` のような数値 ID や `automation.xxx`）→ 拒否（`not_managed`）。既存は無傷。
19. `rollback_deployment`（手順 12 の ID）→ `result: deleted`。HA 画面から消える（デプロイ直前＝存在しない状態へ）。
20. もう一度 `rollback_deployment` → `result: restored`（取り消しの取り消し）。再び一覧に現れる。
21. `delete_managed_automation`（手順 12 の ID）→ `deleted: true`。HA 画面から消える。
22. `rollback_deployment` → `restored`（削除前の定義で復元）。最後に `delete_managed_automation` で片付ける。

## 4. 記録の確認
23. `<DATA_DIR>/audit.log`（アドオン内 `/data/audit.log`）に propose / approve / reject / deploy / delete / rollback が JSON Lines（承認者名つき） で追記され、**トークン類が含まれない** こと。
24. `<DATA_DIR>/backups/<automation_id>/` に操作ごとの JSON が保存され、世代数が設定値（既定 20）を超えないこと。

## 5. 認証
25. `Authorization` ヘッダ無し／誤ったトークンで `/mcp/v1` に POST → 401。正しいトークンで 200。
