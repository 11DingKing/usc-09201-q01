# HTTP API 说明

基址：`/api/v1`。除 `/health` 外均需 `Authorization: Bearer <token>`，请求/响应均为 UTF-8 JSON。

默认开发令牌（生产环境用环境变量 `FOREST_TOKENS` 覆盖）：

| 令牌 | 角色 |
| --- | --- |
| `tok-operator` / `tok-operator-2` | 经办人 |
| `tok-reviewer` | 复核员 |
| `tok-registrar` | 登记机关 |
| `tok-institution` | 授权机构 |

错误响应统一为 `{"error": "<机器码>", "message": "...", ...附加上下文}`。

## 1. 提交变更 `POST /changes`（operator）

```json
{
  "parcel_id": "P-001",
  "kind": "register | member_change | transfer | transfer_end | boundary_adjust",
  "occurred_at": "2026-03-01",
  "base_version": 2,
  "payload": { }
}
```

- `occurred_at` 是**业务发生时间**（支持 `YYYY-MM-DD` 或完整 ISO 时间）；早于最新事件即标记 `backfill`。
- `base_version` 必须等于宗地当前生效版本，或同宗地无其他待复核件，否则 HTTP 409：
  `{"error":"base_version_conflict","current_version":n,"change_id":"..."}`，冲突申请以 `conflicted` 状态留档。
- `register` 的 `payload`：`parcel_name, location, boundary, members[], rights[]`
- `member_change`：`adds[]`（含 id/name/id_card/phone）、`removes[]`
- `transfer`：`transfer_id, transferee_name, portion, start_date, end_date`
- `transfer_end`：`transfer_id, end_date`
- `boundary_adjust`：`boundary, confirmations[]`——确认人必须恰好等于该时点全部共有人。

## 2. 复核队列与决定

- `GET /changes?status=pending|approved|rejected|conflicted`（operator、reviewer）
- `GET /changes/{change_id}`（operator、reviewer）
- `POST /changes/{change_id}/decision`（reviewer）：`{"decision":"approve|reject","note":"..."}`
- 复核通过即生成下一版本（版本号按通过顺序递增）并冻结快照。

## 3. 宗地与版本（任意已认证角色，按角色脱敏）

- `GET /parcels`：宗地清单
- `GET /parcels/{id}`：当前有效版本快照，含 `effective_version`、成员、边界、流转与 `boundary_assessment`
- `GET /parcels/{id}/versions`：完整时间线；每条含 `version, kind, occurred_at, recorded_at, backfill, snapshot`
- `GET /parcels/{id}/versions/{n}`：指定历史版本的冻结快照
- `GET /parcels/{id}/certs`：宗地全部证照

## 4. 证照（registrar）

- `POST /certificates`：`{"parcel_id":"...", "cert_no":"可选"}`。签发时宗地原有效证照自动置为 `superseded`。
- `POST /certificates/{cert_no}/withdraw`：`{"reason":"..."}`，理由必填。
- `GET /certificates/{cert_no}`：证照详情（含发证时冻结快照）。

## 5. 核验 `POST /verify`（institution、registrar）

请求：`{"cert_no":"林证-C1","today":"2026-09-20"}`（`today` 可选，默认 UTC 当日）。

响应：

```json
{
  "valid": false,
  "cert_no": "林证-C1",
  "issued_version": 1,
  "effective_version": 5,
  "cert_status": "superseded",
  "block_reasons": [
    {"code": "cert_superseded", "message": "...", "newer_cert_no": "林证-C2"},
    {"code": "cert_boundary_stale", "message": "...", "boundary_event_id": "..."},
    {"code": "boundary_confirmation_missing",
     "missing_member_ids": ["m4"], "missing_member_names": ["钱河"]}
  ],
  "transfers": [{"transfer_id":"T-9","derived_status":"active","end_date":"2031-05-31"}]
}
```

阻断码：`cert_withdrawn` / `cert_superseded` / `cert_outdated` / `cert_boundary_stale` / `boundary_confirmation_missing`。
核验动作本身（含阻断结果）写入审计。

## 6. 审计（reviewer、registrar）

- `GET /audit?parcel_id=...`：全部审计事件
- `GET /audit/chain`：重放 SHA-256 哈希链，返回 `{"intact":true,"entries":n,"head":"..."}`
