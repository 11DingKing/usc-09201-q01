"""并发场景：两名经办人同时修订、旧证核验与新证签发同时发生。"""

from __future__ import annotations

import threading
import unittest

from tests.helpers import BANK, CLERK_A, CLERK_B, ServerCase


def _fire(threads):
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)


class ConcurrencyTest(ServerCase):
    """全局锁串行化提交，败者得到阻断理由，双方均留痕。"""

    def test_two_clerks_race_exactly_one_commits(self):
        parcel_id = self.register()["parcel_id"]
        barrier = threading.Barrier(2)
        results = {}

        def submit(name, token, member):
            def run():
                barrier.wait()
                results[name] = self.req("POST", f"/parcels/{parcel_id}/changes", {
                    "base_version": 1, "change_type": "member_change",
                    "occurred_at": "2026-02-01",
                    "payload": {"add": [{"name": member, "id_number": "110101199001011111"}]},
                    "evidence": {"note": f"{name} 提交"},
                }, token=token)
            return run

        _fire([threading.Thread(target=submit("A", CLERK_A, "王二甲")),
               threading.Thread(target=submit("B", CLERK_B, "王二乙"))])

        statuses = sorted(results[name][0] for name in ("A", "B"))
        self.assertEqual(statuses, [201, 409])
        loser = [name for name in ("A", "B") if results[name][0] == 409][0]
        error = results[loser][1]["error"]
        self.assertEqual(error["code"], "VERSION_CONFLICT")
        self.assertEqual(error["details"]["current_version"], 2)
        # 只追加了一个版本
        _, parcel = self.req("GET", f"/parcels/{parcel_id}", expect=200)
        self.assertEqual(parcel["current_version"], 2)
        # 提交与阻断都进入审计
        _, audit = self.req("GET", f"/parcels/{parcel_id}/audit", expect=200)
        actions = [e["action"] for e in audit["audit"]]
        self.assertEqual(actions.count("change_committed"), 2)  # 登记 + 一次变更
        self.assertEqual(actions.count("change_blocked"), 1)

    def test_verify_old_cert_races_new_issue(self):
        parcel_id = self.register()["parcel_id"]
        cert = self.req("POST", f"/parcels/{parcel_id}/certificates",
                        {"base_version": 1}, token=CLERK_A, expect=201)[1]
        cert_no = cert["snapshot"]["certificates"][-1]["cert_no"]
        barrier = threading.Barrier(2)
        results = {}

        def verify():
            barrier.wait()
            results["verify"] = self.req("POST", "/verify", {"cert_no": cert_no},
                                         token=BANK)

        def issue():
            barrier.wait()
            results["issue"] = self.req("POST", f"/parcels/{parcel_id}/certificates",
                                        {"base_version": 2}, token=CLERK_A)

        _fire([threading.Thread(target=verify), threading.Thread(target=issue)])

        self.assertEqual(results["issue"][0], 201)
        self.assertEqual(results["issue"][1]["version_no"], 3)
        verify_status, verify_body = results["verify"]
        self.assertEqual(verify_status, 200)
        # 核验落在新证签发前或后，返回版本都必须自洽
        if verify_body["status"] == "valid":
            self.assertEqual(verify_body["current_version"], 2)
            self.assertTrue(verify_body["is_latest_version"])
        else:
            self.assertEqual(verify_body["status"], "superseded")
            self.assertEqual(verify_body["issued_version"], 2)
            self.assertEqual(verify_body["current_version"], 3)
            self.assertFalse(verify_body["is_latest_version"])
        # 核验与签发均留痕
        _, audit = self.req("GET", f"/parcels/{parcel_id}/audit", expect=200)
        actions = [e["action"] for e in audit["audit"]]
        self.assertIn("verify", actions)
        self.assertEqual(actions.count("change_committed"), 3)


if __name__ == "__main__":
    unittest.main()
