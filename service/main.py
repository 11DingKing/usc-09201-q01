"""林地权属版本服务 HTTP 入口。

运行：python3 -m service.main（环境变量 PORT 指定端口，DATA_DIR 指定数据目录）。
认证：除 /health 外均需 `Authorization: Bearer <token>`，角色见 service/auth.py。
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .auth import Authenticator, load_tokens
from .core import TenureService
from .errors import ApiError, Forbidden, NotFound, Unauthorized, ValidationError
from .masking import mask_name, mask_view
from .store import JsonlStore

STAFF_ROLES = {"admin", "clerk", "reviewer"}
READER_ROLES = {"admin", "clerk", "reviewer", "village"}
WRITE_ROLES = {"admin", "clerk"}
REVIEW_ROLES = {"admin", "reviewer"}
VERIFY_ROLES = {"admin", "clerk", "reviewer", "institution"}


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
def ep_health(service, identity, params, body, query):
    return 200, {"status": "ok"}


def ep_register(service, identity, params, body, query):
    payload = {k: body[k] for k in
               ("parcel_code", "location", "area_mu", "boundary", "members", "rights")
               if k in body}
    view = service.register_parcel(
        actor=identity, payload=payload,
        occurred_at=body.get("occurred_at"), evidence=body.get("evidence"))
    return 201, mask_view(view, identity["role"])


def ep_list_parcels(service, identity, params, body, query):
    return 200, {"parcels": service.list_parcels()}


def ep_get_parcel(service, identity, params, body, query):
    return 200, mask_view(service.get_parcel(params["parcel_id"]), identity["role"])


def ep_list_versions(service, identity, params, body, query):
    return 200, {"versions": mask_view(service.list_versions(params["parcel_id"]), identity["role"])}


def ep_get_version(service, identity, params, body, query):
    view = service.get_version(params["parcel_id"], int(params["version_no"]))
    return 200, mask_view(view, identity["role"])


def ep_as_of(service, identity, params, body, query):
    at = query.get("at", [None])[0]
    if not at:
        raise ValidationError("缺少查询参数 at（ISO 日期或日期时间）")
    basis = query.get("basis", ["occurred"])[0]
    view = service.version_as_of(params["parcel_id"], at, basis)
    return 200, mask_view(view, identity["role"])


def ep_parcel_audit(service, identity, params, body, query):
    return 200, {"audit": service.parcel_audit(params["parcel_id"])}


def ep_integrity(service, identity, params, body, query):
    return 200, service.verify_integrity(params["parcel_id"])


def ep_submit_change(service, identity, params, body, query):
    view = service.submit_change(
        actor=identity, parcel_id=params["parcel_id"],
        change_type=body.get("change_type"), payload=body.get("payload") or {},
        occurred_at=body.get("occurred_at"), evidence=body.get("evidence"),
        base_version=body.get("base_version"))
    return 201, mask_view(view, identity["role"])


def ep_issue_certificate(service, identity, params, body, query):
    view = service.issue_certificate(
        actor=identity, parcel_id=params["parcel_id"],
        base_version=body.get("base_version"),
        holder_name=body.get("holder_name"), note=body.get("note", ""),
        occurred_at=body.get("occurred_at"))
    return 201, mask_view(view, identity["role"])


def ep_revoke_certificate(service, identity, params, body, query):
    view = service.revoke_certificate(
        actor=identity, cert_id=params["cert_id"],
        base_version=body.get("base_version"),
        reason=body.get("reason", ""), note=body.get("note", ""),
        occurred_at=body.get("occurred_at"))
    return 201, mask_view(view, identity["role"])


def ep_verify(service, identity, params, body, query):
    if not body.get("cert_no") and not body.get("cert_id"):
        raise ValidationError("需提供 cert_no 或 cert_id")
    result = service.verify_certificate(
        actor=identity, cert_no=body.get("cert_no"), cert_id=body.get("cert_id"))
    if identity["role"] == "institution" and result.get("holder_name"):
        result["holder_name"] = mask_name(result["holder_name"])
    return 200, result


def ep_list_reviews(service, identity, params, body, query):
    status = query.get("status", ["pending"])[0]
    return 200, {"reviews": service.list_reviews(status)}


def ep_decide_review(service, identity, params, body, query):
    item = service.decide_review(
        actor=identity, review_id=params["review_id"],
        decision=body.get("decision"), note=body.get("note", ""))
    return 200, item


def ep_global_audit(service, identity, params, body, query):
    return 200, {"audit": service.global_audit()}


ROUTES = [
    ("GET", re.compile(r"^/health$"), None, ep_health),
    ("POST", re.compile(r"^/parcels$"), WRITE_ROLES, ep_register),
    ("GET", re.compile(r"^/parcels$"), READER_ROLES, ep_list_parcels),
    ("GET", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)$"), READER_ROLES, ep_get_parcel),
    ("GET", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)/versions$"), READER_ROLES, ep_list_versions),
    ("GET", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)/versions/(?P<version_no>\d+)$"), READER_ROLES, ep_get_version),
    ("GET", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)/as-of$"), READER_ROLES, ep_as_of),
    ("GET", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)/audit$"), READER_ROLES, ep_parcel_audit),
    ("GET", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)/integrity$"), READER_ROLES, ep_integrity),
    ("POST", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)/changes$"), WRITE_ROLES, ep_submit_change),
    ("POST", re.compile(r"^/parcels/(?P<parcel_id>[^/]+)/certificates$"), WRITE_ROLES, ep_issue_certificate),
    ("POST", re.compile(r"^/certificates/(?P<cert_id>[^/]+)/revoke$"), WRITE_ROLES, ep_revoke_certificate),
    ("POST", re.compile(r"^/verify$"), VERIFY_ROLES, ep_verify),
    ("GET", re.compile(r"^/reviews$"), REVIEW_ROLES, ep_list_reviews),
    ("POST", re.compile(r"^/reviews/(?P<review_id>[^/]+)/decision$"), REVIEW_ROLES, ep_decide_review),
    ("GET", re.compile(r"^/audit$"), REVIEW_ROLES, ep_global_audit),
]


# ---------------------------------------------------------------------------
# HTTP 骨架
# ---------------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    """林地权属版本服务请求处理。"""

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            status, payload = self._dispatch(method)
        except ApiError as exc:
            status = exc.status
            payload = {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
        except Exception as exc:  # noqa: BLE001
            status = 500
            payload = {"error": {"code": "INTERNAL", "message": f"服务内部错误：{exc}", "details": {}}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        for route_method, pattern, roles, endpoint in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            identity = None
            if roles is not None:
                identity = self.server.authenticator.authenticate(
                    self.headers.get("Authorization"))
                if identity is None:
                    raise Unauthorized("缺少或未知的访问令牌")
                if identity["role"] not in roles:
                    raise Forbidden(f"角色 {identity['role']} 无权访问该接口")
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    raw = self.rfile.read(length)
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise ValidationError("请求体不是合法 JSON") from None
                    if not isinstance(body, dict):
                        raise ValidationError("请求体必须是 JSON 对象")
            return endpoint(self.server.tenure_service, identity, match.groupdict(), body, query)
        raise NotFound(f"接口不存在：{method} {parsed.path}", code="ROUTE_NOT_FOUND")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str = "0.0.0.0", port: int = 0, *,
                  service: TenureService | None = None,
                  data_dir: str | None = None) -> _Server:
    """创建可由应用与测试共同使用的服务实例。"""
    if service is None:
        service = TenureService(JsonlStore(data_dir or os.environ.get("DATA_DIR", "./data")))
    server = _Server((host, port), Handler)
    server.tenure_service = service
    server.authenticator = Authenticator(load_tokens(service.store.data_dir))
    return server


def main() -> None:
    """启动服务。"""
    port = int(os.environ.get("PORT", "3000"))
    data_dir = os.environ.get("DATA_DIR", "./data")
    server = create_server(port=port, data_dir=data_dir)
    print(f"林地权属版本服务已启动：http://0.0.0.0:{port}（数据目录：{data_dir}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
