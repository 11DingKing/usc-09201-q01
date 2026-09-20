"""SQLite 存储：只追加的版本账、复核队列、证照与哈希链审计。

不可变性由数据库触发器强制执行：``versions`` 与 ``audit_log`` 上禁止
UPDATE/DELETE。每个生效版本在入账时即物化为冻结快照，历史版本永远不会被
迟到补录重算。复核申请持久化在 ``changes`` 表中，服务重启后未完成复核仍在
原队列。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ledger import (
    DomainError,
    CHANGE_KINDS,
    fold,
    freeze_snapshot,
    validate_candidate,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS parcels (
    parcel_id  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id    TEXT NOT NULL,
    version      INTEGER NOT NULL,
    event_id     TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    occurred_ts  REAL NOT NULL,
    payload      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    recorded_by  TEXT NOT NULL,
    backfill     INTEGER NOT NULL DEFAULT 0,
    change_id    TEXT NOT NULL,
    snapshot     TEXT NOT NULL,
    UNIQUE(parcel_id, version)
);
CREATE INDEX IF NOT EXISTS idx_versions_parcel ON versions(parcel_id, version);

CREATE TABLE IF NOT EXISTS changes (
    change_id      TEXT PRIMARY KEY,
    parcel_id      TEXT NOT NULL,
    base_version   INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    payload        TEXT NOT NULL,
    submitted_at   TEXT NOT NULL,
    submitted_by   TEXT NOT NULL,
    status         TEXT NOT NULL,
    decided_at     TEXT,
    decided_by     TEXT,
    decision_note  TEXT,
    conflict_with  INTEGER,
    event_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_status ON changes(status, submitted_at);

CREATE TABLE IF NOT EXISTS certificates (
    cert_no           TEXT PRIMARY KEY,
    parcel_id         TEXT NOT NULL,
    version           INTEGER NOT NULL,
    issued_at         TEXT NOT NULL,
    issued_by         TEXT NOT NULL,
    status            TEXT NOT NULL,
    superseded_by     TEXT,
    withdrawn_at      TEXT,
    withdrawal_reason TEXT,
    withdrawn_by      TEXT,
    snapshot          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_certs_parcel ON certificates(parcel_id, issued_at);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor_id   TEXT,
    actor_role TEXT,
    action     TEXT NOT NULL,
    parcel_id  TEXT,
    detail     TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS versions_no_update
BEFORE UPDATE ON versions
BEGIN SELECT RAISE(ABORT, 'versions 表不可修改'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete
BEFORE DELETE ON versions
BEGIN SELECT RAISE(ABORT, 'versions 表不可删除'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log 表不可修改'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log 表不可删除'); END;
"""

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
CONFLICTED = "conflicted"


class StoreError(Exception):
    """存储/接口层错误基类，携带机器可读码与 HTTP 状态。"""

    status = 400

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class NotFoundError(StoreError):
    status = 404


class ConflictError(StoreError):
    status = 409


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    """解析 ISO 时间；仅给日期时按 UTC 零点处理。"""

    text = value.strip()
    if len(text) == 10:
        text = text + "T00:00:00+00:00"
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "seq": row["seq"],
        "version": row["version"],
        "kind": row["kind"],
        "occurred_at": row["occurred_at"],
        "occurred_ts": row["occurred_ts"],
        "recorded_at": row["recorded_at"],
        "recorded_by": row["recorded_by"],
        "backfill": bool(row["backfill"]),
        "change_id": row["change_id"],
        "payload": json.loads(row["payload"]),
    }


