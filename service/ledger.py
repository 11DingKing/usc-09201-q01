"""领域核心：版本归并、变更校验与证照核验规则。

本模块全部为纯函数，便于单测与回放。事件一经复核通过即不可变，当前有效状态
通过对全部已生效事件按 **业务发生时间** 归并得到；因此迟到补录只会落在时间线
的历史位置，不可能用旧扫描件覆盖更晚作出的新决定。每次记入账本的版本都会被
物化冻结（见 ``store``），历史版本永不重算。
"""

from __future__ import annotations

from typing import Any

# 变更类型
REGISTER = "register"                 # 初始登记
MEMBER_CHANGE = "member_change"       # 共有成员增减
TRANSFER = "transfer"                 # 局部流转（设立经营权流转）
TRANSFER_END = "transfer_end"         # 流转提前终止
BOUNDARY_ADJUST = "boundary_adjust"   # 承包边界调整

CHANGE_KINDS = {REGISTER, MEMBER_CHANGE, TRANSFER, TRANSFER_END, BOUNDARY_ADJUST}


class DomainError(ValueError):
    """业务规则阻断；``reason`` 为机器可读的阻断码。"""

    def __init__(self, reason: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.extra = extra


def new_state() -> dict[str, Any]:
    return {
        "parcel_name": None,
        "location": None,
        "boundary": None,
        "boundary_event_id": None,
        "boundary_occurred_at": None,
        "boundary_confirmations": None,
        "boundary_required": None,
        "members": {},
        "rights": [],
        "transfers": {},
    }


def _sort_key(event: dict[str, Any]) -> tuple[Any, int]:
    # 业务发生时间为主序，入账序号为次序（同一时刻先记录者在前）
    return (event["occurred_ts"], event["seq"])


def _apply(state: dict[str, Any], event: dict[str, Any]) -> None:
    kind = event["kind"]
    payload = event["payload"]
    if kind == REGISTER:
        state["parcel_name"] = payload.get("parcel_name")
        state["location"] = payload.get("location")
        state["boundary"] = payload.get("boundary")
        state["boundary_event_id"] = event["event_id"]
        state["boundary_occurred_at"] = event["occurred_at"]
        state["boundary_confirmations"] = None
        state["boundary_required"] = None
        state["rights"] = list(payload.get("rights", []))
        for member in payload.get("members", []):
            state["members"][member["id"]] = dict(member)
    elif kind == MEMBER_CHANGE:
        for member in payload.get("adds", []):
            state["members"][member["id"]] = dict(member)
        for member_id in payload.get("removes", []):
            state["members"].pop(member_id, None)
    elif kind == TRANSFER:
        state["transfers"][payload["transfer_id"]] = {
            "transfer_id": payload["transfer_id"],
            "transferee_name": payload["transferee_name"],
            "portion": payload.get("portion"),
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            "ended_at": None,
            "event_id": event["event_id"],
        }
    elif kind == TRANSFER_END:
        transfer = state["transfers"].get(payload["transfer_id"])
        if transfer is not None:
            transfer["ended_at"] = payload["end_date"]
    elif kind == BOUNDARY_ADJUST:
        state["boundary"] = payload.get("boundary")
        state["boundary_event_id"] = event["event_id"]
        state["boundary_occurred_at"] = event["occurred_at"]
        state["boundary_confirmations"] = list(payload.get("confirmations", []))
        state["boundary_required"] = list(payload.get("required", []))


def fold(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把已生效事件按业务发生顺序归并为当前状态。"""

    state = new_state()
    for event in sorted(events, key=_sort_key):
        _apply(state, event)
    return state


def freeze_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    """归并并补齐版本元数据，生成可冻结的快照。"""

    ordered = sorted(events, key=lambda e: e["version"])
    snapshot = fold(events)
    snapshot["effective_version"] = max((e["version"] for e in events), default=0)
    snapshot["latest_event_id"] = ordered[-1]["event_id"] if ordered else None
    snapshot["recorded_at"] = max((e["recorded_at"] for e in events), default=None)
    snapshot["boundary_assessment"] = assess_latest_boundary(events)
    return snapshot


def assess_latest_boundary(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """评估最新（按业务时间）边界调整是否取得该时点全体共有人确认。

    评估基于完整事件史回放：即使共有人是之后才被补录确认的，只要其在边界
    调整时点已属共有人，未在确认名单中即记为缺失。
    """

    boundaries = [e for e in events if e["kind"] == BOUNDARY_ADJUST]
    if not boundaries:
        return None
    latest = max(boundaries, key=lambda e: (e["occurred_ts"], e["seq"]))
    before = [
        e
        for e in events
        if (e["occurred_ts"], e["seq"]) < (latest["occurred_ts"], latest["seq"])
    ]
    state_before = fold(before)
    required = set(state_before["members"])
    confirmed = set(latest["payload"].get("confirmations", []))
    return {
        "event_id": latest["event_id"],
        "occurred_at": latest["occurred_at"],
        "required_member_ids": sorted(required),
        "confirmed_member_ids": sorted(confirmed),
        "missing": sorted(required - confirmed),
        "extra_confirmer_ids": sorted(confirmed - required),
    }


def state_as_of(events: list[dict[str, Any]], occurred_ts: float) -> dict[str, Any]:
    """按业务发生时间回放，得到某一时点“当时有效”的状态（重构视图）。"""

    return fold([e for e in events if e["occurred_ts"] <= occurred_ts])


def validate_candidate(
    existing: list[dict[str, Any]], candidate: dict[str, Any]
) -> None:
    """复核通过前校验候选变更；不满足规则时抛出 :class:`DomainError`。

    校验针对的是候选事件业务发生时点的状态，因此补录事件按历史位置校验。
    """

    kind = candidate["kind"]
    payload = candidate["payload"]
    if kind not in CHANGE_KINDS:
        raise DomainError("unknown_kind", f"未知变更类型：{kind}")

    if kind == REGISTER:
        if existing:
            raise DomainError(
                "already_registered", "该宗地已有初始登记，不能重复登记"
            )
        if not payload.get("boundary"):
            raise DomainError("boundary_required", "初始登记必须包含承包边界")
        if not payload.get("members"):
            raise DomainError("members_required", "初始登记必须包含共有成员")
        return

    if not existing:
        raise DomainError("not_registered", "宗地尚未初始登记")

    # 先按业务时间排好，定位候选事件并取得其“发生之前”的状态
    combined = sorted(existing + [candidate], key=_sort_key)
    state = new_state()
    saw_candidate = False
    for event in combined:
        if event is candidate:
            saw_candidate = True
            break
        _apply(state, event)
    if not saw_candidate:  # 理论上不会发生
        raise DomainError("candidate_lost", "候选事件定位失败")

    if kind == MEMBER_CHANGE:
        adds = payload.get("adds", [])
        removes = payload.get("removes", [])
        if not adds and not removes:
            raise DomainError("empty_change", "成员增减名单均为空")
        for member in adds:
            for field in ("id", "name"):
                if not member.get(field):
                    raise DomainError(
                        "member_field_missing", f"新增成员缺少字段：{field}"
                    )
            if member["id"] in state["members"]:
                raise DomainError(
                    "member_already_present",
                    f"成员在该时点已在共有名单中：{member['id']}",
                    member_id=member["id"],
                )
        unknown = [mid for mid in removes if mid not in state["members"]]
        if unknown:
            raise DomainError(
                "member_not_found",
                "拟移除成员在该时点不在共有名单中",
                member_ids=unknown,
            )
    elif kind == TRANSFER:
        for field in ("transfer_id", "transferee_name", "start_date", "end_date"):
            if not payload.get(field):
                raise DomainError("transfer_field_missing", f"流转缺少字段：{field}")
        if payload["transfer_id"] in state["transfers"]:
            raise DomainError(
                "transfer_exists", f"流转编号已存在：{payload['transfer_id']}"
            )
        if payload["end_date"] <= payload["start_date"]:
            raise DomainError("invalid_term", "流转终止日期不得早于起始日期")
    elif kind == TRANSFER_END:
        transfer = state["transfers"].get(payload.get("transfer_id"))
        if transfer is None:
            raise DomainError(
                "transfer_not_found",
                f"流转不存在：{payload.get('transfer_id')}",
                transfer_id=payload.get("transfer_id"),
            )
        if not payload.get("end_date"):
            raise DomainError("end_date_required", "提前终止必须包含终止日期")
    elif kind == BOUNDARY_ADJUST:
        if not payload.get("boundary"):
            raise DomainError("boundary_required", "边界调整必须包含新边界")
        required = set(state["members"])
        confirmed = set(payload.get("confirmations", []))
        missing = sorted(required - confirmed)
        unknown = sorted(confirmed - required)
        if missing:
            raise DomainError(
                "boundary_confirmation_missing",
                "边界调整未经全体相关共有人确认",
                missing_member_ids=missing,
                required_member_ids=sorted(required),
            )
        if unknown:
            raise DomainError(
                "unknown_confirmer",
                "确认人中存在该时点的非共有人",
                member_ids=unknown,
            )
        # 冻结本事件适用的应确认名单，供后续核验长期比对
        payload["required"] = sorted(required)


def verify_certificate(
    cert: dict[str, Any],
    current: dict[str, Any],
    issuance_snapshot: dict[str, Any],
    newer_cert: dict[str, Any] | None,
    today: str,
) -> dict[str, Any]:
    """证照核验：返回结构化结论与阻断理由列表。"""

    blocks: list[dict[str, Any]] = []

    if cert["status"] == "withdrawn":
        blocks.append(
            {
                "code": "cert_withdrawn",
                "message": "证照已被登记机关撤回",
                "withdrawn_at": cert.get("withdrawn_at"),
                "reason": cert.get("withdrawal_reason"),
            }
        )
    elif cert["status"] == "superseded" or newer_cert is not None:
        blocks.append(
            {
                "code": "cert_superseded",
                "message": "旧证已被新签发的证照替代",
                "newer_cert_no": (newer_cert or {}).get("cert_no"),
            }
        )

    effective_version = current.get("effective_version") or 0
    if (
        cert["status"] == "active"
        and newer_cert is None
        and effective_version > cert["version"]
    ):
        # 新决定已经生效但新证尚未签发：旧扫描件不得被当作当前依据
        blocks.append(
            {
                "code": "cert_outdated",
                "message": "证载版本落后于当前生效版本，旧证不能覆盖新决定",
                "issued_version": cert["version"],
                "current_version": effective_version,
            }
        )

    current_boundary = current.get("boundary_event_id")
    issued_boundary = issuance_snapshot.get("boundary_event_id")
    if current_boundary and issued_boundary and current_boundary != issued_boundary:
        blocks.append(
            {
                "code": "cert_boundary_stale",
                "message": "发证后承包边界发生调整，旧证记载已不是最新决定",
                "boundary_event_id": current_boundary,
                "boundary_occurred_at": current.get("boundary_occurred_at"),
            }
        )

    required = current.get("boundary_assessment")
    if required and required.get("missing"):
        missing = required["missing"]
        name_of = {m["id"]: m.get("name") for m in current["members"].values()}
        blocks.append(
            {
                "code": "boundary_confirmation_missing",
                "message": "边界调整未经全体相关共有人确认（含迟到补录揭示的共有人）",
                "boundary_event_id": required.get("event_id"),
                "missing_member_ids": missing,
                "missing_member_names": [name_of.get(mid, mid) for mid in missing],
            }
        )

    transfers = []
    for tr in current.get("transfers", {}).values():
        status = "active"
        if tr["ended_at"]:
            status = "ended_early"
        elif today < tr["start_date"]:
            status = "scheduled"
        elif today > tr["end_date"]:
            status = "expired"
        transfers.append({**tr, "derived_status": status})

    return {
        "valid": not blocks,
        "block_reasons": blocks,
        "cert_no": cert["cert_no"],
        "parcel_id": cert["parcel_id"],
        "issued_version": cert["version"],
        "effective_version": current.get("effective_version"),
        "cert_status": cert["status"],
        "transfers": sorted(transfers, key=lambda t: t["start_date"]),
    }
