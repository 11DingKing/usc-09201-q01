# 林地权属版本账

面向集体林权改革的林地权属版本服务：把宗地、成员、权利类型、证照与变更依据连成**不可覆盖的时间线**，向授权机构提供证照核验接口。纯 Python 标准库实现，可独立运行。

## 快速开始

```bash
# 启动服务（默认端口 3000，数据目录 ./data）
python3 -m service.main
PORT=8080 DATA_DIR=/var/lib/forest-ledger python3 -m service.main

# 运行测试
python3 -m unittest

# 验收回放：一宗林地三次变更 + 旧证核验与新证签发并发
python3 scripts/acceptance_replay.py
```

## 角色与令牌

除 `/health` 外所有接口需 `Authorization: Bearer <token>`。默认开发令牌（可在数据目录放置 `tokens.json` 覆盖）：

| 令牌 | 角色 | 权限 |
| --- | --- | --- |
| `token-admin` | admin | 全部权限，敏感信息明文 |
| `token-clerk-a` / `token-clerk-b` | clerk | 登记、变更、签发、撤回，明文 |
| `token-reviewer` | reviewer | 复核队列与决定、审计，明文 |
| `token-village` | village | 查看宗地版本与审计，证件号/电话脱敏 |
| `token-bank` | institution | 仅证照核验，持证人姓名脱敏 |

## 接口概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/parcels` | 初始登记（第 1 版） |
| GET | `/parcels` / `/parcels/{id}` | 宗地列表 / 当前状态 |
| POST | `/parcels/{id}/changes` | 提交变更（成员增减、边界调整、局部流转） |
| GET | `/parcels/{id}/versions` / `.../versions/{n}` | 版本时间线 |
| GET | `/parcels/{id}/as-of?at=...&basis=occurred\|recorded` | 某时刻的有效版本 |
| POST | `/parcels/{id}/certificates` | 签发证照（旧证自动取代） |
| POST | `/certificates/{id}/revoke` | 撤回证照 |
| POST | `/verify` | 授权机构核验证照 |
| GET | `/reviews` / POST `/reviews/{id}/decision` | 复核队列与决定 |
| GET | `/parcels/{id}/audit` / `/audit` | 审计链 |
| GET | `/parcels/{id}/integrity` | 版本哈希链校验 |

完整字段说明见 [docs/api.md](docs/api.md)，领域约定见 [docs/domain.md](docs/domain.md)。

## 关键语义

- **不可覆盖**：每次提交追加一个哈希链版本，历史版本永不修改；账本被篡改时服务拒绝启动。
- **当时有效版本**：迟到补录按业务发生时间入链；`basis=occurred` 按业务时间重放重建，`basis=recorded` 返回系统当时已知的版本。
- **并发控制**：提交携带 `base_version`，过期提交返回 `409 VERSION_CONFLICT` 及阻断理由，并写入审计。
- **复核队列**：每次提交生成复核任务并持久化，服务恢复后未完成复核仍在原队列。
- **完整审计**：提交、阻断、核验、复核决定全部入哈希链审计日志。
