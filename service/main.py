"""林地权属版本账服务入口。

数据库路径由环境变量 ``FOREST_DB`` 指定，默认为 ``./data/forest_ledger.db``；
未完成的复核队列、版本快照与审计链都持久化在该文件中，重启后自动恢复。
"""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from .api import make_handler
from .auth import load_tokens
from .store import Store


def create_app(db_path: str = ":memory:") -> tuple[ThreadingHTTPServer, Store]:
    """创建服务实例与底层存储，供应用启动与测试共同使用。"""

    store = Store(db_path)
    tokens = load_tokens()
    server = ThreadingHTTPServer(("0.0.0.0", 0), make_handler(store, tokens))
    return server, store


def create_server(host: str = "0.0.0.0", port: int = 0) -> ThreadingHTTPServer:
    """兼容旧接口：创建绑定持久化数据库的服务。"""

    db_path = os.environ.get("FOREST_DB", "data/forest_ledger.db")
    store = Store(db_path)
    tokens = load_tokens()
    return ThreadingHTTPServer((host, port), make_handler(store, tokens))


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    db_path = os.environ.get("FOREST_DB", "data/forest_ledger.db")
    store = Store(db_path)
    tokens = load_tokens()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store, tokens))
    print(f"服务已启动：http://0.0.0.0:{port}（数据库：{db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
