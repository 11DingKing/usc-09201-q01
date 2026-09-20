"""HTTP API：版本账、复核队列、证照签发/撤回、核验与审计。

除 ``/health`` 外所有接口都需要不记名令牌；写操作按角色鉴权，返回体中的
敏感身份信息按角色脱敏。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import auth
from .ledger import verify_certificate
from .store import (
    APPROVED,
    PENDING,
    REJECTED,
    ConflictError,
    NotFoundError,
    Store,
    StoreError,
)

# 角色 -> 允许的操作矩阵
PERMISSIONS = {
    "submit_change": {auth.OPERATOR},
    "list_changes": {auth.OPERATOR, auth.REVIEWER},
    "decide_change": {auth.REVIEWER},
    "issue_cert": {auth.REGISTRAR},
    "withdraw_cert": {auth.REGISTRAR},
    "verify": {auth.INSTITUTION, auth.REGISTRAR},
    "read_audit": {auth.REVIEWER, auth.REGISTRAR},
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def make_handler(store: Store, tokens: dict[str, dict[str, str]]) -> type:
    """生成绑定了存储与令牌表的 Handler 类。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "ForestLedger/0.1"

        # ── 通用工具 ─────────────────────────────────────────────────────

        def _send(self, status: int, body: Any) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_error(self, err: ApiError | StoreError) -> None:
            body: dict[str, Any] = {"error": err.code, "message": err.message}
            if isinstance(err, (ApiError, StoreError)) and getattr(err, "extra", None):
                for key, value in err.extra.items():
                    body[key] = value
            self._send(err.status, body)

        def _principal(self) -> dict[str, str]:
            principal = auth.principal_from_headers(tokens, self.headers)
            if principal is None:
                raise ApiError(401, "unauthorized", "缺少或无效的不记名令牌")
            return principal

        def _require(self, action: str) -> dict[str, str]:
            principal = self._principal()
            if principal["role"] not in PERMISSIONS[action]:
                raise ApiError(
                    403,
                    "forbidden",
                    f"角色 {principal['role']} 无权执行 {action}",
                )
            return principal

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ApiError(400, "bad_json", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "bad_body", "请求体必须是 JSON 对象")
            return data

        def _masked(self, data: Any) -> Any:
            return auth.mask_for_role(data, getattr(self, "_role", None))

        # ── 路由 ─────────────────────────────────────────────────────────

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = unquote(parsed.path).rstrip("/") or "/"
                query = parse_qs(parsed.query)
                if path == "/health":
                    self._send(200, {"status": "ok"})
                    return
                principal = self._principal()
                self._role = principal["role"]
                self._route_get(path, query, principal)
            except (ApiError, StoreError) as err:
                self._send_error(err)

        def do_POST(self) -> None:  # noqa: N802
            try:
                path = unquote(urlparse(self.path).path).rstrip("/") or "/"
                principal = self._principal()
                self._role = principal["role"]
                self._route_post(path, principal)
            except (ApiError, StoreError) as err:
                self._send_error(err)

        def _route_get(
            self,
            path: str,
            query: dict[str, list[str]],
            principal: dict[str, str],
        ) -> None:
            parts = [p for p in path.split("/") if p]

            if path == "/api/v1/parcels":
                self._send(200, {"parcels": store.list_parcels()})
                return

            if len(parts) == 4 and parts[:3] == ["api", "v1", "parcels"]:
                self._send(
                    200, self._masked(store.current_snapshot(parts[3]))
                )
                return

            if len(parts) == 5 and parts[:3] == ["api", "v1", "parcels"] \
                    and parts[4] == "versions":
                self._send(
                    200,
                    {"timeline": self._masked(store.timeline(parts[3]))},
                )
                return

            if len(parts) == 6 and parts[:3] == ["api", "v1", "parcels"] \
                    and parts[4] == "versions":
                self._send(
                    200,
                    self._masked(store.version_snapshot(parts[3], int(parts[5]))),
                )
                return

            if path == "/api/v1/changes":
                self._require("list_changes")
                status = query.get("status", [None])[0]
                self._send(200, {"changes": store.list_changes(status)})
                return

            if len(parts) == 4 and parts[:3] == ["api", "v1", "changes"]:
                self._require("list_changes")
                self._send(200, self._masked(store.get_change(parts[3])))
                return

            if len(parts) == 5 and parts[:3] == ["api", "v1", "parcels"] \
                    and parts[4] == "certs":
                self._send(
                    200, {"certificates": store.list_certificates(parts[3])}
                )
                return

            if len(parts) == 4 and parts[:3] == ["api", "v1", "certificates"]:
                cert = store.get_certificate(parts[3])
                cert["snapshot"] = self._masked(cert["snapshot"])
                self._send(200, cert)
                return

            if path == "/api/v1/audit":
                self._require("read_audit")
                parcel_id = query.get("parcel_id", [None])[0]
                self._send(200, {"entries": store.list_audit(parcel_id)})
                return

            if path == "/api/v1/audit/chain":
                self._require("read_audit")
                self._send(200, store.verify_chain())
                return

            raise ApiError(404, "not_found", f"路径不存在：{path}")

        def _route_post(
            self, path: str, principal: dict[str, str]
        ) -> None:
            parts = [p for p in path.split("/") if p]
            body = self._body()

            if path == "/api/v1/changes":
                actor = self._require("submit_change")
                try:
                    parcel_id = body["parcel_id"]
                    kind = body["kind"]
                    payload = body["payload"]
                    occurred_at = body["occurred_at"]
                    base_version = int(body.get("base_version", 0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ApiError(
                        400, "bad_request",
                        "需要 parcel_id/kind/payload/occurred_at/base_version",
                    ) from exc
                if not isinstance(payload, dict):
                    raise ApiError(400, "bad_request", "payload 必须是对象")
                change = store.submit_change(
                    actor, parcel_id, kind, payload, occurred_at, base_version
                )
                self._send(201, change)
                return

            if len(parts) == 5 and parts[:3] == ["api", "v1", "changes"] \
                    and parts[4] == "decision":
                actor = self._require("decide_change")
                decision_in = body.get("decision")
                decision = {
                    "approve": APPROVED,
                    "reject": REJECTED,
                }.get(decision_in)
                if decision is None:
                    raise ApiError(
                        400, "bad_decision", "decision 必须为 approve 或 reject"
                    )
                change = store.decide_change(
                    actor, parts[3], decision, body.get("note")
                )
                self._send(200, change)
                return

            if path == "/api/v1/certificates":
                actor = self._require("issue_cert")
                parcel_id = body.get("parcel_id")
                if not parcel_id:
                    raise ApiError(400, "parcel_id_required", "需要 parcel_id")
                cert = store.issue_certificate(
                    actor, parcel_id, body.get("cert_no")
                )
                cert["snapshot"] = self._masked(cert["snapshot"])
                self._send(201, cert)
                return

            if len(parts) == 5 and parts[:3] == ["api", "v1", "certificates"] \
                    and parts[4] == "withdraw":
                actor = self._require("withdraw_cert")
                cert = store.withdraw_certificate(
                    actor, parts[3], body.get("reason", "")
                )
                cert["snapshot"] = self._masked(cert["snapshot"])
                self._send(200, cert)
                return

            if path == "/api/v1/verify":
                actor = self._require("verify")
                cert_no = body.get("cert_no")
                if not cert_no:
                    raise ApiError(400, "cert_no_required", "需要 cert_no")
                cert, current, newer_cert, today = store.verification_materials(
                    cert_no, body.get("today")
                )
                result = verify_certificate(
                    cert, current, cert["snapshot"], newer_cert, today
                )
                result["verified_by"] = actor["id"]
                result["verifier_role"] = actor["role"]
                result = self._masked(result)
                # 核验本身也入账（含阻断结果），保证完整审计
                store.append_audit(
                    "cert_verified",
                    actor,
                    cert["parcel_id"],
                    {
                        "cert_no": cert_no,
                        "valid": result["valid"],
                        "block_codes": [b["code"] for b in result["block_reasons"]],
                    },
                )
                self._send(200, result)
                return

            raise ApiError(404, "not_found", f"路径不存在：{path}")

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            return

    return Handler
