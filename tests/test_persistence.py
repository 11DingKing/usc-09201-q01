"""持久化：重启后未完成复核仍在原队列；版本与审计不可改删。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from service.auth import INSTITUTION, OPERATOR
from service.auth import mask_for_role
from service.store import Store


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ledger.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register_pending(self, store: Store, actor: dict) -> str:
        change = store.submit_change(
            actor,
            "P-RESTART",
            "register",
            {
                "parcel_name": "重启试宗",
                "location": "青山村",
                "boundary": "B0",
                "members": [
                    {"id": "m1", "name": "陈山", "id_card": "350101198001011234",
                     "phone": "13800000001"}
                ],
                "rights": [],
            },
            "2026-01-01",
            0,
        )
        return change["change_id"]

    def test_queue_survives_restart(self) -> None:
        actor = {"id": "op-1", "role": OPERATOR, "name": "张经办"}
        reviewer = {"id": "rv-1", "role": "reviewer", "name": "王复核"}

        store = Store(self.path)
        cid = self._register_pending(store, actor)
        self.assertEqual(store.list_changes("pending")[0]["change_id"], cid)
        store.close()

        # 恢复服务：未完成复核仍在原队列（同一 change_id、同一提交人）
        store2 = Store(self.path)
        try:
            pending = store2.list_changes("pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["change_id"], cid)
            self.assertEqual(pending[0]["submitted_by"], "op-1")

            approved = store2.decide_change(reviewer, cid, "approved")
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(
                store2.current_snapshot("P-RESTART")["effective_version"], 1
            )
            self.assertTrue(store2.verify_chain()["intact"])
        finally:
            store2.close()

    def test_versions_and_audit_are_append_only(self) -> None:
        actor = {"id": "op-1", "role": OPERATOR, "name": "张经办"}
        reviewer = {"id": "rv-1", "role": "reviewer", "name": "王复核"}
        store = Store(self.path)
        try:
            cid = self._register_pending(store, actor)
            store.decide_change(reviewer, cid, "approved")
            conn = store._conn
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE versions SET payload='[]' WHERE version=1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM versions WHERE version=1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE audit_log SET action='x' WHERE audit_id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM audit_log WHERE audit_id=1")
            # 数据未受影响
            self.assertEqual(
                store.version_snapshot("P-RESTART", 1)["boundary"], "B0"
            )
        finally:
            store.close()


class MaskingTest(unittest.TestCase):
    def test_internal_roles_see_plaintext(self) -> None:
        record = {"name": "陈山", "id_card": "350101198001011234",
                  "phone": "13800000001"}
        for role in ("operator", "reviewer", "registrar"):
            self.assertEqual(mask_for_role(record, role)["id_card"],
                             "350101198001011234")

    def test_institution_sees_masked_identity(self) -> None:
        record = {
            "name": "陈山",
            "id_card": "350101198001011234",
            "phone": "13800000001",
            "members": [{"id_card": "350101199107074567",
                         "phone": "13800000004"}],
        }
        masked = mask_for_role(record, INSTITUTION)
        self.assertEqual(masked["name"], "陈山")  # 姓名保留
        self.assertEqual(masked["id_card"], "350101********1234")
        self.assertEqual(masked["phone"], "138****0001")
        self.assertEqual(masked["members"][0]["id_card"], "350101********4567")


if __name__ == "__main__":
    unittest.main()
