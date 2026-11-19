# 設計概要

## 構成
```
Muse (クラウド VM) --HTTPS/Bearer--> Tailscale Service --HTTP--> [本サーバー :8099 /mcp/v1]
                                                                   │ BearerAuthMiddleware (全パス)
                                                                   │ FastMCP (stateless, JSON)
                                                                   ▼
                                       validation ─ catalog(許可リスト) ─ policy
                                       deployer ─ backup / audit
                                                                   │ HAClient (用途限定)
                                                                   ▼
                              http://supervisor/core/api  (SUPERVISOR_TOKEN)   ※単体: HA_URL/api + HA_TOKEN
```

## 承認フロー
- `deploy_automation`（MCP）は `ProposalService.propose` を呼ぶだけ。検証済みの定義・ハッシュ・期限を `/data/proposals/p_*.json` に保存。
- `delete_managed_automation` / `rollback_deployment` も同様に提案（`propose_delete` / `propose_rollback`）を作るだけ（2026-11-19 変更）。
  削除は提案時点の定義スナップショットを、ロールバックは対象バックアップの timestamp を固定し、承認時に変化があれば実行しない。
- 承認・却下は `ui.py`（別ポート 8100、別 ASGI アプリ）からのみ。`ProposalService.approve` が
  ハッシュ照合 → 種別に応じ `Deployer.deploy`（再検証を含む既存の厳密手順）/ `delete` / `rollback` を実行する。MCP アプリに承認ルートは存在しない。
- Ingress: Supervisor のアドレスのみ許可（`UIAuthMiddleware`）。HA 側で認証・管理者限定（`panel_admin`）。
- 解釈: 元要件の「7 ツール」は維持し、`deploy_automation` を「提案作成」に変更。結果通知用ツールは増やさず、
  `list_managed_automations` に `proposals` を追加（定義本体は含まない）。

## 使用する HA REST API（これだけ）
| 用途 | API |
|---|---|
| 実在確認・一覧・entity 出現確認 | `GET /api/states` |
| area 取得 | `POST /api/template`（`area_name()`）|
| HA 全体の設定検証 | `POST /api/config/core/check_config` |
| 存在確認・読み戻し | `GET /api/config/automation/config/{id}` |
| 作成 | `POST /api/config/automation/config/{id}` |
| 削除 | `DELETE /api/config/automation/config/{id}` |

`automations.yaml` は直接触りません。`/api/config/automation/config/*` には `muse_` 検証を通った ID しか到達できません
（`HAClient.assert_managed`。バックアップ側にも同じ検証）。

## create-only の三重防御
1. `validate` が既存 ID を `id_exists` で拒否
2. `deploy` が POST 前に GET で不在を再確認
3. `HAClient.create_automation_config` 自身が POST 直前にもう一度 GET（HA の POST は上書き挙動のため）
さらに書込系はプロセス内で `asyncio.Lock` により直列化しています。

## 解釈した点
- **area の取得**: HA の REST にはレジストリ API が無いため、template API の `area_name()` で取得。YAML の `area` を優先。
- **`include_attributes`**: true で `device_class` / `unit_of_measurement` を付与（任意の生属性は漏洩防止のため返さない）。
- **旧記法**: 警告つきで正規化して受理。
- **ID**: 自動生成は `muse_` + alias の ASCII slug + ランダム 6 桁。preview の ID は「例」で、デプロイ時は別の値。揃えたい場合は `id` を指定。
- **entity_id の予測**: HA が alias から決める（日本語 alias は変換される／重複で `_2`）。デプロイ結果では state 属性 `id` 一致で実際の entity を特定。
- **サービス既定許可**: 「対象 entity と同ドメイン」+ `persistent_notification.create/dismiss`（動作確認手順で必要なため）。ただし `lock` / `alarm_control_panel` ドメインは既定拒否で、`extra_allowed_services` へのサービス単位の明示が必要（2026-11-19 変更）。
- **rollback**: 「直近の記録済み操作の取り消し」。deploy→削除、delete→再作成（同 ID が存在すれば上書き禁止で拒否）。
- **fail-closed の範囲**: `area_id`/`device_id`/`label_id` 指定、`entity_id: all`、テンプレートでの動的 entity 指定、
  `event`/`scene`/device アクション、`webhook` 等のトリガーは既定で拒否。
