"""验收回放：一宗林地三次变更、迟到补录、并发修订、旧证核验与新证签发并发。"""

from __future__ import annotations

import threading
import unittest

from tests.support import INST, OP, OP2, RG, RV, ServerHandle


def member(mid: str, name: str, id_card: str, phone: str) -> dict:
    return {"id": mid, "name": name, "id_card": id_card, "phone": phone}


class AcceptanceReplayTest(unittest.TestCase):
    """按验收会脚本回放完整业务。"""

    def setUp(self) -> None:
        self.h = ServerHandle()

    def tearDown(self) -> None:
        self.h.stop()

    def submit(self, token: str, body: dict) -> dict:
        status, data = self.h.request("POST", "/api/v1/changes", token, body)
        self.assertEqual(status, 201, data)
        return data

    def approve(self, change_id: str) -> dict:
        status, data = self.h.request(
            "POST", f"/api/v1/changes/{change_id}/decision", RV,
            {"decision": "approve", "note": "材料齐全"},
        )
        self.assertEqual(status, 200, data)
        return data

    def test_three_changes_backfill_certificates_and_concurrent_verify(self) -> None:
        parcel = "P-001"

        # 第一次变更：初始登记（两名共有人，承包边界 B0）
        chg1 = self.submit(OP, {
            "parcel_id": parcel,
            "kind": "register",
            "occurred_at": "2026-01-01",
            "base_version": 0,
            "payload": {
                "parcel_name": "青山后片林地",
                "location": "青山村三组",
                "boundary": "B0:东沟-西岭-南坡-北岩",
                "members": [
                    member("m1", "陈山", "350101198001011234", "13800000001"),
                    member("m2", "林秀", "350101198203032345", "13800000002"),
                ],
                "rights": [{"type": "contract_management", "holder": "m1,m2"}],
            },
        })
        self.approve(chg1["change_id"])

        # 签发第一批权证（旧证 C1，版本 1）
        status, c1 = self.h.request(
            "POST", "/api/v1/certificates", RG,
            {"parcel_id": parcel, "cert_no": "林证-C1"},
        )
        self.assertEqual(status, 201, c1)
        self.assertEqual(c1["version"], 1)

        # 第二次变更：共有成员增加（赵海，2 月入册）
        chg2 = self.submit(OP, {
            "parcel_id": parcel,
            "kind": "member_change",
            "occurred_at": "2026-02-01",
            "base_version": 1,
            "payload": {"adds": [member("m3", "赵海", "350101199005053456",
                                         "13800000003")]},
        })
        self.approve(chg2["change_id"])

        # 边界调整只报了两名老共有人确认 → 复核被规则阻断
        status, err = self.h.request("POST", "/api/v1/changes", OP, {
            "parcel_id": parcel,
            "kind": "boundary_adjust",
            "occurred_at": "2026-03-01",
            "base_version": 2,
            "payload": {"boundary": "B1:东沟改线-西岭-南坡-北岩",
                        "confirmations": ["m1", "m2"]},
        })
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "boundary_confirmation_missing")
        self.assertEqual(err["missing_member_ids"], ["m3"])

        # 第三次变更：全体共有人确认后边界调整生效（版本 3）
        chg3 = self.submit(OP, {
            "parcel_id": parcel,
            "kind": "boundary_adjust",
            "occurred_at": "2026-03-01",
            "base_version": 2,
            "payload": {"boundary": "B1:东沟改线-西岭-南坡-北岩",
                        "confirmations": ["m1", "m2", "m3"]},
        })
        self.approve(chg3["change_id"])

        # 局部流转：合作社受让部分经营权，期限五年
        chg4 = self.submit(OP, {
            "parcel_id": parcel,
            "kind": "transfer",
            "occurred_at": "2026-06-01",
            "base_version": 3,
            "payload": {"transfer_id": "T-9", "transferee_name": "绿野林下经济合作社",
                        "portion": "东坡 40 亩",
                        "start_date": "2026-06-01", "end_date": "2031-05-31"},
        })
        self.approve(chg4["change_id"])

        # 迟到补录：钱河其实 1 月 10 日就已成为共有人（早于 3 月边界调整）
        chg5 = self.submit(OP, {
            "parcel_id": parcel,
            "kind": "member_change",
            "occurred_at": "2026-01-10",
            "base_version": 4,
            "payload": {"adds": [member("m4", "钱河", "350101199107074567",
                                         "13800000004")]},
        })
        self.assertTrue(chg5["status"] == "pending")
        self.approve(chg5["change_id"])

        # 当前版本按业务时间重放：共有人 4 名；3 月边界调整漏了钱河
        status, current = self.h.request("GET", f"/api/v1/parcels/{parcel}", INST)
        self.assertEqual(status, 200)
        self.assertEqual(current["effective_version"], 5)
        self.assertEqual(
            sorted(current["members"]), ["m1", "m2", "m3", "m4"]
        )
        self.assertEqual(current["boundary_assessment"]["missing"], ["m4"])
        # 补录事件在时间线中标记为 backfill，但版本号按记账顺序追加
        status, timeline = self.h.request(
            "GET", f"/api/v1/parcels/{parcel}/versions", INST
        )
        v5 = timeline["timeline"][-1]
        self.assertTrue(v5["backfill"])
        self.assertEqual(v5["kind"], "member_change")
        # 历史版本 3 的冻结快照保持记账当时的结论，不被补录覆盖
        v3 = timeline["timeline"][2]
        self.assertEqual(v3["version"], 3)
        self.assertEqual(v3["snapshot"]["boundary_assessment"]["missing"], [])

        # 旧证核验（企业融资场景）：撤回/替代 + 边界缺陷都要给出阻断理由
        status, verdict = self.h.request(
            "POST", "/api/v1/verify", INST, {"cert_no": "林证-C1",
                                             "today": "2026-09-20"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(verdict["valid"])
        codes = {b["code"] for b in verdict["block_reasons"]}
        self.assertIn("cert_outdated", codes)  # 新决定已生效、新证未发：旧证不得盖过
        self.assertIn("cert_boundary_stale", codes)
        self.assertIn("boundary_confirmation_missing", codes)
        boundary_block = next(
            b for b in verdict["block_reasons"]
            if b["code"] == "boundary_confirmation_missing"
        )
        self.assertEqual(boundary_block["missing_member_names"], ["钱河"])
        # 流转期限可向授权机构说明
        transfer = verdict["transfers"][0]
        self.assertEqual(transfer["derived_status"], "active")
        self.assertEqual(transfer["end_date"], "2031-05-31")
        # 授权机构看到脱敏的身份证号与电话
        self.assertNotIn("350101199107074567", str(verdict))
        self.assertTrue(any("*" in m["id_card"] for m in current["members"].values()))

        # 补做全体确认（含钱河）后，重发证才可通过核验
        chg6 = self.submit(OP, {
            "parcel_id": parcel,
            "kind": "boundary_adjust",
            "occurred_at": "2026-09-15",
            "base_version": 5,
            "payload": {"boundary": "B1:东沟改线-西岭-南坡-北岩",
                        "confirmations": ["m1", "m2", "m3", "m4"]},
        })
        self.approve(chg6["change_id"])
        status, c2 = self.h.request(
            "POST", "/api/v1/certificates", RG,
            {"parcel_id": parcel, "cert_no": "林证-C2"},
        )
        self.assertEqual(status, 201, c2)
        self.assertEqual(c2["version"], 6)
        status, verdict2 = self.h.request(
            "POST", "/api/v1/verify", INST, {"cert_no": "林证-C2",
                                             "today": "2026-09-20"}
        )
        self.assertTrue(verdict2["valid"], verdict2["block_reasons"])

        # 旧证现在同时带 superseded 状态与替代证号
        status, c1_view = self.h.request(
            "GET", "/api/v1/certificates/林证-C1", INST
        )
        self.assertEqual(c1_view["status"], "superseded")
        self.assertEqual(c1_view["superseded_by"], "林证-C2")

    def _setup_stale_parcel(self, parcel: str) -> None:
        """造一宗已发旧证（v1）且之后又有一次生效变更（v2）的地块。"""

        chg = self.submit(OP, {
            "parcel_id": parcel, "kind": "register",
            "occurred_at": "2026-01-01", "base_version": 0,
            "payload": {"parcel_name": "并发试宗", "location": "青山村",
                        "boundary": "B0",
                        "members": [member("m1", "陈山", "350101198001011234",
                                           "13800000001")],
                        "rights": []},
        })
        self.approve(chg["change_id"])
        status, _ = self.h.request(
            "POST", "/api/v1/certificates", RG,
            {"parcel_id": parcel, "cert_no": f"林证-{parcel}-OLD"})
        self.assertEqual(status, 201)
        chg2 = self.submit(OP, {
            "parcel_id": parcel, "kind": "member_change",
            "occurred_at": "2026-02-01", "base_version": 1,
            "payload": {"adds": [member("m2", "林秀", "350101198203032345",
                                        "13800000002")]},
        })
        self.approve(chg2["change_id"])

    def test_verify_issue_interleavings(self) -> None:
        """旧证核验与新证签发的两种交错：先发后验与先验后发。"""

        # 交错一：先核验（证载版本落后）→ 再签发 → 旧证被正式替代
        parcel_x = "P-X"
        self._setup_stale_parcel(parcel_x)
        status, before = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": f"林证-{parcel_x}-OLD", "today": "2026-09-20"})
        self.assertEqual(status, 200)
        self.assertFalse(before["valid"])
        self.assertIn("cert_outdated",
                      {b["code"] for b in before["block_reasons"]})
        self.assertEqual(before["issued_version"], 1)
        self.assertEqual(before["effective_version"], 2)

        status, new_cert = self.h.request(
            "POST", "/api/v1/certificates", RG, {"parcel_id": parcel_x})
        self.assertEqual(status, 201)
        status, after = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": f"林证-{parcel_x}-OLD", "today": "2026-09-20"})
        codes = {b["code"] for b in after["block_reasons"]}
        self.assertIn("cert_superseded", codes)
        stale = next(b for b in after["block_reasons"]
                     if b["code"] == "cert_superseded")
        self.assertEqual(stale["newer_cert_no"], new_cert["cert_no"])
        status, fresh = self.h.request(
            "POST", "/api/v1/verify", INST,
            {"cert_no": new_cert["cert_no"], "today": "2026-09-20"})
        self.assertTrue(fresh["valid"], fresh["block_reasons"])

        # 交错二：真正同时发生 —— 核验必须呈现单一全序：结果最多翻转一次，
        # 从 cert_outdated 单调变为 cert_superseded，绝不允许读脏或回退。
        parcel_y = "P-Y"
        self._setup_stale_parcel(parcel_y)
        verdicts: list[set[str]] = []
        saw_outdated = threading.Event()
        start = threading.Event()
        errors: list[Exception] = []

        def verify_loop() -> None:
            try:
                start.set()
                # 持续核验，直到亲眼看到“已替代”为止（带轮次上限防止死循环）
                for _ in range(2000):
                    status, data = self.h.request(
                        "POST", "/api/v1/verify", INST,
                        {"cert_no": f"林证-{parcel_y}-OLD",
                         "today": "2026-09-20"})
                    self.assertEqual(status, 200, data)
                    codes_local = {b["code"] for b in data["block_reasons"]}
                    verdicts.append(codes_local)
                    if "cert_outdated" in codes_local:
                        saw_outdated.set()
                    if "cert_superseded" in codes_local:
                        break
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def gated_issue() -> None:
            try:
                start.wait(2)
                self.assertTrue(saw_outdated.wait(2))
                # 核验方正在高频读取时签发，制造真实交错
                status, data = self.h.request(
                    "POST", "/api/v1/certificates", RG, {"parcel_id": parcel_y})
                self.assertEqual(status, 201, data)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=verify_loop)
        t2 = threading.Thread(target=gated_issue)
        t1.start(); t2.start()
        t2.join(5); t1.join(10)
        self.assertEqual(errors, [])

        def phase(codes: set[str]) -> int:
            if "cert_superseded" in codes:
                return 2
            return 1  # cert_outdated

        phases = [phase(c) for c in verdicts]
        self.assertEqual(phases, sorted(phases), "核验结果必须沿单一全序单调")
        self.assertIn(1, phases, "签发前应至少观察到一次 cert_outdated")
        self.assertEqual(phases[-1], 2, "签发后最终应观察到 cert_superseded")

    def test_two_operators_concurrent_edits(self) -> None:
        """两名经办人并发修订同一宗地：乐观锁只放行一个，另一个得到 409 并留痕。"""

        parcel = "P-OP"
        self._setup_stale_parcel(parcel)  # 当前版本 2

        body_a = {
            "parcel_id": parcel, "kind": "member_change",
            "occurred_at": "2026-03-01", "base_version": 2,
            "payload": {"adds": [member("m3", "赵海", "350101199005053456",
                                        "13800000003")]},
        }
        body_b = {
            "parcel_id": parcel, "kind": "boundary_adjust",
            "occurred_at": "2026-03-02", "base_version": 2,
            "payload": {"boundary": "B9", "confirmations": ["m1", "m2"]},
        }
        results: list[tuple[str, int, dict]] = []
        barrier = threading.Barrier(2)

        def edit(token: str, body: dict, tag: str) -> None:
            barrier.wait()
            results.append((tag, *self.h.request(
                "POST", "/api/v1/changes", token, body)))

        t1 = threading.Thread(target=edit, args=(OP, body_a, "A"))
        t2 = threading.Thread(target=edit, args=(OP2, body_b, "B"))
        t1.start(); t2.start()
        t1.join(5); t2.join(5)

        statuses = {tag: status for tag, status, _ in results}
        self.assertEqual(sorted(statuses.values()), [201, 409], results)
        loser_tag = next(tag for tag, status in statuses.items() if status == 409)
        loser = next(data for tag, _, data in results if tag == loser_tag)
        self.assertEqual(loser["error"], "base_version_conflict")
        self.assertEqual(loser["current_version"], 2)

        # 两件申请都留在队列里；冲突件不能复核，必须基于新版本重新提交
        status, queue = self.h.request(
            "GET", "/api/v1/changes?status=pending", RV)
        self.assertEqual(status, 200)
        pending = queue["changes"]
        self.assertEqual(len(pending), 1)
        status, conflicted = self.h.request(
            "GET", "/api/v1/changes?status=conflicted", RV)
        self.assertEqual(len(conflicted["changes"]), 1)
        cid = conflicted["changes"][0]["change_id"]
        status, err = self.h.request(
            "POST", f"/api/v1/changes/{cid}/decision", RV,
            {"decision": "approve"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "change_conflicted")

        # 放行的那件复核后成为版本 3
        winner_id = pending[0]["change_id"]
        self.approve(winner_id)
        status, current = self.h.request(
            "GET", f"/api/v1/parcels/{parcel}", OP)
        self.assertEqual(current["effective_version"], 3)

        # 冲突件基于 v3 重新提交后可正常复核（边界调整需 m3 也确认）
        loser_body = body_b if loser_tag == "B" else body_a
        if loser_body["kind"] == "boundary_adjust":
            loser_body["payload"]["confirmations"] = ["m1", "m2", "m3"]
        loser_body["base_version"] = 3
        status, resubmitted = self.h.request(
            "POST", "/api/v1/changes", OP2, loser_body)
        self.assertEqual(status, 201, resubmitted)
        self.approve(resubmitted["change_id"])


if __name__ == "__main__":
    unittest.main()