class Store:
    """封装全部持久化操作；写操作在锁内串行执行。"""

    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ── 内部工具 ─────────────────────────────────────────────────────────

    def _row(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def _audit(
        self,
        action: str,
        actor: dict[str, str] | None,
        parcel_id: str | None,
        detail: dict[str, Any],
    ) -> None:
        """追加一条哈希链审计记录（须在写事务内调用）。"""

        head = self._row("SELECT v FROM kv WHERE k='audit_head'")
        prev_hash = head["v"] if head else "GENESIS"
        at = now_iso()
        body = {
            "at": at,
            "actor_id": actor.get("id") if actor else None,
            "actor_role": actor.get("role") if actor else None,
            "action": action,
            "parcel_id": parcel_id,
            "detail": detail,
            "prev_hash": prev_hash,
        }
        entry_hash = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self._conn.execute(
            """INSERT INTO audit_log
               (at, actor_id, actor_role, action, parcel_id, detail,
                prev_hash, entry_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                at,
                body["actor_id"],
                body["actor_role"],
                action,
                parcel_id,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                prev_hash,
                entry_hash,
            ),
        )
        self._conn.execute(
            "INSERT INTO kv(k, v) VALUES('audit_head', ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (entry_hash,),
        )

    def _require_parcel(self, parcel_id: str) -> sqlite3.Row:
        row = self._row("SELECT * FROM parcels WHERE parcel_id=?", (parcel_id,))
        if row is None:
            raise NotFoundError("parcel_not_found", f"宗地不存在：{parcel_id}")
        return row

    def _events(self, parcel_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM versions WHERE parcel_id=? ORDER BY version", (parcel_id,)
        )
        return [_event_from_row(row) for row in rows]

    def _effective_version(self, parcel_id: str) -> int:
        row = self._row(
            "SELECT MAX(version) AS v FROM versions WHERE parcel_id=?", (parcel_id,)
        )
        return int(row["v"] or 0)

    def _last_occurred_ts(self, parcel_id: str) -> float | None:
        row = self._row(
            "SELECT MAX(occurred_ts) AS t FROM versions WHERE parcel_id=?",
            (parcel_id,),
        )
        return row["t"] if row and row["t"] is not None else None

    # ── 宗地 ─────────────────────────────────────────────────────────────

    def parcel_exists(self, parcel_id: str) -> bool:
        return self._row("SELECT 1 FROM parcels WHERE parcel_id=?", (parcel_id,)) is not None

    def list_parcels(self) -> list[dict[str, Any]]:
        rows = self._all(
            """SELECT p.*,
                      (SELECT MAX(version) FROM versions v
                       WHERE v.parcel_id = p.parcel_id) AS effective_version
               FROM parcels p ORDER BY p.created_at"""
        )
        return [
            {
                "parcel_id": r["parcel_id"],
                "created_at": r["created_at"],
                "created_by": r["created_by"],
                "effective_version": r["effective_version"] or 0,
            }
            for r in rows
        ]

    def current_snapshot(self, parcel_id: str) -> dict[str, Any]:
        self._require_parcel(parcel_id)
        row = self._row(
            """SELECT snapshot FROM versions
               WHERE parcel_id=? ORDER BY version DESC LIMIT 1""",
            (parcel_id,),
        )
        if row is None:
            # 宗地已创建但首笔登记尚未复核通过
            return {"parcel_id": parcel_id, "effective_version": 0, "registered": False}
        snapshot = json.loads(row["snapshot"])
        snapshot["parcel_id"] = parcel_id
        snapshot["registered"] = True
        return snapshot

    def version_snapshot(self, parcel_id: str, version: int) -> dict[str, Any]:
        self._require_parcel(parcel_id)
        row = self._row(
            "SELECT * FROM versions WHERE parcel_id=? AND version=?",
            (parcel_id, version),
        )
        if row is None:
            raise NotFoundError(
                "version_not_found", f"版本不存在：{parcel_id} v{version}"
            )
        snapshot = json.loads(row["snapshot"])
        snapshot["parcel_id"] = parcel_id
        return snapshot

    def timeline(self, parcel_id: str) -> list[dict[str, Any]]:
        self._require_parcel(parcel_id)
        rows = self._all(
            "SELECT * FROM versions WHERE parcel_id=? ORDER BY version", (parcel_id,)
        )
        return [
            {
                "version": r["version"],
                "event_id": r["event_id"],
                "kind": r["kind"],
                "occurred_at": r["occurred_at"],
                "recorded_at": r["recorded_at"],
                "recorded_by": r["recorded_by"],
                "backfill": bool(r["backfill"]),
                "change_id": r["change_id"],
                "snapshot": json.loads(r["snapshot"]),
            }
            for r in rows
        ]

    # ── 变更与复核队列 ───────────────────────────────────────────────────

    def submit_change(
        self,
        actor: dict[str, str],
        parcel_id: str,
        kind: str,
        payload: dict[str, Any],
        occurred_at: str,
        base_version: int,
        change_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in CHANGE_KINDS:
            raise StoreError("unknown_kind", f"未知变更类型：{kind}")
        try:
            occurred_dt = parse_time(occurred_at)
        except ValueError:
            raise StoreError("bad_time", f"无法解析发生时间：{occurred_at}")

        with self._lock, self._conn:  # type: ignore[attr-defined]
            exists = self.parcel_exists(parcel_id)
            if kind == "register":
                if exists:
                    raise ConflictError(
                        "already_registered", "该宗地已存在，不能重复初始登记"
                    )
                self._conn.execute(
                    "INSERT INTO parcels(parcel_id, created_at, created_by) "
                    "VALUES(?, ?, ?)",
                    (parcel_id, now_iso(), actor["id"]),
                )
            else:
                self._require_parcel(parcel_id)

            effective = self._effective_version(parcel_id)
            status = PENDING
            conflict_reason: str | None = None
            pending_change_id: str | None = None
            if base_version != effective:
                # 乐观锁失败：申请仍保留在队列中，但标记为冲突，不能直接复核
                status = CONFLICTED
                conflict_reason = "base_version_conflict"
            elif kind != "register":
                pending = self._row(
                    "SELECT change_id FROM changes WHERE parcel_id=? "
                    "AND status=? LIMIT 1",
                    (parcel_id, PENDING),
                )
                if pending is not None:
                    # 同一宗地已有一件待复核修订，并发修订不得并行排队
                    status = CONFLICTED
                    conflict_reason = "base_version_conflict"
                    pending_change_id = pending["change_id"]

            # 业务规则在提交时先给出即时反馈（冲突件除外，状态可能已被他人改变）
            if status == PENDING:
                candidate = self._candidate(
                    parcel_id, kind, payload, occurred_dt, occurred_at
                )
                try:
                    validate_candidate(self._events(parcel_id), candidate)
                except DomainError as exc:
                    raise StoreError(exc.reason, exc.message, **exc.extra)

            last_ts = self._last_occurred_ts(parcel_id)
            backfill = last_ts is not None and occurred_dt.timestamp() < last_ts
            cid = change_id or f"chg-{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                """INSERT INTO changes(
                       change_id, parcel_id, base_version, kind, occurred_at,
                       payload, submitted_at, submitted_by, status, conflict_with)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    parcel_id,
                    base_version,
                    kind,
                    occurred_dt.isoformat(timespec="seconds"),
                    json.dumps(payload, ensure_ascii=False),
                    now_iso(),
                    actor["id"],
                    status,
                    effective,
                ),
            )
            self._audit(
                "change_submitted",
                actor,
                parcel_id,
                {
                    "change_id": cid,
                    "kind": kind,
                    "base_version": base_version,
                    "effective_version": effective,
                    "pending_change_id": pending_change_id,
                    "occurred_at": occurred_dt.isoformat(timespec="seconds"),
                    "backfill": backfill,
                    "status": status,
                },
            )

        result = self.get_change(cid)
        if status == CONFLICTED:
            # 在事务外抛出，确保冲突草稿已经提交留档
            raise ConflictError(
                conflict_reason or "base_version_conflict",
                "所基于的版本存在并发修订，申请已标记为冲突并留档，请基于最新版本重新提交",
                change_id=cid,
                base_version=base_version,
                current_version=effective,
                **(
                    {"pending_change_id": pending_change_id}
                    if pending_change_id
                    else {}
                ),
            )
        return result

    @staticmethod
    def _candidate(
        parcel_id: str,
        kind: str,
        payload: dict[str, Any],
        occurred_dt: datetime,
        occurred_at: str,
    ) -> dict[str, Any]:
        return {
            "event_id": f"evt-candidate-{uuid.uuid4().hex[:8]}",
            "seq": 10**18,  # 同一业务时点上，候选事件排在已入账事件之后
            "version": None,
            "kind": kind,
            "occurred_at": occurred_dt.isoformat(timespec="seconds"),
            "occurred_ts": occurred_dt.timestamp(),
            "payload": payload,
        }

    def get_change(self, change_id: str) -> dict[str, Any]:
        row = self._row("SELECT * FROM changes WHERE change_id=?", (change_id,))
        if row is None:
            raise NotFoundError("change_not_found", f"变更申请不存在：{change_id}")
        return self._change_dict(row)

    @staticmethod
    def _change_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "change_id": row["change_id"],
            "parcel_id": row["parcel_id"],
            "base_version": row["base_version"],
            "kind": row["kind"],
            "occurred_at": row["occurred_at"],
            "payload": json.loads(row["payload"]),
            "submitted_at": row["submitted_at"],
            "submitted_by": row["submitted_by"],
            "status": row["status"],
            "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "decision_note": row["decision_note"],
            "conflict_with": row["conflict_with"],
            "event_id": row["event_id"],
        }

    def list_changes(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._all(
                "SELECT * FROM changes WHERE status=? ORDER BY submitted_at",
                (status,),
            )
        else:
            rows = self._all(
                "SELECT * FROM changes ORDER BY submitted_at")
        return [self._change_dict(r) for r in rows]

    def decide_change(
        self,
        actor: dict[str, str],
        change_id: str,
        decision: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        if decision not in (APPROVED, REJECTED):
            raise StoreError("bad_decision", "decision 必须为 approve 或 reject")
        decision_conflict: dict[str, Any] | None = None
        with self._lock, self._conn:  # type: ignore[attr-defined]
            row = self._row(
                "SELECT * FROM changes WHERE change_id=?", (change_id,))
            if row is None:
                raise NotFoundError("change_not_found", f"变更申请不存在：{change_id}")
            change = self._change_dict(row)
            if change["status"] != PENDING:
                raise ConflictError(
                    f"change_{change['status']}",
                    f"申请当前状态为 {change['status']}，不能复核",
                    change_id=change_id,
                    status=change["status"],
                )

            if decision == REJECTED:
                self._conn.execute(
                    """UPDATE changes SET status=?, decided_at=?, decided_by=?,
                           decision_note=? WHERE change_id=?""",
                    (REJECTED, now_iso(), actor["id"], note, change_id),
                )
                self._audit(
                    "change_rejected", actor, change["parcel_id"],
                    {"change_id": change_id, "note": note},
                )
                return self.get_change(change_id)

            parcel_id = change["parcel_id"]
            effective = self._effective_version(parcel_id)
            if change["base_version"] != effective:
                # 提交后、复核前被并发修订抢先：标记落库后在事务外报错
                self._conn.execute(
                    """UPDATE changes SET status=?, conflict_with=?
                       WHERE change_id=?""",
                    (CONFLICTED, effective, change_id),
                )
                self._audit(
                    "change_conflicted", actor, parcel_id,
                    {"change_id": change_id, "effective_version": effective},
                )
                decision_conflict = {
                    "change_id": change_id,
                    "base_version": change["base_version"],
                    "current_version": effective,
                }

        if decision_conflict is not None:
            raise ConflictError(
                "concurrent_modification",
                "复核期间宗地已被他人修订，本申请需基于最新版本重新提交",
                **decision_conflict,
            )

        with self._lock, self._conn:  # type: ignore[attr-defined]
            row = self._row(
                "SELECT * FROM changes WHERE change_id=?", (change_id,))
            change = self._change_dict(row)
            parcel_id = change["parcel_id"]
            effective = self._effective_version(parcel_id)
            occurred_dt = parse_time(change["occurred_at"])
            payload = json.loads(row["payload"])
            candidate = self._candidate(
                parcel_id, change["kind"], payload, occurred_dt,
                change["occurred_at"],
            )
            events = self._events(parcel_id)
            try:
                validate_candidate(events, candidate)
            except DomainError as exc:
                raise StoreError(exc.reason, exc.message, **exc.extra)

            new_version = effective + 1
            event_id = f"evt-{uuid.uuid4().hex[:12]}"
            candidate["event_id"] = event_id
            candidate["version"] = new_version
            last_ts = self._last_occurred_ts(parcel_id)
            backfill = last_ts is not None and occurred_dt.timestamp() < last_ts
            recorded_at = now_iso()
            all_events = events + [
                {**candidate, "recorded_at": recorded_at,
                 "recorded_by": actor["id"], "change_id": change_id,
                 "backfill": backfill}
            ]
            snapshot = freeze_snapshot(all_events)

            self._conn.execute(
                """INSERT INTO versions(
                       parcel_id, version, event_id, kind, occurred_at, occurred_ts,
                       payload, recorded_at, recorded_by, backfill, change_id,
                       snapshot)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parcel_id,
                    new_version,
                    event_id,
                    change["kind"],
                    occurred_dt.isoformat(timespec="seconds"),
                    occurred_dt.timestamp(),
                    json.dumps(payload, ensure_ascii=False),
                    recorded_at,
                    actor["id"],
                    1 if backfill else 0,
                    change_id,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.execute(
                """UPDATE changes SET status=?, decided_at=?, decided_by=?,
                       decision_note=?, event_id=? WHERE change_id=?""",
                (APPROVED, recorded_at, actor["id"], note, event_id, change_id),
            )
            self._audit(
                "change_approved",
                actor,
                parcel_id,
                {
                    "change_id": change_id,
                    "version": new_version,
                    "event_id": event_id,
                    "kind": change["kind"],
                    "occurred_at": change["occurred_at"],
                    "backfill": backfill,
                },
            )
            return self.get_change(change_id)

    # ── 证照 ─────────────────────────────────────────────────────────────

    def issue_certificate(
        self,
        actor: dict[str, str],
        parcel_id: str,
        cert_no: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._conn:  # type: ignore[attr-defined]
            self._require_parcel(parcel_id)
            effective = self._effective_version(parcel_id)
            if effective == 0:
                raise ConflictError(
                    "not_registered", "宗地初始登记尚未复核通过，不能签发权证"
                )
            snapshot = self.current_snapshot(parcel_id)
            snapshot.pop("parcel_id", None)
            snapshot.pop("registered", None)
            number = cert_no or f"林证-{uuid.uuid4().hex[:10].upper()}"
            if self._row("SELECT 1 FROM certificates WHERE cert_no=?", (number,)):
                raise ConflictError("cert_no_exists", f"证号已存在：{number}")
            issued_at = now_iso()

            previous = self._all(
                "SELECT cert_no FROM certificates WHERE parcel_id=? AND status='active'",
                (parcel_id,),
            )
            for prev in previous:
                self._conn.execute(
                    "UPDATE certificates SET status='superseded', superseded_by=? "
                    "WHERE cert_no=?",
                    (number, prev["cert_no"]),
                )

            self._conn.execute(
                """INSERT INTO certificates(
                       cert_no, parcel_id, version, issued_at, issued_by, status,
                       snapshot)
                   VALUES(?, ?, ?, ?, ?, 'active', ?)""",
                (
                    number,
                    parcel_id,
                    effective,
                    issued_at,
                    actor["id"],
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._audit(
                "cert_issued",
                actor,
                parcel_id,
                {
                    "cert_no": number,
                    "version": effective,
                    "superseded": [p["cert_no"] for p in previous],
                },
            )
            return self.get_certificate(number)

    def withdraw_certificate(
        self,
        actor: dict[str, str],
        cert_no: str,
        reason: str,
    ) -> dict[str, Any]:
        if not reason:
            raise StoreError("reason_required", "撤回证照必须填写理由")
        with self._lock, self._conn:  # type: ignore[attr-defined]
            row = self._row(
                "SELECT * FROM certificates WHERE cert_no=?", (cert_no,))
            if row is None:
                raise NotFoundError("cert_not_found", f"证照不存在：{cert_no}")
            if row["status"] == "withdrawn":
                raise ConflictError("cert_already_withdrawn", "证照已处于撤回状态")
            withdrawn_at = now_iso()
            self._conn.execute(
                """UPDATE certificates SET status='withdrawn', withdrawn_at=?,
                       withdrawal_reason=?, withdrawn_by=? WHERE cert_no=?""",
                (withdrawn_at, reason, actor["id"], cert_no),
            )
            self._audit(
                "cert_withdrawn",
                actor,
                row["parcel_id"],
                {"cert_no": cert_no, "reason": reason},
            )
            return self.get_certificate(cert_no)

    def get_certificate(self, cert_no: str) -> dict[str, Any]:
        row = self._row(
            "SELECT * FROM certificates WHERE cert_no=?", (cert_no,))
        if row is None:
            raise NotFoundError("cert_not_found", f"证照不存在：{cert_no}")
        return {
            "cert_no": row["cert_no"],
            "parcel_id": row["parcel_id"],
            "version": row["version"],
            "issued_at": row["issued_at"],
            "issued_by": row["issued_by"],
            "status": row["status"],
            "superseded_by": row["superseded_by"],
            "withdrawn_at": row["withdrawn_at"],
            "withdrawal_reason": row["withdrawal_reason"],
            "withdrawn_by": row["withdrawn_by"],
            "snapshot": json.loads(row["snapshot"]),
        }

    def list_certificates(self, parcel_id: str) -> list[dict[str, Any]]:
        self._require_parcel(parcel_id)
        rows = self._all(
            "SELECT cert_no, version, issued_at, issued_by, status, "
            "superseded_by, withdrawn_at, withdrawal_reason, withdrawn_by "
            "FROM certificates WHERE parcel_id=? ORDER BY issued_at",
            (parcel_id,),
        )
        return [dict(r) for r in rows]

    # ── 核验 ─────────────────────────────────────────────────────────────

    def verification_materials(
        self, cert_no: str, today: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, str]:
        """组装核验所需：证照、发证冻结快照、更新的权证、今天日期。"""

        cert = self.get_certificate(cert_no)
        current = self.current_snapshot(cert["parcel_id"])
        newer = self._row(
            "SELECT * FROM certificates WHERE parcel_id=? AND status='active' "
            "AND version > ? ORDER BY version DESC LIMIT 1",
            (cert["parcel_id"], cert["version"]),
        )
        newer_cert = dict(newer) if newer else None
        day = today or datetime.now(timezone.utc).date().isoformat()
        return cert, current, newer_cert, day

    # ── 审计 ─────────────────────────────────────────────────────────────

    def append_audit(
        self,
        action: str,
        actor: dict[str, str] | None,
        parcel_id: str | None,
        detail: dict[str, Any],
    ) -> None:
        """对外暴露的审计追加入口（独立事务）。"""

        with self._lock, self._conn:  # type: ignore[attr-defined]
            self._audit(action, actor, parcel_id, detail)

    def list_audit(self, parcel_id: str | None = None) -> list[dict[str, Any]]:
        if parcel_id:
            rows = self._all(
                "SELECT * FROM audit_log WHERE parcel_id=? ORDER BY audit_id",
                (parcel_id,),
            )
        else:
            rows = self._all("SELECT * FROM audit_log ORDER BY audit_id")
        return [
            {
                "audit_id": r["audit_id"],
                "at": r["at"],
                "actor_id": r["actor_id"],
                "actor_role": r["actor_role"],
                "action": r["action"],
                "parcel_id": r["parcel_id"],
                "detail": json.loads(r["detail"]),
                "prev_hash": r["prev_hash"],
                "entry_hash": r["entry_hash"],
            }
            for r in rows
        ]

    def verify_chain(self) -> dict[str, Any]:
        """重放哈希链，返回链首到链尾的完整性结论。"""

        rows = self._all("SELECT * FROM audit_log ORDER BY audit_id")
        prev_hash = "GENESIS"
        for r in rows:
            if r["prev_hash"] != prev_hash:
                return {
                    "intact": False,
                    "broken_at": r["audit_id"],
                    "reason": "prev_hash 不衔接",
                    "entries": len(rows),
                }
            body = {
                "at": r["at"],
                "actor_id": r["actor_id"],
                "actor_role": r["actor_role"],
                "action": r["action"],
                "parcel_id": r["parcel_id"],
                "detail": json.loads(r["detail"]),
                "prev_hash": prev_hash,
            }
            digest = hashlib.sha256(
                json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if digest != r["entry_hash"]:
                return {
                    "intact": False,
                    "broken_at": r["audit_id"],
                    "reason": "条目哈希不匹配",
                    "entries": len(rows),
                }
            prev_hash = r["entry_hash"]
        head = self._row("SELECT v FROM kv WHERE k='audit_head'")
        chain_head = head["v"] if head else "GENESIS"
        if chain_head != prev_hash:
            return {
                "intact": False,
                "broken_at": None,
                "reason": "链尾哈希与账本头不一致",
                "entries": len(rows),
            }
        return {"intact": True, "entries": len(rows), "head": prev_hash}
