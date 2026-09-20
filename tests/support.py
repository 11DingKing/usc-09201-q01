"""测试公共工具。"""

from __future__ import annotations

import http.client
import json
import threading
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from service.auth import load_tokens
from service.main import create_app
from service.store import Store

TOKENS = load_tokens()
OP = "tok-operator"
OP2 = "tok-operator-2"
RV = "tok-reviewer"
RG = "tok-registrar"
INST = "tok-institution"


class ServerHandle:
    def __init__(self) -> None:
        self.server, self.store = create_app(":memory:")
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        parts = urlsplit(path)
        encoded_path = urlunsplit(
            (parts.scheme, parts.netloc,
             quote(parts.path, safe="/"), parts.query, parts.fragment)
        )
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        conn.request(method, encoded_path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        conn.close()
        return response.status, json.loads(raw) if raw else None

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()


def file_store(path: str) -> Store:
    return Store(path)
