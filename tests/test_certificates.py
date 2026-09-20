"""证照撤回、流转期限状态与迟到补录按历史时点校验。"""

from __future__ import annotations

import unittest

from tests.support import INST, OP, RG, RV, ServerHandle


def member(mid: str, name: str, id_card: str = "350101198001011234",
           phone: str = "13800000001") -> dict:
    return {"id": mid, "name": name, "id_card": id_card, "phone": phone}


class CertificateAndBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = ServerHandle()

    def tearDown(self) -> None:
        self.h.stop()

    def _register(self, parcel: str = "P-100") -> None:
        status, chg = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": parcel, "kind": "register",
            "occurred_at": "2026-01-01", "base_version": 0,
            "payload": {"parcel_name": "撤回试宗", "location": "青山村",
                        "boundary": "B0",
                        "members": [member("m1", "陈山"), member("m2", "林秀",
                                        "350101198203032345", "13800000002")],
                        "rights": []},
        })
        self.assertEqual(status, 201)
        status, _ = self.h.request(
            "POST", f"/api/v1/changes/{chg['change_id']}/decision", RV,
            {"decision": "approve"})
        self.assertEqual(status, 200)

    def _approve(self, change_id: str) -> None:
        status, data = self.h.request(
            "POST", f"/api/v1/changes/{change_id}/decision", RV,
            {"decision": "approve"})
        self.assertEqual(status, 200, data)

    def test_withdrawn_certificate_is_blocked(self) -> None:
        self._register()
        status, cert = self.h.request(
            "POST", "/api/v1/certificates", RG,
            {"parcel_id": "P-100", "cert_no": "林证-W"})
        self.assertEqual(status, 201)

        # 撤回必须填写理由
        status, err = self.h.request(
            "POST", "/api/v1/certificates/林证-W/withdraw", RG, {"reason": ""})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "reason_required")

        status, _ = self.h.request(
            "POST", "/api/v1/certificates/林证-W/withdraw", RG,
            {"reason": "档案核对发现登记错误"})
        self.assertEqual(status, 200)

        status, verdict = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": "林证-W", "today": "2026-09-20"})
        self.assertEqual(status, 200)
        self.assertFalse(verdict["valid"])
        block = verdict["block_reasons"][0]
        self.assertEqual(block["code"], "cert_withdrawn")
        self.assertEqual(block["reason"], "档案核对发现登记错误")

        # 撤回不能重复
        status, err = self.h.request(
            "POST", "/api/v1/certificates/林证-W/withdraw", RG,
            {"reason": "再次撤回"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "cert_already_withdrawn")

    def test_transfer_term_statuses(self) -> None:
        self._register()
        status, chg = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": "P-100", "kind": "transfer",
            "occurred_at": "2026-01-05", "base_version": 1,
            "payload": {"transfer_id": "T-1", "transferee_name": "绿野合作社",
                        "portion": "东坡",
                        "start_date": "2026-01-10", "end_date": "2026-12-31"},
        })
        self.assertEqual(status, 201, chg)
        self._approve(chg["change_id"])
        status, cert = self.h.request(
            "POST", "/api/v1/certificates", RG,
            {"parcel_id": "P-100", "cert_no": "林证-T"})
        self.assertEqual(status, 201)

        status, before = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": "林证-T", "today": "2026-01-09"})
        self.assertEqual(before["transfers"][0]["derived_status"], "scheduled")
        status, active = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": "林证-T", "today": "2026-06-01"})
        self.assertEqual(active["transfers"][0]["derived_status"], "active")
        status, expired = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": "林证-T", "today": "2027-01-01"})
        self.assertEqual(expired["transfers"][0]["derived_status"], "expired")

        # 流转提前终止
        status, end = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": "P-100", "kind": "transfer_end",
            "occurred_at": "2026-05-01", "base_version": 2,
            "payload": {"transfer_id": "T-1", "end_date": "2026-04-30"},
        })
        self.assertEqual(status, 201, end)
        self._approve(end["change_id"])
        status, ended = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": "林证-T", "today": "2026-06-01"})
        self.assertEqual(ended["transfers"][0]["derived_status"], "ended_early")

    def test_late_backfill_boundary_checked_against_historical_members(
        self,
    ) -> None:
        """补录 1 月的成员后，补录 2 月边界调整必须包含该成员确认。"""

        self._register()
        # 先补录 m3 在 1 月 5 日入册
        status, add = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": "P-100", "kind": "member_change",
            "occurred_at": "2026-01-05", "base_version": 1,
            "payload": {"adds": [member("m3", "赵海", "350101199005053456",
                                        "13800000003")]},
        })
        self.assertEqual(status, 201)
        self._approve(add["change_id"])

        # 迟到补录 2 月 1 日的边界调整，只带 m1/m2 → 按 2 月时点 m3 已是共有人
        status, err = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": "P-100", "kind": "boundary_adjust",
            "occurred_at": "2026-02-01", "base_version": 2,
            "payload": {"boundary": "B1", "confirmations": ["m1", "m2"]},
        })
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "boundary_confirmation_missing")
        self.assertEqual(err["missing_member_ids"], ["m3"])

        # 非共有人确认同样被拒
        status, err = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": "P-100", "kind": "boundary_adjust",
            "occurred_at": "2026-02-01", "base_version": 2,
            "payload": {"boundary": "B1",
                        "confirmations": ["m1", "m2", "m3", "mX"]},
        })
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "unknown_confirmer")

    def test_role_permissions(self) -> None:
        self._register()
        # 授权机构不能签发
        status, err = self.h.request(
            "POST", "/api/v1/certificates", INST, {"parcel_id": "P-100"})
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "forbidden")
        # 经办人不能复核
        status, chg = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": "P-100", "kind": "member_change",
            "occurred_at": "2026-03-01", "base_version": 1,
            "payload": {"adds": [member("m9", "周九", "350101199209095678",
                                        "13800000009")]},
        })
        self.assertEqual(status, 201)
        status, err = self.h.request(
            "POST", f"/api/v1/changes/{chg['change_id']}/decision", OP,
            {"decision": "approve"})
        self.assertEqual(status, 403)
        # 无令牌
        status, err = self.h.request("GET", "/api/v1/parcels")
        self.assertEqual(status, 401)
        self.assertEqual(err["error"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
