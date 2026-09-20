"""验收回放：一宗林地三次变更，旧证核验与新证签发并发，
检查返回版本、阻断理由与完整审计，并验证恢复后复核队列不丢。"""

from __future__ import annotations

import threading
import unittest
import urllib.parse

from tests.helpers import BANK, CLERK_A, CLERK_B, REVIEWER, VILLAGE, ServerCase


class AcceptanceReplayTest(ServerCase):
    """对应验收会脚本的端到端场景。"""

    def test_full_replay(self):
        # 1. 初始登记（第一批经营权证底账）
        v1 = self.register()
        parcel_id = v1["parcel_id"]
        members = self.member_ids(parcel_id)

        # 2. 三次变更：成员增加 → 边界调整（全体确认）→ 局部流转
        _, v2 = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-10",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111",
                                 "phone": "13800003333", "share": 0.1}]},
            "evidence": {"documents": [{"type": "户籍迁入证明", "ref": "HJ-2026-02"}]},
        }, token=CLERK_A, expect=201)
        members = self.member_ids(parcel_id)
        _, v3 = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 2, "change_type": "boundary_adjust", "occurred_at": "2026-03-05",
            "payload": {"boundary": ["东至山脊", "南至小河改道", "西至机耕道", "北至国有林场"],
                        "area_mu": 118.0},
            "evidence": {"documents": [{"type": "村民会议记录", "ref": "HY-2026-03"}],
                         "confirmed_by": members},
        }, token=CLERK_B, expect=201)
        _, v4 = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 3, "change_type": "partial_transfer", "occurred_at": "2026-04-01",
            "payload": {"right_type": "林地经营权", "holder_name": "绿源合作社",
                        "scope_area_mu": 30, "term_start": "2026-04-01", "term_end": "2036-03-31"},
            "evidence": {"documents": [{"type": "流转合同", "ref": "LZ-2026-01"}]},
        }, token=CLERK_A, expect=201)
        self.assertEqual((v2["version_no"], v3["version_no"], v4["version_no"]), (2, 3, 4))

        # 3. 签发旧证 C1
        _, v5 = self.req("POST", f"/parcels/{parcel_id}/certificates",
                         {"base_version": 4}, token=CLERK_A, expect=201)
        old_cert = v5["snapshot"]["certificates"][-1]

        # 4. 迟到补录：业务发生时间早于成员增加，仍保留当时有效版本
        _, v6 = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 5, "change_type": "member_change", "occurred_at": "2026-01-25",
            "payload": {"add": [{"name": "孙五", "id_number": "110101198805053333"}]},
            "evidence": {"documents": [{"type": "补录说明", "ref": "BL-2026-01"}]},
        }, token=CLERK_B, expect=201)
        self.assertTrue(v6["is_backfill"])
        _, as_of = self.req(
            "GET", f"/parcels/{parcel_id}/as-of?at=2026-02-01&basis=occurred", expect=200)
        # 补录（01-25 生效）在 2 月 1 日已生效，02-10 的成员增加尚未发生
        self.assertEqual(as_of["based_on_versions"], [1, 6])
        self.assertEqual(as_of["latest_version_no"], 6)
        at = urllib.parse.quote(v5["recorded_at"])
        _, recorded = self.req(
            "GET", f"/parcels/{parcel_id}/as-of?at={at}&basis=recorded", expect=200)
        self.assertEqual(recorded["version_no"], 5)  # 当时系统有效版本仍是签发版

        # 5. 两名经办人并发修订：一人成功，一人被阻断并给出理由
        barrier = threading.Barrier(2)
        race = {}

        def submit(name, token):
            def run():
                barrier.wait()
                race[name] = self.req("POST", f"/parcels/{parcel_id}/changes", {
                    "base_version": 6, "change_type": "member_change",
                    "occurred_at": "2026-05-01",
                    "payload": {"add": [{"name": f"成员{name}",
                                         "id_number": "110101199501014444"}]},
                    "evidence": {"note": f"经办 {name} 修订"},
                }, token=token)
            return run

        threads = [threading.Thread(target=submit("A", CLERK_A)),
                   threading.Thread(target=submit("B", CLERK_B))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(sorted(race[k][0] for k in race), [201, 409])
        blocked = [race[k][1] for k in race if race[k][0] == 409][0]
        self.assertEqual(blocked["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(blocked["error"]["details"]["current_version"], 7)
        self.assertIn("并发修订", blocked["error"]["message"])

        # 6. 旧证核验与新证签发同时发生
        barrier2 = threading.Barrier(2)
        phase = {}

        def verify_old():
            barrier2.wait()
            phase["verify"] = self.req("POST", "/verify",
                                       {"cert_no": old_cert["cert_no"]}, token=BANK)

        def issue_new():
            barrier2.wait()
            phase["issue"] = self.req("POST", f"/parcels/{parcel_id}/certificates",
                                      {"base_version": 7}, token=CLERK_A)

        threads = [threading.Thread(target=verify_old), threading.Thread(target=issue_new)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 新证签发成功，返回版本为 8
        self.assertEqual(phase["issue"][0], 201)
        self.assertEqual(phase["issue"][1]["version_no"], 8)
        new_cert = phase["issue"][1]["snapshot"]["certificates"][-1]
        # 核验返回版本自洽：签发前 valid@7 或签发后 superseded@8
        verify_body = phase["verify"][1]
        self.assertEqual(verify_body["issued_version"], 5)
        if verify_body["status"] == "valid":
            self.assertEqual(verify_body["current_version"], 7)
        else:
            self.assertEqual(verify_body["status"], "superseded")
            self.assertEqual(verify_body["current_version"], 8)
        # 旧证现在一定是 superseded，新证 valid
        _, old_now = self.req("POST", "/verify", {"cert_no": old_cert["cert_no"]},
                              token=BANK, expect=200)
        self.assertEqual(old_now["status"], "superseded")
        self.assertEqual(old_now["current_version"], 8)
        _, new_now = self.req("POST", "/verify", {"cert_no": new_cert["cert_no"]},
                              token=BANK, expect=200)
        self.assertEqual(new_now["status"], "valid")

        # 7. 完整审计：登记 + 三次变更 + 两次签发 + 补录 + 一次成员修订 = 8 次提交，
        #    一次阻断，至少两次核验
        _, audit = self.req("GET", f"/parcels/{parcel_id}/audit", expect=200)
        entries = audit["audit"]
        committed = [e for e in entries if e["action"] == "change_committed"]
        self.assertEqual(len(committed), 8)
        self.assertEqual([e["detail"]["version_no"] for e in committed],
                         [1, 2, 3, 4, 5, 6, 7, 8])
        blocked_entries = [e for e in entries if e["action"] == "change_blocked"]
        self.assertEqual(len(blocked_entries), 1)
        self.assertEqual(blocked_entries[0]["detail"]["reason_code"], "VERSION_CONFLICT")
        verifies = [e for e in entries if e["action"] == "verify"]
        self.assertGreaterEqual(len(verifies), 2)
        self.assertTrue(all(e["actor"] == "bank-1" for e in verifies))
        # 审计链哈希连续
        for prev, curr in zip(entries, entries[1:]):
            self.assertEqual(curr["prev_hash"], prev["hash"])

        # 8. 服务恢复：未完成复核仍在原队列
        _, pending = self.req("GET", "/reviews?status=pending", token=REVIEWER, expect=200)
        self.assertEqual(len(pending["reviews"]), 8)
        self.restart()
        _, pending_after = self.req("GET", "/reviews?status=pending",
                                    token=REVIEWER, expect=200)
        self.assertEqual([r["review_id"] for r in pending_after["reviews"]],
                         [r["review_id"] for r in pending["reviews"]])
        # 恢复后版本与证照状态完整
        _, parcel = self.req("GET", f"/parcels/{parcel_id}", expect=200)
        self.assertEqual(parcel["current_version"], 8)
        _, old_after = self.req("POST", "/verify", {"cert_no": old_cert["cert_no"]},
                                token=BANK, expect=200)
        self.assertEqual(old_after["status"], "superseded")

        # 9. 村集体查证：边界调整经过全体相关人确认，且证件已脱敏
        _, boundary_version = self.req("GET", f"/parcels/{parcel_id}/versions/3",
                                       token=VILLAGE, expect=200)
        self.assertEqual(boundary_version["change_type"], "boundary_adjust")
        confirmed = boundary_version["evidence"]["confirmed_by"]
        member_ids = [m["member_id"] for m in boundary_version["snapshot"]["members"]]
        self.assertEqual(set(confirmed), set(member_ids))
        self.assertIn("*", boundary_version["snapshot"]["members"][0]["id_number"])

        # 10. 复核员完成全部复核；时间线完整性校验通过
        for item in pending_after["reviews"]:
            self.req("POST", f"/reviews/{item['review_id']}/decision",
                     {"decision": "approved"}, token=REVIEWER, expect=200)
        _, left = self.req("GET", "/reviews?status=pending", token=REVIEWER, expect=200)
        self.assertEqual(left["reviews"], [])
        _, integrity = self.req("GET", f"/parcels/{parcel_id}/integrity", expect=200)
        self.assertTrue(integrity["ok"])
        self.assertEqual(integrity["versions_checked"], 8)


if __name__ == "__main__":
    unittest.main()
