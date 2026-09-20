"""按角色脱敏与访问控制。"""

from __future__ import annotations

import unittest

from tests.helpers import ADMIN, BANK, CLERK_A, REVIEWER, VILLAGE, ServerCase


class MaskingTest(ServerCase):
    """敏感身份信息按角色脱敏。"""

    def setUp(self):
        super().setUp()
        self.parcel_id = self.register()["parcel_id"]

    def test_staff_see_full_identity(self):
        for token in (ADMIN, CLERK_A, REVIEWER):
            _, view = self.req("GET", f"/parcels/{self.parcel_id}", token=token, expect=200)
            member = view["state"]["members"][0]
            self.assertEqual(member["id_number"], "110101196001011234")
            self.assertEqual(member["phone"], "13800001111")

    def test_village_sees_masked_identity(self):
        _, view = self.req("GET", f"/parcels/{self.parcel_id}", token=VILLAGE, expect=200)
        member = view["state"]["members"][0]
        self.assertEqual(member["name"], "张大山")  # 姓名对村集体可见
        self.assertNotIn("19600101", member["id_number"])
        self.assertTrue(member["id_number"].startswith("110"))
        self.assertTrue(member["id_number"].endswith("1234"))
        self.assertIn("*", member["id_number"])
        self.assertEqual(member["phone"], "138****1111")

    def test_village_masked_in_history_versions(self):
        _, view = self.req("GET", f"/parcels/{self.parcel_id}/versions/1",
                           token=VILLAGE, expect=200)
        member = view["snapshot"]["members"][0]
        self.assertIn("*", member["id_number"])

    def test_institution_cannot_read_parcels(self):
        for path in ("/parcels", f"/parcels/{self.parcel_id}",
                     f"/parcels/{self.parcel_id}/versions"):
            status, resp = self.req("GET", path, token=BANK, expect=403)
            self.assertEqual(resp["error"]["code"], "FORBIDDEN")

    def test_institution_verify_holder_masked(self):
        self.req("POST", f"/parcels/{self.parcel_id}/certificates",
                 {"base_version": 1}, token=CLERK_A, expect=201)
        _, parcel = self.req("GET", f"/parcels/{self.parcel_id}", expect=200)
        cert_no = parcel["state"]["certificates"][0]["cert_no"]
        _, result = self.req("POST", "/verify", {"cert_no": cert_no}, token=BANK, expect=200)
        self.assertEqual(result["holder_name"], "张**")
        # 机构看不到成员清单与证件号
        self.assertNotIn("members", result)
        self.assertNotIn("id_number", str(result))

    def test_missing_or_unknown_token(self):
        self.req("GET", f"/parcels/{self.parcel_id}", token=None, expect=401)
        self.req("GET", f"/parcels/{self.parcel_id}", token="token-ghost", expect=401)

    def test_role_cannot_write_or_review(self):
        status, resp = self.req("POST", f"/parcels/{self.parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111"}]},
            "evidence": {"note": "越权尝试"},
        }, token=VILLAGE, expect=403)
        self.assertEqual(resp["error"]["code"], "FORBIDDEN")
        self.req("GET", "/reviews", token=CLERK_A, expect=403)
        self.req("POST", "/verify", {"cert_no": "X"}, token=VILLAGE, expect=403)


if __name__ == "__main__":
    unittest.main()
