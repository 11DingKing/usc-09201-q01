"""接口边界行为：404、409、参数校验与只追加语义。"""

from __future__ import annotations

import unittest

from tests.helpers import BANK, CLERK_A, ServerCase, parcel_body


class EdgeCaseTest(ServerCase):
    def test_unknown_resources(self):
        self.req("GET", "/parcels/P-none", expect=404)
        parcel_id = self.register()["parcel_id"]
        self.req("GET", f"/parcels/{parcel_id}/versions/99", expect=404)
        _, resp = self.req("GET", f"/parcels/{parcel_id}/as-of?at=2020-01-01", expect=404)
        self.assertEqual(resp["error"]["code"], "NO_VERSION_AT_TIME")
        self.req("POST", "/certificates/Z-none/revoke",
                 {"base_version": 1, "reason": "x"}, token=CLERK_A, expect=404)
        self.req("POST", "/reviews/Q-none/decision",
                 {"decision": "approved"}, expect=404)
        self.req("GET", "/no-such-route", expect=404)

    def test_duplicate_parcel_code(self):
        self.register()
        _, resp = self.req("POST", "/parcels", parcel_body(), token=CLERK_A, expect=409)
        self.assertEqual(resp["error"]["code"], "PARCEL_CODE_EXISTS")

    def test_invalid_change_type_and_body(self):
        parcel_id = self.register()["parcel_id"]
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "sell_whole", "occurred_at": "2026-02-01",
            "payload": {}, "evidence": {"note": "x"},
        }, token=CLERK_A, expect=400)
        self.assertIn("不支持的变更类型", resp["error"]["message"])
        # base_version 缺失
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "王二", "id_number": "110101199001011111"}]},
            "evidence": {"note": "x"},
        }, token=CLERK_A, expect=400)
        self.assertIn("base_version", resp["error"]["message"])

    def test_member_remove_unknown_and_empty_guard(self):
        parcel_id = self.register()["parcel_id"]
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"remove": ["M-none"]},
            "evidence": {"note": "x"},
        }, token=CLERK_A, expect=400)
        self.assertIn("不存在", resp["error"]["message"])
        members = self.member_ids(parcel_id)
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"remove": members},
            "evidence": {"note": "全部退出"},
        }, token=CLERK_A, expect=400)
        self.assertIn("不能为空", resp["error"]["message"])

    def test_duplicate_member_id_number(self):
        parcel_id = self.register()["parcel_id"]
        _, resp = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"add": [{"name": "张大山", "id_number": "110101196001011234"}]},
            "evidence": {"note": "重复证件号"},
        }, token=CLERK_A, expect=400)
        self.assertIn("已在册", resp["error"]["message"])

    def test_member_remove_then_view_history(self):
        parcel_id = self.register()["parcel_id"]
        members = self.member_ids(parcel_id)
        _, view = self.req("POST", f"/parcels/{parcel_id}/changes", {
            "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-01",
            "payload": {"remove": [members[1]]},
            "evidence": {"documents": [{"type": "退出申请", "ref": "TC-2026-01"}]},
        }, token=CLERK_A, expect=201)
        self.assertEqual(len(view["snapshot"]["members"]), 1)
        # 历史版本仍见退出前成员
        _, v1 = self.req("GET", f"/parcels/{parcel_id}/versions/1", expect=200)
        self.assertEqual(len(v1["snapshot"]["members"]), 2)

    def test_verify_requires_cert_identifier(self):
        _, resp = self.req("POST", "/verify", {}, token=BANK, expect=400)
        self.assertIn("cert_no", resp["error"]["message"])

    def test_bad_json_and_nondict_body(self):
        request = self.req("POST", "/parcels", None, token=CLERK_A, expect=400)
        # 无请求体 → 缺少必填字段
        self.assertEqual(request[1]["error"]["code"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
