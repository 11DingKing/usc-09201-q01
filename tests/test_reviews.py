"""复核队列：入队、决定与服务恢复后队列保持。"""

from __future__ import annotations

import unittest

from tests.helpers import CLERK_A, REVIEWER, ServerCase


class ReviewQueueTest(ServerCase):
    """每次提交生成复核任务；重启后未完成复核仍在原队列。"""

    def _three_changes(self):
        parcel_id = self.register()["parcel_id"]
        self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111"}]},
            "evidence": {"note": "成员增加"},
        }, token=CLERK_A, expect=201)
        self.req("POST", f"/parcels/{parcel_id}/certificates",
                 {"base_version": 2}, token=CLERK_A, expect=201)
        return parcel_id

    def test_every_commit_enqueues_review(self):
        parcel_id = self._three_changes()
        _, queue = self.req("GET", "/reviews?status=pending", token=REVIEWER, expect=200)
        self.assertEqual(len(queue["reviews"]), 3)
        self.assertEqual([r["version_no"] for r in queue["reviews"]], [1, 2, 3])
        self.assertTrue(all(r["parcel_id"] == parcel_id for r in queue["reviews"]))
        self.assertTrue(all(r["status"] == "pending" for r in queue["reviews"]))

    def test_decide_and_no_double_decision(self):
        self._three_changes()
        _, queue = self.req("GET", "/reviews", token=REVIEWER, expect=200)
        review_id = queue["reviews"][0]["review_id"]
        _, decided = self.req("POST", f"/reviews/{review_id}/decision",
                              {"decision": "approved", "note": "材料齐全"},
                              token=REVIEWER, expect=200)
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["decided_by"], "reviewer-1")
        _, resp = self.req("POST", f"/reviews/{review_id}/decision",
                           {"decision": "rejected"}, token=REVIEWER, expect=409)
        self.assertEqual(resp["error"]["code"], "REVIEW_NOT_PENDING")
        # 复核决定已留痕
        _, audit = self.req("GET", "/audit", token=REVIEWER, expect=200)
        decisions = [e for e in audit["audit"] if e["action"] == "review_decided"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["detail"]["decision"], "approved")

    def test_pending_reviews_survive_restart_in_order(self):
        parcel_id = self._three_changes()
        _, before = self.req("GET", "/reviews?status=pending", token=REVIEWER, expect=200)
        # 先完成一条，留下两条未完成
        first = before["reviews"][0]["review_id"]
        self.req("POST", f"/reviews/{first}/decision",
                 {"decision": "approved"}, token=REVIEWER, expect=200)
        _, pending_before = self.req("GET", "/reviews?status=pending",
                                     token=REVIEWER, expect=200)
        # 服务恢复
        self.restart()
        _, pending_after = self.req("GET", "/reviews?status=pending",
                                    token=REVIEWER, expect=200)
        self.assertEqual([r["review_id"] for r in pending_after["reviews"]],
                         [r["review_id"] for r in pending_before["reviews"]])
        self.assertEqual(len(pending_after["reviews"]), 2)
        # 恢复后仍可继续复核
        remaining = pending_after["reviews"][0]["review_id"]
        _, decided = self.req("POST", f"/reviews/{remaining}/decision",
                              {"decision": "rejected", "note": "补充材料"},
                              token=REVIEWER, expect=200)
        self.assertEqual(decided["status"], "rejected")
        # 已决定的任务重启后不会回到待办
        self.restart()
        _, final = self.req("GET", "/reviews?status=pending", token=REVIEWER, expect=200)
        self.assertEqual(len(final["reviews"]), 1)
        # 宗地版本在重启后完整恢复
        _, parcel = self.req("GET", f"/parcels/{parcel_id}", expect=200)
        self.assertEqual(parcel["current_version"], 3)


if __name__ == "__main__":
    unittest.main()
