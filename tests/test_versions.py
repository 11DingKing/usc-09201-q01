"""版本时间线：登记、变更、并发阻断、迟到补录、按时间查询与完整性。"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.parse

from tests.helpers import CLERK_A, CLERK_B, ServerCase, make_service, parcel_body


class VersionTimelineTest(ServerCase):
    """不可覆盖时间线与当时有效版本。"""

    def test_register_creates_first_version(self):
        view = self.register()
        self.assertEqual(view["version_no"], 1)
        self.assertEqual(view["change_type"], "register")
        self.assertEqual(view["prev_hash"], "0" * 64)
        self.assertEqual(len(view["hash"]), 64)
        self.assertEqual(view["review_status"], "pending")
        parcel_id = view["parcel_id"]

        _, parcel = self.req("GET", f"/parcels/{parcel_id}", expect=200)
        self.assertEqual(parcel["current_version"], 1)
        self.assertEqual(len(parcel["state"]["members"]), 2)
        self.assertEqual(parcel["state"]["certificates"], [])

    def test_member_change_and_version_chain(self):
        parcel_id = self.register()["parcel_id"]
        status, view = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1,
            "change_type": "member_change",
            "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111",
                                 "phone": "13800003333", "share": 0.2}]},
            "evidence": {"documents": [{"type": "户籍证明", "ref": "HJ-2026-01"}]},
        }, token=CLERK_A, expect=201)
        self.assertEqual(view["version_no"], 2)
        self.assertEqual(len(view["snapshot"]["members"]), 3)
        # 哈希链相连
        _, versions = self.req("GET", f"/parcels/{parcel_id}/versions", expect=200)
        self.assertEqual([v["version_no"] for v in versions["versions"]], [1, 2])
        _, v1 = self.req("GET", f"/parcels/{parcel_id}/versions/1", expect=200)
        self.assertEqual(view["prev_hash"], v1["hash"])
        # 历史版本未被覆盖：v1 仍是 2 名成员
        self.assertEqual(len(v1["snapshot"]["members"]), 2)

    def test_concurrent_clerks_one_blocked_with_reason(self):
        parcel_id = self.register()["parcel_id"]
        # 经办 B 基于过期版本提交 → 阻断并给出理由
        self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111"}]},
            "evidence": {"note": "经办 A 的变更"},
        }, token=CLERK_A, expect=201)
        status, blocked = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-02",
            "payload": {"add": [{"name": "赵四", "id_number": "110101199202022222"}]},
            "evidence": {"note": "经办 B 的变更"},
        }, token=CLERK_B, expect=409)
        self.assertEqual(blocked["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(blocked["error"]["details"]["base_version"], 1)
        self.assertEqual(blocked["error"]["details"]["current_version"], 2)
        self.assertIn("并发修订", blocked["error"]["message"])
        # 阻断已留痕
        _, audit = self.req("GET", f"/parcels/{parcel_id}/audit", expect=200)
        blocked_entries = [e for e in audit["audit"] if e["action"] == "change_blocked"]
        self.assertEqual(len(blocked_entries), 1)
        self.assertEqual(blocked_entries[0]["actor"], "clerk-b")
        self.assertEqual(blocked_entries[0]["detail"]["reason_code"], "VERSION_CONFLICT")

    def test_boundary_adjust_requires_all_members_confirmed(self):
        parcel_id = self.register()["parcel_id"]
        members = self.member_ids(parcel_id)
        # 缺少一名共有人确认 → 阻断
        status, blocked = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "boundary_adjust", "occurred_at": "2026-03-01",
            "payload": {"boundary": ["东至山脊", "南至小河改道", "西至机耕道", "北至国有林场"],
                        "area_mu": 118.0},
            "evidence": {"documents": [{"type": "村民会议记录", "ref": "HY-2026-03"}],
                         "confirmed_by": [members[0]]},
        }, token=CLERK_A, expect=400)
        self.assertEqual(blocked["error"]["code"], "CONFIRMATION_INCOMPLETE")
        self.assertEqual(blocked["error"]["details"]["missing_confirmations"], [members[1]])
        # 全体确认后通过
        _, view = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "boundary_adjust", "occurred_at": "2026-03-01",
            "payload": {"boundary": ["东至山脊", "南至小河改道", "西至机耕道", "北至国有林场"],
                        "area_mu": 118.0},
            "evidence": {"documents": [{"type": "村民会议记录", "ref": "HY-2026-03"}],
                         "confirmed_by": members},
        }, token=CLERK_A, expect=201)
        self.assertEqual(view["snapshot"]["area_mu"], 118.0)
        self.assertEqual(view["evidence"]["confirmed_by"], members)

    def test_partial_transfer_validation(self):
        parcel_id = self.register()["parcel_id"]
        # 流转面积超过宗地面积
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "partial_transfer", "occurred_at": "2026-04-01",
            "payload": {"right_type": "林地经营权", "holder_name": "绿源合作社",
                        "scope_area_mu": 200, "term_start": "2026-04-01", "term_end": "2036-03-31"},
            "evidence": {"note": "流转合同"},
        }, token=CLERK_A, expect=400)
        self.assertEqual(resp["error"]["code"], "VALIDATION_ERROR")
        # 期限颠倒
        self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "partial_transfer", "occurred_at": "2026-04-01",
            "payload": {"right_type": "林地经营权", "holder_name": "绿源合作社",
                        "scope_area_mu": 30, "term_start": "2036-04-01", "term_end": "2026-03-31"},
            "evidence": {"note": "流转合同"},
        }, token=CLERK_A, expect=400)
        # 正常局部流转
        _, view = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "partial_transfer", "occurred_at": "2026-04-01",
            "payload": {"right_type": "林地经营权", "holder_name": "绿源合作社",
                        "scope_area_mu": 30, "term_start": "2026-04-01", "term_end": "2036-03-31"},
            "evidence": {"documents": [{"type": "流转合同", "ref": "LZ-2026-01"}]},
        }, token=CLERK_A, expect=201)
        rights = view["snapshot"]["rights"]
        self.assertEqual(len(rights), 2)
        transfer = rights[-1]
        self.assertEqual(transfer["right_type"], "林地经营权")
        self.assertEqual(transfer["scope_area_mu"], 30.0)
        self.assertTrue(transfer["term_end"] > transfer["term_start"])

    def test_late_backfill_preserves_effective_versions(self):
        parcel_id = self.register()["parcel_id"]
        _, v2 = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111"}]},
            "evidence": {"note": "正常变更"},
        }, token=CLERK_A, expect=201)
        time.sleep(0.01)
        # 迟到补录：业务发生时间早于 v2
        _, v3 = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 2, "change_type": "member_change", "occurred_at": "2026-01-20",
            "payload": {"add": [{"name": "孙五", "id_number": "110101198805053333"}]},
            "evidence": {"documents": [{"type": "补录说明", "ref": "BL-2026-01"}],
                         "note": "纸质材料迟到补录"},
        }, token=CLERK_B, expect=201)
        self.assertTrue(v3["is_backfill"])
        self.assertEqual(v3["version_no"], 3)
        # 按业务发生时间重建：2026-01-25 时补录已生效，王二（02-01 加入）尚未在册
        _, as_of = self.req(
            "GET", f"/parcels/{parcel_id}/as-of?at=2026-01-25&basis=occurred", expect=200)
        self.assertTrue(as_of["reconstructed"])
        self.assertEqual(as_of["based_on_versions"], [1, 3])
        self.assertEqual(as_of["latest_version_no"], 3)
        names = [m["name"] for m in as_of["snapshot"]["members"]]
        self.assertIn("孙五", names)
        self.assertNotIn("王二", names)
        # 2026-02-02 时两笔都已生效
        _, as_of2 = self.req(
            "GET", f"/parcels/{parcel_id}/as-of?at=2026-02-02&basis=occurred", expect=200)
        self.assertEqual(as_of2["based_on_versions"], [1, 3, 2])
        names = [m["name"] for m in as_of2["snapshot"]["members"]]
        self.assertIn("孙五", names)
        self.assertIn("王二", names)
        # 按记录时间：补录落盘前的时刻，系统当时有效版本是 v2（当时有效版本保留）
        at = urllib.parse.quote(v2["recorded_at"])
        _, recorded = self.req(
            "GET", f"/parcels/{parcel_id}/as-of?at={at}&basis=recorded", expect=200)
        self.assertFalse(recorded["reconstructed"])
        self.assertEqual(recorded["version_no"], 2)
        names = [m["name"] for m in recorded["snapshot"]["members"]]
        self.assertIn("王二", names)
        self.assertNotIn("孙五", names)
        # 历史版本均未覆盖
        _, integrity = self.req("GET", f"/parcels/{parcel_id}/integrity", expect=200)
        self.assertTrue(integrity["ok"])
        self.assertEqual(integrity["versions_checked"], 3)

    def test_backfill_before_registration_rejected(self):
        parcel_id = self.register()["parcel_id"]
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2025-12-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111"}]},
            "evidence": {"note": "发生时间早于登记"},
        }, token=CLERK_A, expect=400)
        self.assertIn("初始登记", resp["error"]["message"])

    def test_evidence_required(self):
        parcel_id = self.register()["parcel_id"]
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111"}]},
        }, token=CLERK_A, expect=400)
        self.assertIn("变更依据", resp["error"]["message"])

    def test_tampered_ledger_refused_on_reload(self):
        parcel_id = self.register()["parcel_id"]
        self._stop()
        # 篡改账本中的一行
        path = os.path.join(self.tmp.name, "events.jsonl")
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        record = json.loads(lines[0])
        record["snapshot"]["area_mu"] = 999.0
        lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        with self.assertRaises(RuntimeError):
            make_service(self.tmp.name)


if __name__ == "__main__":
    unittest.main()
