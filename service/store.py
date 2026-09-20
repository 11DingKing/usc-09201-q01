"""JSONL 账本持久化：版本事件、复核队列、审计链。

所有记录只追加、不覆盖；每次写入后立即 fsync，保证服务异常停止后
已提交的数据不丢失，重启时按原顺序重放恢复。
"""

from __future__ import annotations

import json
import os

EVENTS_FILE = "events.jsonl"
REVIEWS_FILE = "reviews.jsonl"
AUDIT_FILE = "audit.jsonl"


class JsonlStore:
    """按行追加的 JSON 账本存储。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    def _append(self, name: str, record: dict) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with open(self._path(name), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _read(self, name: str):
        path = self._path(name)
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    # 版本事件（不可覆盖时间线）
    def append_event(self, record: dict) -> None:
        self._append(EVENTS_FILE, record)

    def read_events(self):
        return self._read(EVENTS_FILE)

    # 复核队列（入队与决定各为一条记录，重放后未决定项仍在原队列）
    def append_review(self, record: dict) -> None:
        self._append(REVIEWS_FILE, record)

    def read_reviews(self):
        return self._read(REVIEWS_FILE)

    # 审计链
    def append_audit(self, record: dict) -> None:
        self._append(AUDIT_FILE, record)

    def read_audit(self):
        return self._read(AUDIT_FILE)
