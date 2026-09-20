"""基础健康检查测试。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request

from service.main import create_app


class HealthTest(unittest.TestCase):
    """验证基础服务可以响应。"""

    def test_health(self) -> None:
        server, store = create_app(":memory:")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://{host}:{port}/health") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"status": "ok"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            store.close()


if __name__ == "__main__":
    unittest.main()
