"""证照生命周期：签发、取代、撤回与授权机构核验。"""

from __future__ import annotations

import unittest

from tests.helpers import BANK, CLERK_A, ServerCase


class CertificateTest(ServerCase):
    """证照版本与核验返回。"""

    def _issue(self, parcel_id: str, base_version: int):
        return self.req("POST", f"/parcels/{parcel_id}/certificates",
                        {"base_version": base_version}, token=CLERK_A, expect=201)[1]

    def test_issue_and_verify(self):
        parcel_id = self.register()["parcel_id"]
        view = self._issue(parcel_id, 1)
        self.assertEqual(view["version_no"], 2)
        self.assertEqual(view["change_type"], "certificate_issue")
        cert = view["snapshot"]["certificates"][-1]
        self.assertEqual(cert["status"], "valid")
        self.assertEqual(cert["issued_version"], 2)

        _, result = self.req("POST", "/verify", {"cert_no": cert["cert_no"]},
                             token=BANK, expect=200)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["issued_version"], 2)
        self.assertEqual(result["current_version"], 2)
        self.assertTrue(result["is_latest_version"])
        self.assertEqual(result["parcel_code"], "TD-2026-0001")

    def test_new_issue_supersedes_old_cert(self):
        parcel_id = self.register()["parcel_id"]
        first = self._issue(parcel_id, 1)["snapshot"]["certificates"][-1]
        second = self._issue(parcel_id, 2)["snapshot"]["certificates"][-1]
        self.assertNotEqual(first["cert_no"], second["cert_no"])

        _, old = self.req("POST", "/verify", {"cert_no": first["cert_no"]},
                          token=BANK, expect=200)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["issued_version"], 2)
        self.assertEqual(old["current_version"], 3)
        self.assertFalse(old["is_latest_version"])

        _, new = self.req("POST", "/verify", {"cert_no": second["cert_no"]},
                          token=BANK, expect=200)
        self.assertEqual(new["status"], "valid")
        self.assertTrue(new["is_latest_version"])

    def test_revoke_keeps_history(self):
        parcel_id = self.register()["parcel_id"]
        cert = self._issue(parcel_id, 1)["snapshot"]["certificates"][-1]
        _, view = self.req("POST", f"/certificates/{cert['cert_id']}/revoke",
                           {"base_version": 2, "reason": "权属争议待裁定"},
                           token=CLERK_A, expect=201)
        self.assertEqual(view["change_type"], "certificate_revoke")
        revoked = [c for c in view["snapshot"]["certificates"]
                   if c["cert_id"] == cert["cert_id"]][0]
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["revoke_reason"], "权属争议待裁定")

        _, result = self.req("POST", "/verify", {"cert_no": cert["cert_no"]},
                             token=BANK, expect=200)
        self.assertEqual(result["status"], "revoked")
        self.assertEqual(result["revoke_reason"], "权属争议待裁定")
        # 历史版本仍保留证照有效时的状态
        _, v2 = self.req("GET", f"/parcels/{parcel_id}/versions/2", expect=200)
        self.assertEqual(v2["snapshot"]["certificates"][0]["status"], "valid")
        # 重复撤回被阻断
        _, again = self.req("POST", f"/certificates/{cert['cert_id']}/revoke",
                            {"base_version": 3, "reason": "重复操作"},
                            token=CLERK_A, expect=409)
        self.assertEqual(again["error"]["code"], "CERT_ALREADY_REVOKED")

    def test_issue_with_stale_base_blocked(self):
        parcel_id = self.register()["parcel_id"]
        self._issue(parcel_id, 1)
        _, resp = self.req("POST", f"/parcels/{parcel_id}/certificates",
                           {"base_version": 1}, token=CLERK_A, expect=409)
        self.assertEqual(resp["error"]["code"], "VERSION_CONFLICT")

    def test_verify_unknown_cert(self):
        _, result = self.req("POST", "/verify", {"cert_no": "LQ-2099-9999"},
                             token=BANK, expect=200)
        self.assertEqual(result["status"], "unknown")
        # 未知证照的核验同样留痕
        _, audit = self.req("GET", "/audit", expect=200)
        verifies = [e for e in audit["audit"] if e["action"] == "verify"]
        self.assertEqual(verifies[-1]["detail"]["result"], "unknown")


if __name__ == "__main__":
    unittest.main()
