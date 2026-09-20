"""林地权属版本服务核心逻辑。

设计要点：
- 不可覆盖时间线：每次变更追加一个带哈希链的版本，历史版本永不修改；
  宗地、成员、权利类型、证照与变更依据都保存在版本快照中。
- 当时有效版本：迟到补录按业务发生时间（occurred_at）入链，
  按记录时间（recorded_at）查询仍返回补录前系统已知的版本。
- 并发控制：所有提交携带 base_version，全局锁串行化，
  过期提交被阻断并给出结构化理由，同时写入审计。
- 复核队列：每次提交生成复核任务并持久化，服务恢复后未完成复核仍在原队列。
- 审计：提交、阻断、核验、复核决定全部入哈希链审计日志。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone

from .errors import Conflict, NotFound, ValidationError
from .store import JsonlStore

GENESIS_HASH = "0" * 64

USER_CHANGE_TYPES = {"member_change", "boundary_adjust", "partial_transfer"}
ALL_CHANGE_TYPES = USER_CHANGE_TYPES | {"register", "certificate_issue", "certificate_revoke"}

AUDIT_HASH_FIELDS = ("seq", "at", "actor", "role", "action", "parcel_id", "detail", "prev_hash")
VERSION_HASH_FIELDS = (
    "parcel_id", "version_no", "change_type", "change_id",
    "occurred_at", "recorded_at", "actor", "payload", "evidence",
    "is_backfill", "snapshot", "prev_hash",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return utcnow().isoformat(timespec="microseconds")


def parse_ts(value, field="occurred_at") -> datetime:
    """解析 ISO 日期或日期时间；缺时区按 UTC 处理。

    兼容 URL 查询串中 "+" 被解码为空格的常见情况。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 不能为空，需为 ISO 日期或日期时间")
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        repaired = re.sub(r" (\d{2}:\d{2}(:\d{2})?)$", r"+\1", text)
        try:
            dt = datetime.fromisoformat(repaired)
        except ValueError:
            raise ValidationError(f"{field} 不是合法的日期时间：{value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _norm_ts(value, field="occurred_at") -> str:
    return parse_ts(value, field).isoformat(timespec="microseconds")


def _hash_obj(obj) -> str:
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _version_hash(version: dict) -> str:
    return _hash_obj({k: version[k] for k in VERSION_HASH_FIELDS})


def _audit_hash(entry: dict) -> str:
    return _hash_obj({k: entry[k] for k in AUDIT_HASH_FIELDS})


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _member_id(parcel_id: str, id_number: str) -> str:
    """成员 ID 由宗地与其证件号派生：重放顺序不同也能得到同一 ID。"""
    digest = hashlib.sha256(f"{parcel_id}|{id_number}".encode("utf-8")).hexdigest()
    return f"M-{digest[:12]}"


def _right_id(parcel_id: str, right: dict) -> str:
    """权利 ID 由权利内容派生，保证按业务时间重放时确定。"""
    key = "|".join([
        parcel_id, right["right_type"], right["holder_name"],
        str(right["scope_area_mu"]), str(right["term_start"]), str(right["term_end"]),
    ])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"R-{digest[:12]}"


def _require_text(payload: dict, field: str, label: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}（{field}）不能为空")
    return value.strip()


def _require_area(value, field="area_mu") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValidationError(f"{field} 必须为大于 0 的数字（亩）")
    return float(value)


class TenureService:
    """林地权属版本账。所有写操作在全局锁内串行提交。"""

    def __init__(self, store: JsonlStore):
        self.store = store
        self._lock = threading.RLock()
        self._parcels: dict[str, dict] = {}
        self._certs: dict[str, dict] = {}
        self._cert_nos: dict[str, str] = {}
        self._reviews: list[dict] = []
        self._review_index: dict[str, dict] = {}
        self._review_by_change: dict[str, dict] = {}
        self._audit_log: list[dict] = []
        self._audit_prev = GENESIS_HASH
        self._load()

    # ------------------------------------------------------------------
    # 载入与重放
    # ------------------------------------------------------------------
    def _load(self) -> None:
        for record in self.store.read_events():
            self._apply_version(record, verify=True)
        for record in self.store.read_reviews():
            self._apply_review_record(record)
        for entry in self.store.read_audit():
            if entry.get("prev_hash") != self._audit_prev or _audit_hash(entry) != entry.get("hash"):
                raise RuntimeError("审计链校验失败：账本可能被篡改")
            self._audit_log.append(entry)
            self._audit_prev = entry["hash"]

    def _apply_version(self, version: dict, verify: bool) -> None:
        parcel = self._parcels.get(version["parcel_id"])
        if parcel is None:
            if version["version_no"] != 1:
                raise RuntimeError("版本链断裂：缺少初始登记")
            parcel = {"versions": []}
            self._parcels[version["parcel_id"]] = parcel
        versions = parcel["versions"]
        if version["version_no"] != len(versions) + 1:
            raise RuntimeError("版本链断裂：版本号不连续")
        expected_prev = versions[-1]["hash"] if versions else GENESIS_HASH
        if verify and (version.get("prev_hash") != expected_prev or _version_hash(version) != version.get("hash")):
            raise RuntimeError("版本链校验失败：账本可能被篡改")
        versions.append(version)
        for cert in version["snapshot"].get("certificates", []):
            self._certs[cert["cert_id"]] = {
                **cert,
                "parcel_id": version["parcel_id"],
                "parcel_code": version["snapshot"]["parcel_code"],
            }
            self._cert_nos[cert["cert_no"]] = cert["cert_id"]

    def _apply_review_record(self, record: dict) -> None:
        if record["type"] == "enqueue":
            item = {
                "review_id": record["review_id"],
                "parcel_id": record["parcel_id"],
                "version_no": record["version_no"],
                "change_id": record["change_id"],
                "change_type": record["change_type"],
                "enqueued_at": record["enqueued_at"],
                "status": "pending",
                "decision": None,
                "decided_by": None,
                "decided_at": None,
                "note": None,
            }
            self._reviews.append(item)
            self._review_index[item["review_id"]] = item
            self._review_by_change[item["change_id"]] = item
        elif record["type"] == "decision":
            item = self._review_index[record["review_id"]]
            item["status"] = record["decision"]
            item["decision"] = record["decision"]
            item["decided_by"] = record["decided_by"]
            item["decided_at"] = record["decided_at"]
            item["note"] = record.get("note")

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def _audit(self, actor: dict, action: str, parcel_id, detail: dict) -> None:
        entry = {
            "seq": len(self._audit_log) + 1,
            "at": _now_iso(),
            "actor": actor["actor_id"],
            "role": actor["role"],
            "action": action,
            "parcel_id": parcel_id,
            "detail": detail,
            "prev_hash": self._audit_prev,
        }
        entry["hash"] = _audit_hash(entry)
        self._audit_prev = entry["hash"]
        self._audit_log.append(entry)
        self.store.append_audit(entry)

    # ------------------------------------------------------------------
    # 提交变更
    # ------------------------------------------------------------------
    def register_parcel(self, *, actor: dict, payload: dict, occurred_at, evidence) -> dict:
        """初始登记，生成第 1 版。"""
        with self._lock:
            payload = dict(payload or {})
            self._validate_payload("register", payload, None)
            parcel_id = _new_id("P")
            version = self._commit(
                actor=actor, parcel_id=parcel_id, change_type="register",
                payload=payload, occurred_at=occurred_at, evidence=evidence,
            )
            return self._version_view(version)

    def submit_change(self, *, actor: dict, parcel_id: str, change_type: str,
                      payload: dict, occurred_at, evidence, base_version) -> dict:
        """提交业务变更：成员增减、边界调整、局部流转。"""
        with self._lock:
            if change_type not in USER_CHANGE_TYPES:
                raise ValidationError(
                    f"不支持的变更类型：{change_type!r}",
                    details={"allowed": sorted(USER_CHANGE_TYPES)},
                )
            parcel = self._require_parcel(parcel_id)
            self._check_base_version(actor, parcel_id, change_type, base_version)
            payload = dict(payload or {})
            self._validate_payload(change_type, payload, parcel["versions"][-1]["snapshot"])
            version = self._commit(
                actor=actor, parcel_id=parcel_id, change_type=change_type,
                payload=payload, occurred_at=occurred_at, evidence=evidence,
            )
            return self._version_view(version)

    def issue_certificate(self, *, actor: dict, parcel_id: str, base_version,
                          holder_name=None, note="", occurred_at=None) -> dict:
        """签发证照：绑定当前版本，旧证自动转为已取代。"""
        with self._lock:
            parcel = self._require_parcel(parcel_id)
            self._check_base_version(actor, parcel_id, "certificate_issue", base_version)
            snapshot = parcel["versions"][-1]["snapshot"]
            cert_no = f"LQ{utcnow():%Y}-{len(self._certs) + 1:04d}"
            holder = holder_name or (snapshot["members"][0]["name"] if snapshot["members"] else "")
            payload = {
                "cert_id": _new_id("Z"),
                "cert_no": cert_no,
                "holder_name": holder,
                "note": note or "",
            }
            evidence = {"documents": [{"type": "证照签发记录", "ref": cert_no}], "note": note or ""}
            version = self._commit(
                actor=actor, parcel_id=parcel_id, change_type="certificate_issue",
                payload=payload, occurred_at=occurred_at or _now_iso(), evidence=evidence,
            )
            return self._version_view(version)

    def revoke_certificate(self, *, actor: dict, cert_id: str, base_version,
                           reason="", note="", occurred_at=None) -> dict:
        """撤回证照：历史版本保留，撤回本身成为新版本。"""
        with self._lock:
            cert = self._certs.get(cert_id)
            if cert is None:
                raise NotFound(f"证照不存在：{cert_id}", code="CERT_NOT_FOUND")
            parcel_id = cert["parcel_id"]
            self._require_parcel(parcel_id)
            self._check_base_version(actor, parcel_id, "certificate_revoke", base_version)
            if cert["status"] == "revoked":
                raise Conflict("证照已撤回，不能重复撤回", code="CERT_ALREADY_REVOKED",
                               details={"cert_id": cert_id, "cert_no": cert["cert_no"]})
            if not isinstance(reason, str) or not reason.strip():
                raise ValidationError("撤回证照必须说明理由 reason")
            payload = {"cert_id": cert_id, "cert_no": cert["cert_no"], "reason": reason.strip()}
            evidence = {
                "documents": [{"type": "证照撤回决定", "ref": cert["cert_no"]}],
                "note": note or reason.strip(),
            }
            version = self._commit(
                actor=actor, parcel_id=parcel_id, change_type="certificate_revoke",
                payload=payload, occurred_at=occurred_at or _now_iso(), evidence=evidence,
            )
            return self._version_view(version)

    def _check_base_version(self, actor: dict, parcel_id: str, change_type: str, base_version) -> None:
        if isinstance(base_version, bool) or not isinstance(base_version, int):
            raise ValidationError("base_version 必须为整数（提交所基于的当前版本号）")
        current = len(self._parcels[parcel_id]["versions"])
        if base_version != current:
            self._audit(actor, "change_blocked", parcel_id, {
                "change_type": change_type,
                "reason_code": "VERSION_CONFLICT",
                "reason": f"提交基于版本 {base_version}，但宗地当前版本已是 {current}",
                "base_version": base_version,
                "current_version": current,
            })
            raise Conflict(
                f"存在并发修订：提交基于版本 {base_version}，但宗地当前版本已是 {current}，请刷新后重试",
                code="VERSION_CONFLICT",
                details={"base_version": base_version, "current_version": current,
                         "change_type": change_type},
            )

    def _commit(self, *, actor: dict, parcel_id: str, change_type: str,
                payload: dict, occurred_at, evidence) -> dict:
        """在锁内完成版本组装、落盘、复核入队与审计。调用方须已完成并发与内容校验。"""
        versions = self._parcels.get(parcel_id, {}).get("versions", [])
        prev = versions[-1] if versions else None
        occurred = _norm_ts(occurred_at)
        if prev is not None and parse_ts(occurred) < parse_ts(versions[0]["occurred_at"]):
            raise ValidationError("业务发生时间早于初始登记时间，请核对补录日期")
        evidence = self._validate_evidence(
            evidence, change_type, prev["snapshot"] if prev else None)
        recorded = _now_iso()
        snapshot = self._build_snapshot(
            prev["snapshot"] if prev else None, change_type, payload,
            parcel_id=parcel_id, version_no=len(versions) + 1,
            actor=actor, recorded_at=recorded)
        is_backfill = prev is not None and parse_ts(occurred) < max(
            parse_ts(v["occurred_at"]) for v in versions)
        version = {
            "parcel_id": parcel_id,
            "version_no": len(versions) + 1,
            "change_id": _new_id("C"),
            "change_type": change_type,
            "occurred_at": occurred,
            "recorded_at": recorded,
            "actor": actor["actor_id"],
            "payload": payload,
            "evidence": evidence,
            "is_backfill": is_backfill,
            "summary": self._summarize(change_type, payload, prev["snapshot"] if prev else None),
            "snapshot": snapshot,
            "prev_hash": prev["hash"] if prev else GENESIS_HASH,
        }
        version["hash"] = _version_hash(version)
        self.store.append_event(version)
        self._apply_version(version, verify=False)
        review_id = self._enqueue_review(version)
        self._audit(actor, "change_committed", parcel_id, {
            "change_type": change_type,
            "version_no": version["version_no"],
            "change_id": version["change_id"],
            "is_backfill": is_backfill,
            "review_id": review_id,
        })
        return version

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate_payload(self, change_type: str, payload: dict, prev_snapshot) -> None:
        if change_type == "register":
            parcel_code = _require_text(payload, "parcel_code", "宗地编号")
            for existing in self._parcels.values():
                if existing["versions"][-1]["snapshot"]["parcel_code"] == parcel_code:
                    raise Conflict(f"宗地编号已存在：{parcel_code}", code="PARCEL_CODE_EXISTS")
            _require_text(payload, "location", "坐落")
            payload["area_mu"] = _require_area(payload.get("area_mu"))
            boundary = payload.get("boundary")
            if not isinstance(boundary, list) or not boundary:
                raise ValidationError("boundary 需为非空数组（四至描述或界址点）")
            members = payload.get("members")
            if not isinstance(members, list) or not members:
                raise ValidationError("members 需为非空数组（承包共有人）")
            id_numbers = []
            for member in members:
                if not isinstance(member, dict):
                    raise ValidationError("成员信息需为对象")
                _require_text(member, "name", "成员姓名")
                id_numbers.append(_require_text(member, "id_number", "成员证件号"))
            if len(set(id_numbers)) != len(id_numbers):
                raise ValidationError("成员证件号重复")
            rights = payload.get("rights") or []
            if not isinstance(rights, list):
                raise ValidationError("rights 需为数组")
            for right in rights:
                self._validate_right(right)
            return

        if change_type == "member_change":
            add = payload.get("add") or []
            remove = payload.get("remove") or []
            if not add and not remove:
                raise ValidationError("成员变更内容为空：需包含 add 或 remove")
            existing = {m["member_id"] for m in prev_snapshot["members"]}
            for mid in remove:
                if mid not in existing:
                    raise ValidationError(f"要退出的成员不存在：{mid}",
                                          details={"unknown_members": [mid]})
            added_id_numbers = []
            for member in add:
                if not isinstance(member, dict):
                    raise ValidationError("新增成员信息需为对象")
                _require_text(member, "name", "成员姓名")
                added_id_numbers.append(_require_text(member, "id_number", "成员证件号"))
            if len(set(added_id_numbers)) != len(added_id_numbers):
                raise ValidationError("新增成员证件号重复")
            remaining_id_numbers = {m["id_number"] for m in prev_snapshot["members"]
                                    if m["member_id"] not in set(remove)}
            duplicated = [n for n in added_id_numbers if n in remaining_id_numbers]
            if duplicated:
                raise ValidationError("新增成员证件号已在册：" + "、".join(duplicated))
            if not (existing - set(remove)) and not add:
                raise ValidationError("承包共有人不能为空")
            return

        if change_type == "boundary_adjust":
            boundary = payload.get("boundary")
            if not isinstance(boundary, list) or not boundary:
                raise ValidationError("boundary 需为非空数组（调整后的四至）")
            payload["area_mu"] = _require_area(payload.get("area_mu"))
            return

        if change_type == "partial_transfer":
            _require_text(payload, "right_type", "权利类型")
            _require_text(payload, "holder_name", "受让方名称")
            scope = _require_area(payload.get("scope_area_mu"), "scope_area_mu")
            if scope > prev_snapshot["area_mu"]:
                raise ValidationError(
                    f"流转面积 {scope} 亩超过宗地面积 {prev_snapshot['area_mu']} 亩",
                    details={"scope_area_mu": scope, "area_mu": prev_snapshot["area_mu"]})
            start = parse_ts(payload.get("term_start"), "term_start")
            end = parse_ts(payload.get("term_end"), "term_end")
            if end <= start:
                raise ValidationError("流转期限无效：term_end 必须晚于 term_start")
            return

        raise ValidationError(f"不支持的变更类型：{change_type!r}")

    def _validate_right(self, right: dict) -> None:
        if not isinstance(right, dict):
            raise ValidationError("权利信息需为对象")
        _require_text(right, "right_type", "权利类型")
        _require_text(right, "holder_name", "权利人名称")
        start = right.get("term_start")
        end = right.get("term_end")
        if start and end and parse_ts(end, "term_end") <= parse_ts(start, "term_start"):
            raise ValidationError("权利期限无效：term_end 必须晚于 term_start")

    def _validate_evidence(self, evidence, change_type: str, prev_snapshot) -> dict:
        if not isinstance(evidence, dict):
            raise ValidationError("缺少变更依据 evidence（documents / confirmed_by / note）")
        documents = evidence.get("documents") or []
        note = (evidence.get("note") or "").strip()
        confirmed_by = list(evidence.get("confirmed_by") or [])
        if not documents and not note:
            raise ValidationError("变更依据不能为空：至少提供一份 documents 或 note")
        docs = []
        for doc in documents:
            if not isinstance(doc, dict) or not doc.get("type"):
                raise ValidationError("变更依据 documents 需包含 type 与 ref")
            docs.append({"type": str(doc["type"]), "ref": str(doc.get("ref", ""))})
        normalized = {"documents": docs, "confirmed_by": confirmed_by, "note": note}
        if change_type == "boundary_adjust":
            member_ids = [m["member_id"] for m in prev_snapshot["members"]]
            missing = [mid for mid in member_ids if mid not in confirmed_by]
            if missing:
                raise ValidationError(
                    "边界调整需全体共有人确认，缺少确认的成员：" + "、".join(missing),
                    code="CONFIRMATION_INCOMPLETE",
                    details={"missing_confirmations": missing},
                )
        return normalized

    # ------------------------------------------------------------------
    # 快照构建
    # ------------------------------------------------------------------
    def _make_member(self, parcel_id: str, data: dict) -> dict:
        id_number = data["id_number"].strip()
        return {
            "member_id": _member_id(parcel_id, id_number),
            "name": data["name"].strip(),
            "id_number": id_number,
            "phone": (data.get("phone") or "").strip(),
            "share": data.get("share"),
        }

    def _make_right(self, parcel_id: str, data: dict, default_scope) -> dict:
        right = {
            "right_type": data["right_type"].strip(),
            "holder_name": data["holder_name"].strip(),
            "holder_ref": data.get("holder_ref"),
            "scope_area_mu": float(data.get("scope_area_mu") or default_scope),
            "term_start": _norm_ts(data["term_start"], "term_start") if data.get("term_start") else None,
            "term_end": _norm_ts(data["term_end"], "term_end") if data.get("term_end") else None,
            "status": "active",
            "note": data.get("note", ""),
        }
        right["right_id"] = _right_id(parcel_id, right)
        return right

    def _build_snapshot(self, prev, change_type: str, payload: dict, *,
                        parcel_id: str, version_no: int, actor: dict,
                        recorded_at: str) -> dict:
        """由上一版快照与变更内容推导新快照。

        同一组版本按业务发生时间重放时产出相同快照（成员与权利 ID 确定），
        因此既用于提交，也用于"当时有效版本"重建。
        """
        if change_type == "register":
            return {
                "parcel_code": payload["parcel_code"].strip(),
                "location": payload["location"].strip(),
                "area_mu": payload["area_mu"],
                "boundary": list(payload["boundary"]),
                "members": [self._make_member(parcel_id, m) for m in payload["members"]],
                "rights": [self._make_right(parcel_id, r, payload["area_mu"])
                           for r in payload.get("rights") or []],
                "certificates": [],
            }

        snapshot = copy.deepcopy(prev)
        if change_type == "member_change":
            remove_ids = set(payload.get("remove") or [])
            snapshot["members"] = [m for m in snapshot["members"]
                                   if m["member_id"] not in remove_ids]
            for m in payload.get("add") or []:
                snapshot["members"].append(self._make_member(parcel_id, m))
        elif change_type == "boundary_adjust":
            snapshot["boundary"] = list(payload["boundary"])
            snapshot["area_mu"] = payload["area_mu"]
        elif change_type == "partial_transfer":
            snapshot["rights"].append(
                self._make_right(parcel_id, payload, payload["scope_area_mu"]))
        elif change_type == "certificate_issue":
            for cert in snapshot["certificates"]:
                if cert["status"] == "valid":
                    cert["status"] = "superseded"
            snapshot["certificates"].append({
                "cert_id": payload["cert_id"],
                "cert_no": payload["cert_no"],
                "holder_name": payload.get("holder_name", ""),
                "status": "valid",
                "issued_version": version_no,
                "issued_at": recorded_at,
                "issued_by": actor["actor_id"],
            })
        elif change_type == "certificate_revoke":
            for cert in snapshot["certificates"]:
                if cert["cert_id"] == payload["cert_id"]:
                    cert["status"] = "revoked"
                    cert["revoked_version"] = version_no
                    cert["revoke_reason"] = payload.get("reason", "")
                    break
        return snapshot

    def _summarize(self, change_type: str, payload: dict, prev_snapshot) -> str:
        if change_type == "register":
            return f"初始登记：宗地 {payload['parcel_code']}"
        if change_type == "member_change":
            return f"成员变更：新增 {len(payload.get('add') or [])} 人，退出 {len(payload.get('remove') or [])} 人"
        if change_type == "boundary_adjust":
            old = prev_snapshot["area_mu"] if prev_snapshot else None
            return f"边界调整：面积 {old} → {payload['area_mu']} 亩"
        if change_type == "partial_transfer":
            return (f"局部流转：{payload['scope_area_mu']} 亩{payload['right_type']}"
                    f"流转予{payload['holder_name']}，期限 {payload['term_start']} ~ {payload['term_end']}")
        if change_type == "certificate_issue":
            return f"签发证照 {payload['cert_no']}"
        if change_type == "certificate_revoke":
            return f"撤回证照 {payload['cert_no']}：{payload.get('reason', '')}"
        return change_type

    # ------------------------------------------------------------------
    # 复核队列
    # ------------------------------------------------------------------
    def _enqueue_review(self, version: dict) -> str:
        record = {
            "type": "enqueue",
            "review_id": _new_id("Q"),
            "parcel_id": version["parcel_id"],
            "version_no": version["version_no"],
            "change_id": version["change_id"],
            "change_type": version["change_type"],
            "enqueued_at": _now_iso(),
        }
        self.store.append_review(record)
        self._apply_review_record(record)
        return record["review_id"]

    def list_reviews(self, status: str | None = "pending") -> list[dict]:
        with self._lock:
            if status in (None, "all"):
                items = self._reviews
            else:
                items = [r for r in self._reviews if r["status"] == status]
            return copy.deepcopy(items)

    def decide_review(self, *, actor: dict, review_id: str, decision: str, note="") -> dict:
        with self._lock:
            item = self._review_index.get(review_id)
            if item is None:
                raise NotFound(f"复核任务不存在：{review_id}", code="REVIEW_NOT_FOUND")
            if item["status"] != "pending":
                raise Conflict(
                    f"复核任务 {review_id} 已是 {item['status']} 状态，不能重复复核",
                    code="REVIEW_NOT_PENDING",
                    details={"status": item["status"]},
                )
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 只能是 approved 或 rejected")
            record = {
                "type": "decision",
                "review_id": review_id,
                "decision": decision,
                "decided_by": actor["actor_id"],
                "decided_at": _now_iso(),
                "note": note or "",
            }
            self.store.append_review(record)
            self._apply_review_record(record)
            self._audit(actor, "review_decided", item["parcel_id"], {
                "review_id": review_id,
                "version_no": item["version_no"],
                "decision": decision,
                "note": note or "",
            })
            return copy.deepcopy(self._review_index[review_id])

    # ------------------------------------------------------------------
    # 证照核验
    # ------------------------------------------------------------------
    def verify_certificate(self, *, actor: dict, cert_no=None, cert_id=None) -> dict:
        """授权机构核验：返回证照状态、签发版本与当前版本，全程留痕。"""
        with self._lock:
            cert = None
            if cert_id:
                cert = self._certs.get(cert_id)
            elif cert_no:
                cid = self._cert_nos.get(cert_no)
                cert = self._certs.get(cid) if cid else None
            if cert is None:
                self._audit(actor, "verify", None, {
                    "cert_no": cert_no, "cert_id": cert_id, "result": "unknown"})
                return {
                    "status": "unknown",
                    "cert_no": cert_no,
                    "cert_id": cert_id,
                    "checked_at": _now_iso(),
                }
            parcel = self._parcels[cert["parcel_id"]]
            current = len(parcel["versions"])
            result = {
                "status": cert["status"],
                "cert_id": cert["cert_id"],
                "cert_no": cert["cert_no"],
                "parcel_id": cert["parcel_id"],
                "parcel_code": cert["parcel_code"],
                "holder_name": cert.get("holder_name", ""),
                "issued_version": cert["issued_version"],
                "current_version": current,
                "is_latest_version": cert["issued_version"] == current,
                "issued_at": cert["issued_at"],
                "checked_at": _now_iso(),
            }
            if cert["status"] == "revoked":
                result["revoke_reason"] = cert.get("revoke_reason", "")
                result["revoked_version"] = cert.get("revoked_version")
            self._audit(actor, "verify", cert["parcel_id"], {
                "cert_no": cert["cert_no"],
                "result": cert["status"],
                "issued_version": cert["issued_version"],
                "current_version": current,
            })
            return result

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _require_parcel(self, parcel_id: str) -> dict:
        parcel = self._parcels.get(parcel_id)
        if parcel is None:
            raise NotFound(f"宗地不存在：{parcel_id}", code="PARCEL_NOT_FOUND")
        return parcel

    def get_parcel(self, parcel_id: str) -> dict:
        with self._lock:
            parcel = self._require_parcel(parcel_id)
            versions = parcel["versions"]
            latest = versions[-1]
            snap = latest["snapshot"]
            return copy.deepcopy({
                "parcel_id": parcel_id,
                "parcel_code": snap["parcel_code"],
                "location": snap["location"],
                "current_version": latest["version_no"],
                "state": {
                    "area_mu": snap["area_mu"],
                    "boundary": snap["boundary"],
                    "members": snap["members"],
                    "rights": snap["rights"],
                    "certificates": snap["certificates"],
                },
                "registered_at": versions[0]["recorded_at"],
                "updated_at": latest["recorded_at"],
            })

    def list_parcels(self) -> list[dict]:
        with self._lock:
            return [{
                "parcel_id": parcel_id,
                "parcel_code": parcel["versions"][-1]["snapshot"]["parcel_code"],
                "location": parcel["versions"][-1]["snapshot"]["location"],
                "current_version": len(parcel["versions"]),
            } for parcel_id, parcel in self._parcels.items()]

    def list_versions(self, parcel_id: str) -> list[dict]:
        with self._lock:
            parcel = self._require_parcel(parcel_id)
            return [self._version_slim(v) for v in parcel["versions"]]

    def get_version(self, parcel_id: str, version_no: int) -> dict:
        with self._lock:
            parcel = self._require_parcel(parcel_id)
            for version in parcel["versions"]:
                if version["version_no"] == version_no:
                    return self._version_view(version)
            raise NotFound(f"版本不存在：{version_no}", code="VERSION_NOT_FOUND")

    def version_as_of(self, parcel_id: str, at, basis: str = "occurred") -> dict:
        """查询某时刻的有效版本。

        basis=occurred：按业务发生时间重放，重建该时点实际生效的状态
        （迟到补录落在其真实发生位置，不会把后来的变更提前带入）；
        basis=recorded：按记录时间，返回该时点系统已知的当时有效版本。
        """
        with self._lock:
            parcel = self._require_parcel(parcel_id)
            moment = parse_ts(at, "at")
            versions = parcel["versions"]
            if basis == "recorded":
                candidates = [v for v in versions if parse_ts(v["recorded_at"]) <= moment]
                if not candidates:
                    raise NotFound("该时间点之前没有生效版本", code="NO_VERSION_AT_TIME")
                chosen = max(candidates, key=lambda v: v["version_no"])
                view = self._version_view(chosen)
                view["basis"] = "recorded"
                view["reconstructed"] = False
                return view
            if basis != "occurred":
                raise ValidationError("basis 只能是 occurred 或 recorded")
            ordered = sorted(versions,
                             key=lambda v: (parse_ts(v["occurred_at"]), v["version_no"]))
            applicable = [v for v in ordered if parse_ts(v["occurred_at"]) <= moment]
            if not applicable:
                raise NotFound("该时间点之前没有生效版本", code="NO_VERSION_AT_TIME")
            snapshot = None
            for version in applicable:
                snapshot = self._build_snapshot(
                    snapshot, version["change_type"], version["payload"],
                    parcel_id=parcel_id, version_no=version["version_no"],
                    actor={"actor_id": version["actor"]},
                    recorded_at=version["recorded_at"])
            return {
                "parcel_id": parcel_id,
                "basis": "occurred",
                "as_of": moment.isoformat(timespec="microseconds"),
                "reconstructed": True,
                "snapshot": snapshot,
                "based_on_versions": [v["version_no"] for v in applicable],
                "latest_version_no": applicable[-1]["version_no"],
                "current_version": len(versions),
            }

    def parcel_audit(self, parcel_id: str) -> list[dict]:
        with self._lock:
            self._require_parcel(parcel_id)
            return copy.deepcopy([e for e in self._audit_log if e["parcel_id"] == parcel_id])

    def global_audit(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._audit_log)

    def verify_integrity(self, parcel_id: str) -> dict:
        """重算版本哈希链，证明时间线未被覆盖或篡改。"""
        with self._lock:
            parcel = self._require_parcel(parcel_id)
            prev = GENESIS_HASH
            for version in parcel["versions"]:
                if version["prev_hash"] != prev or _version_hash(version) != version["hash"]:
                    return {
                        "parcel_id": parcel_id,
                        "ok": False,
                        "broken_at": version["version_no"],
                        "versions_checked": version["version_no"],
                    }
                prev = version["hash"]
            return {
                "parcel_id": parcel_id,
                "ok": True,
                "broken_at": None,
                "versions_checked": len(parcel["versions"]),
            }

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def _version_view(self, version: dict) -> dict:
        view = copy.deepcopy(version)
        review = self._review_by_change.get(version["change_id"])
        view["review_status"] = review["status"] if review else "pending"
        view["review_id"] = review["review_id"] if review else None
        return view

    def _version_slim(self, version: dict) -> dict:
        review = self._review_by_change.get(version["change_id"])
        return {
            "parcel_id": version["parcel_id"],
            "version_no": version["version_no"],
            "change_id": version["change_id"],
            "change_type": version["change_type"],
            "occurred_at": version["occurred_at"],
            "recorded_at": version["recorded_at"],
            "actor": version["actor"],
            "is_backfill": version["is_backfill"],
            "summary": version["summary"],
            "hash": version["hash"],
            "review_status": review["status"] if review else "pending",
            "review_id": review["review_id"] if review else None,
        }
