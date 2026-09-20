# 林地权属版本账

集体林权改革场景下的**林地权属版本服务**：把宗地、共有成员、权利类型、流转期限和证照连成一条**只追加、不可覆盖的时间线**，并向授权机构提供证照核验接口。

## 解决的问题

- 承包边界、共有成员、流转期限散落在纸质材料中，旧扫描件可能盖过新决定；
- 共有成员增减、局部流转、证照撤回、迟到补录、两名经办人并发修订，都要保留**当时有效版本**；
- 企业持新证融资时，村集体需要迅速说明某次边界调整是否经过全体相关人确认；
- 敏感身份信息按角色脱敏；服务恢复后，未完成复核仍在原队列。

## 设计要点

| 需求 | 实现 |
| --- | --- |
| 不可覆盖的时间线 | 事件溯源 + 版本快照；`versions`/`audit_log` 由 SQLite 触发器禁止 UPDATE/DELETE |
| 迟到补录不改写历史 | 业务发生时间 `occurred_at` 与记账时间 `recorded_at` 双时间轴；状态按业务时间归并，历史版本快照永久冻结 |
| 成员增减 / 局部流转 / 边界调整 / 撤回 | `member_change`、`transfer`、`transfer_end`、`boundary_adjust`、证照撤回等事件类型 |
| 边界调整须全体确认 | 复核时按事件发生时点的共有人名单校验，补录揭示的共有人同样纳入核验 |
| 并发修订 | 乐观锁 `base_version`；输方申请以 `conflicted` 留档并返回 409，可基于新版本重新提交 |
| 复核队列不丢 | 申请持久化在 SQLite，进程重启后仍在原队列 |
| 按角色脱敏 | operator / reviewer / registrar 可见明文；institution 见姓名，身份证号、电话脱敏 |
| 完整审计 | 所有写操作与核验结果写入 SHA-256 哈希链，`GET /api/v1/audit/chain` 重放校验 |
| 核验阻断 | 撤回、被新证替代、版本落后、边界过期、确认缺失均返回机器可读阻断码 |

## 运行

```bash
python3 -m service.main          # 默认 FOREST_DB=data/forest_ledger.db，端口 3000
PORT=8080 FOREST_DB=/data/ledger.db python3 -m service.main
curl http://127.0.0.1:3000/health
```

仅使用 Python 3.11+ 标准库，无第三方依赖。令牌配置见 `service/auth.py`，可用环境变量 `FOREST_TOKENS`（JSON）覆盖默认开发令牌。

## 接口概览

所有接口（除 `/health`）需要 `Authorization: Bearer <token>`。详见 [docs/api.md](docs/api.md)。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/v1/changes` | operator | 提交变更（须带 `base_version`、`occurred_at`） |
| GET | `/api/v1/changes?status=pending` | operator, reviewer | 复核队列 |
| POST | `/api/v1/changes/{id}/decision` | reviewer | `approve` / `reject` |
| GET | `/api/v1/parcels/{id}` | 任意已认证 | 当前有效版本（按角色脱敏） |
| GET | `/api/v1/parcels/{id}/versions` | 任意已认证 | 完整版本时间线（含冻结快照、补录标记） |
| GET | `/api/v1/parcels/{id}/versions/{n}` | 任意已认证 | 取某一历史版本 |
| POST | `/api/v1/certificates` | registrar | 签发权证（旧有效证自动置为 superseded） |
| POST | `/api/v1/certificates/{no}/withdraw` | registrar | 撤回证照 |
| POST | `/api/v1/verify` | institution, registrar | 证照核验，返回版本与阻断理由 |
| GET | `/api/v1/audit`、`/api/v1/audit/chain` | reviewer, registrar | 审计明细与哈希链校验 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

12 项测试覆盖：验收会回放脚本（一宗林地登记→成员增加→边界调整→流转→迟到补录→重发证）、旧证核验与新证签发并发的线性一致性、两名经办人并发修订、撤回、流转期限四态、历史时点确认校验、角色鉴权与脱敏、重启队列恢复、只追加触发器、哈希链完整性。
