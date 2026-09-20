"""测试公共工具：内存级服务实例、临时数据目录与 HTTP 客户端。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from service.core import TenureService
from service.main import create_server
from service.store import JsonlStore

ADMIN = "token-admin"
CLERK_A = "token-clerk-a"
CLERK_B = "token-clerk-b"
REVIEWER = "token-reviewer"
VILLAGE = "token-village"
BANK = "token-bank"


def make_service(data_dir: str) -> TenureService:
    return TenureService(JsonlStore(data_dir))


def parcel_body(**overrides) -> dict:
    body = {
        "parcel_code": "TD-2026-0001",
        "location": "青山镇白云村三组",
        "area_mu": 120.5,
        "boundary": ["东至山脊", "南至小河", "西至机耕道", "北至国有林场"],
        "members": [
            {"name": "张大山", "id_number": "110101196001011234",
             "phone": "13800001111", "share": 0.6},
            {"name": "李秀兰", "id_number": "110101196203024321",
             "phone": "13800002222", "share": 0.4},
        ],
        "rights": [
            {"right_type": "承包经营权", "holder_name": "白云村三组",
             "term_start": "2026-01-01", "term_end": "2056-12-31"},
        ],
        "occurred_at": "2026-01-10",
        "evidence": {
            "documents": [{"type": "承包合同", "ref": "HT-2026-001"}],
            "note": "第一批经营权证登记",
        },
    }
    body.update(overrides)
    return body


class ServerCase(unittest.TestCase):
    """启动真实 HTTP 服务的测试基类。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._running = False
        self.service = make_service(self.tmp.name)
        self._start()
        self.addCleanup(self._stop)

    def _start(self) -> None:
        self.server = create_server("127.0.0.1", 0, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._running = True
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def _stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def restart(self) -> None:
        """模拟服务恢复：同一数据目录重建服务与 HTTP 层。"""
        self._stop()
        self.service = make_service(self.tmp.name)
        self._start()

    def req(self, method: str, path: str, body=None, token=ADMIN, expect=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                status, payload = response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, json.loads(exc.read())
        if expect is not None:
            self.assertEqual(status, expect, payload)
        return status, payload

    def register(self, token=CLERK_A, **overrides) -> dict:
        status, payload = self.req("POST", "/parcels", parcel_body(**overrides),
                                   token=token, expect=201)
        return payload

    def member_ids(self, parcel_id: str):
        _, view = self.req("GET", f"/parcels/{parcel_id}", expect=200)
        return [m["member_id"] for m in view["state"]["members"]]
