# 接口说明

所有接口（除 `/health`）需请求头 `Authorization: Bearer <token>`。请求与响应均为 JSON（UTF-8）。时间字段为 ISO 8601（日期或日期时间，缺时区按 UTC）。

错误统一格式：

```json
{"error": {"code": "VERSION_CONFLICT", "message": "存在并发修订：……", "details": {"base_version": 6, "current_version": 7}}}
```

| 状态码 | 常见 code | 含义 |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` / `CONFIRMATION_INCOMPLETE` | 内容不合法、变更依据不足、边界调整未获全体确认 |
| 401 | `UNAUTHENTICATED` | 缺少或未知令牌 |
| 403 | `FORBIDDEN` | 角色无权访问 |
| 404 | `PARCEL_NOT_FOUND` / `VERSION_NOT_FOUND` / `CERT_NOT_FOUND` / `REVIEW_NOT_FOUND` / `NO_VERSION_AT_TIME` | 资源不存在 |
| 409 | `VERSION_CONFLICT` / `REVIEW_NOT_PENDING` / `CERT_ALREADY_REVOKED` / `PARCEL_CODE_EXISTS` | 与账本当前状态冲突（阻断理由见 message 与 details） |

## 健康检查

`GET /health` → `{"status": "ok"}`（无需令牌）

## 登记与变更

### `POST /parcels`（clerk/admin）

初始登记，生成第 1 版。

```json
{
  "parcel_code": "TD-2026-0001",
  "location": "青山镇白云村三组",
  "area_mu": 120.5,
  "boundary": ["东至山脊", "南至小河", "西至机耕道", "北至国有林场"],
  "members": [{"name": "张大山", "id_number": "110101196001011234", "phone": "13800001111", "share": 0.6}],
  "rights": [{"right_type": "承包经营权", "holder_name": "白云村三组", "term_start": "2026-01-01", "term_end": "2056-12-31"}],
  "occurred_at": "2026-01-10",
  "evidence": {"documents": [{"type": "承包合同", "ref": "HT-2026-001"}], "note": "第一批登记"}
}
```

→ `201` 版本视图。`parcel_code` 重复时 `409 PARCEL_CODE_EXISTS`。

### `POST /parcels/{id}/changes`（clerk/admin）

提交业务变更。`change_type` 与对应 `payload`：

- `member_change`：`{"add": [{"name", "id_number", "phone"?, "share"?}], "remove": ["M-..."]}` —— 共有成员增减；退出成员须存在，承包共有人不能为空，证件号不得重复。
- `boundary_adjust`：`{"boundary": [...], "area_mu": 118.0}` —— 边界调整；`evidence.confirmed_by` 必须包含当前全体共有人 member_id，否则 `400 CONFIRMATION_INCOMPLETE`。
- `partial_transfer`：`{"right_type", "holder_name", "scope_area_mu", "term_start", "term_end", "note"?}` —— 局部流转；流转面积不得超过宗地面积，期限必须起止有序。

公共字段：`base_version`（必填，提交所基于的当前版本号）、`occurred_at`（业务发生时间，早于已有版本业务时间时标记 `is_backfill`，不得早于初始登记）、`evidence`（必填，`documents` 或 `note` 至少其一）。

→ `201` 版本视图；`base_version` 过期时 `409 VERSION_CONFLICT`（details 含 `base_version` 与 `current_version`），阻断写入审计。

## 证照

### `POST /parcels/{id}/certificates`（clerk/admin）

签发证照，绑定当前版本；此前有效证照自动转为 `superseded`。字段：`base_version`（必填）、`holder_name?`、`note?`、`occurred_at?`（缺省为当前时间）。→ `201` 版本视图，证号形如 `LQ2026-0001`。

### `POST /certificates/{cert_id}/revoke`（clerk/admin）

撤回证照。字段：`base_version`、`reason`（必填）、`note?`、`occurred_at?`。重复撤回 `409 CERT_ALREADY_REVOKED`。历史版本中的证照状态保持不变。

### `POST /verify`（institution/clerk/reviewer/admin）

授权机构核验。`{"cert_no": "LQ2026-0001"}` 或 `{"cert_id": "Z-..."}` →

```json
{
  "status": "valid | superseded | revoked | unknown",
  "cert_no": "LQ2026-0001",
  "parcel_code": "TD-2026-0001",
  "holder_name": "张**",
  "issued_version": 5,
  "current_version": 8,
  "is_latest_version": false,
  "issued_at": "...", "checked_at": "..."
}
```

`status=revoked` 时附 `revoke_reason` / `revoked_version`。每次核验（含未知证号）均写入审计。

## 查询

- `GET /parcels`（reader 角色）→ 宗地列表。
- `GET /parcels/{id}` → 当前状态（`current_version` + `state`）。
- `GET /parcels/{id}/versions` → 版本时间线（摘要，含 `is_backfill`、`review_status`、`hash`）。
- `GET /parcels/{id}/versions/{n}` → 指定版本完整视图（快照、变更依据、哈希）。
- `GET /parcels/{id}/as-of?at=<时间>&basis=occurred|recorded` → 某时刻有效版本：
  - `occurred`（默认）：按业务发生时间重放重建，返回 `{"reconstructed": true, "snapshot": ..., "based_on_versions": [...], "latest_version_no": n}`；
  - `recorded`：返回该记录时刻系统已知的版本视图（`"reconstructed": false`）。
- `GET /parcels/{id}/audit` → 该宗地审计条目（提交、阻断、核验、复核决定）。
- `GET /parcels/{id}/integrity` → 重算版本哈希链：`{"ok": true, "versions_checked": 8}`。
- `GET /audit`（reviewer/admin）→ 全局审计链。

## 复核

- `GET /reviews?status=pending|approved|rejected|all`（reviewer/admin）→ 复核队列，按入队顺序。
- `POST /reviews/{id}/decision`（reviewer/admin）：`{"decision": "approved|rejected", "note"?}`。已决定任务再次决定返回 `409 REVIEW_NOT_PENDING`。

每次提交自动生成一项 `pending` 复核任务并持久化；服务重启后未完成复核仍在原队列。复核决定不修改历史版本，仅追加决定记录并写入审计。

## 版本视图字段

```json
{
  "parcel_id": "P-...", "version_no": 3, "change_id": "C-...",
  "change_type": "boundary_adjust",
  "occurred_at": "业务发生时间", "recorded_at": "系统记录时间",
  "actor": "提交人", "is_backfill": false,
  "payload": {"变更内容"}, "evidence": {"documents": [], "confirmed_by": [], "note": ""},
  "summary": "边界调整：面积 120.5 → 118.0 亩",
  "snapshot": {"parcel_code": "...", "location": "...", "area_mu": 118.0,
               "boundary": [], "members": [], "rights": [], "certificates": []},
  "prev_hash": "...", "hash": "...",
  "review_status": "pending", "review_id": "Q-..."
}
```
