#!/usr/bin/env python3
"""验收回放：一宗林地三次变更 + 旧证核验与新证签发并发。

回放内容（对应验收会检查项）：
  1. 初始登记，随后三次变更（成员增加、边界调整、局部流转）；
  2. 签发旧证，迟到补录一笔历史成员变更；
  3. 两名经办人并发修订 —— 检查阻断理由；
  4. 旧证核验与新证签发同时发生 —— 检查返回版本；
  5. 完整审计回放；
  6. 服务恢复后未完成复核仍在原队列；
  7. 村集体视角查证边界调整是否经全体相关人确认（敏感信息已脱敏）。

用法：python3 scripts/acceptance_replay.py [数据目录]
退出码 0 表示全部检查通过。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.core import TenureService  # noqa: E402
from service.main import create_server  # noqa: E402
from service.store import JsonlStore  # noqa: E402

CLERK_A = "token-clerk-a"
CLERK_B = "token-clerk-b"
REVIEWER = "token-reviewer"
VILLAGE = "token-village"
BANK = "token-bank"

CHECKS = []


def check(condition, label, detail=""):
    CHECKS.append(condition)
    print(f"    [{'通过' if condition else '失败'}] {label}" + (f"：{detail}" if detail else ""))
    if not condition:
        raise SystemExit(f"验收检查未通过：{label}")


class Client:
    def __init__(self, base):
        self.base = base

    def req(self, method, path, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


def section(title):
    print(f"\n══ {title} ══")


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="forest-ledger-")
    service = TenureService(JsonlStore(data_dir))
    server = create_server("127.0.0.1", 0, service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    client = Client(f"http://{host}:{port}")
    print(f"服务已启动：http://{host}:{port}（数据目录：{data_dir}）")

    try:
        replay(client)
        print("\n══ 服务重启：验证恢复后状态 ══")
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service2 = TenureService(JsonlStore(data_dir))
        server2 = create_server("127.0.0.1", 0, service=service2)
        thread2 = threading.Thread(target=server2.serve_forever, daemon=True)
        thread2.start()
        host, port = server2.server_address
        client2 = Client(f"http://{host}:{port}")
        try:
            after_restart(client2)
        finally:
            server2.shutdown()
            server2.server_close()
            thread2.join(timeout=5)
    finally:
        if thread.is_alive():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    print(f"\n验收回放完成：{sum(CHECKS)}/{len(CHECKS)} 项检查通过。")


def replay(client: Client):
    section("1. 初始登记")
    status, v1 = client.req("POST", "/parcels", {
        "parcel_code": "TD-2026-0001",
        "location": "青山镇白云村三组",
        "area_mu": 120.5,
        "boundary": ["东至山脊", "南至小河", "西至机耕道", "北至国有林场"],
        "members": [
            {"name": "张大山", "id_number": "110101196001011234",
             "phone": "13800001111", "share": 0.6},
            {"name": "李秀兰", "id_number": "110101196203024321",
             "phone": "13800002222", "share": 0.4},
        ],
        "rights": [{"right_type": "承包经营权", "holder_name": "白云村三组",
                    "term_start": "2026-01-01", "term_end": "2056-12-31"}],
        "occurred_at": "2026-01-10",
        "evidence": {"documents": [{"type": "承包合同", "ref": "HT-2026-001"}],
                     "note": "第一批经营权证底账"},
    }, token=CLERK_A)
    parcel_id = v1["parcel_id"]
    check(status == 201 and v1["version_no"] == 1, "登记成功，生成第 1 版",
          f"parcel_id={parcel_id}")

    section("2. 三次变更：成员增加 → 边界调整 → 局部流转")
    _, parcel = client.req("GET", f"/parcels/{parcel_id}", token=CLERK_A)
    members = [m["member_id"] for m in parcel["state"]["members"]]
    _, v2 = client.req("POST", f"/parcels/{parcel_id}/changes", {
        "base_version": 1, "change_type": "member_change", "occurred_at": "2026-02-10",
        "payload": {"add": [{"name": "王二", "id_number": "110101199001011111",
                             "phone": "13800003333", "share": 0.1}]},
        "evidence": {"documents": [{"type": "户籍迁入证明", "ref": "HJ-2026-02"}]},
    }, token=CLERK_A)
    check(v2["version_no"] == 2, "成员增加", v2["summary"])

    _, parcel = client.req("GET", f"/parcels/{parcel_id}", token=CLERK_A)
    members = [m["member_id"] for m in parcel["state"]["members"]]
    _, v3 = client.req("POST", f"/parcels/{parcel_id}/changes", {
        "base_version": 2, "change_type": "boundary_adjust", "occurred_at": "2026-03-05",
        "payload": {"boundary": ["东至山脊", "南至小河改道", "西至机耕道", "北至国有林场"],
                    "area_mu": 118.0},
        "evidence": {"documents": [{"type": "村民会议记录", "ref": "HY-2026-03"}],
                     "confirmed_by": members},
    }, token=CLERK_B)
    check(v3["version_no"] == 3, "边界调整（全体共有人确认）", v3["summary"])

    _, v4 = client.req("POST", f"/parcels/{parcel_id}/changes", {
        "base_version": 3, "change_type": "partial_transfer", "occurred_at": "2026-04-01",
        "payload": {"right_type": "林地经营权", "holder_name": "绿源合作社",
                    "scope_area_mu": 30, "term_start": "2026-04-01", "term_end": "2036-03-31"},
        "evidence": {"documents": [{"type": "流转合同", "ref": "LZ-2026-01"}]},
    }, token=CLERK_A)
    check(v4["version_no"] == 4, "局部流转", v4["summary"])

    section("3. 签发旧证 + 迟到补录")
    _, v5 = client.req("POST", f"/parcels/{parcel_id}/certificates",
                       {"base_version": 4, "occurred_at": "2026-04-15"}, token=CLERK_A)
    old_cert = v5["snapshot"]["certificates"][-1]
    check(v5["version_no"] == 5, "旧证签发", f"证号 {old_cert['cert_no']}")

    _, v6 = client.req("POST", f"/parcels/{parcel_id}/changes", {
        "base_version": 5, "change_type": "member_change", "occurred_at": "2026-01-25",
        "payload": {"add": [{"name": "孙五", "id_number": "110101198805053333"}]},
        "evidence": {"documents": [{"type": "补录说明", "ref": "BL-2026-01"}],
                     "note": "纸质材料迟到补录"},
    }, token=CLERK_B)
    check(v6["version_no"] == 6 and v6["is_backfill"], "迟到补录入链", "标记 is_backfill")
    _, as_of = client.req("GET", f"/parcels/{parcel_id}/as-of?at=2026-02-01&basis=occurred",
                          token=CLERK_A)
    names = [m["name"] for m in as_of["snapshot"]["members"]]
    check("孙五" in names and "王二" not in names,
          "按业务时间重建 2026-02-01 状态：补录生效、后来的变更未提前带入",
          f"基于版本 {as_of['based_on_versions']}")
    at = urllib.parse.quote(v5["recorded_at"])
    _, recorded = client.req("GET", f"/parcels/{parcel_id}/as-of?at={at}&basis=recorded",
                             token=CLERK_A)
    check(recorded["version_no"] == 5, "按记录时间回看：当时有效版本仍是第 5 版（未被补录覆盖）")

    section("4. 两名经办人并发修订 —— 检查阻断理由")
    barrier = threading.Barrier(2)
    race = {}

    def submit(name, token):
        def run():
            barrier.wait()
            race[name] = client.req("POST", f"/parcels/{parcel_id}/changes", {
                "base_version": 6, "change_type": "member_change",
                "occurred_at": "2026-05-01",
                "payload": {"add": [{"name": f"成员{name}",
                                     "id_number": "110101199501014444"}]},
                "evidence": {"note": f"经办 {name} 修订"},
            }, token=token)
        return run

    threads = [threading.Thread(target=submit("甲", CLERK_A)),
               threading.Thread(target=submit("乙", CLERK_B))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(race[k][0] for k in race)
    check(statuses == [201, 409], "并发提交恰有一笔成功、一笔被阻断", f"状态 {statuses}")
    blocked = [race[k][1] for k in race if race[k][0] == 409][0]
    check(blocked["error"]["code"] == "VERSION_CONFLICT",
          "阻断理由", blocked["error"]["message"])

    section("5. 旧证核验与新证签发同时发生 —— 检查返回版本")
    barrier2 = threading.Barrier(2)
    phase = {}

    def verify_old():
        barrier2.wait()
        phase["verify"] = client.req("POST", "/verify",
                                     {"cert_no": old_cert["cert_no"]}, token=BANK)

    def issue_new():
        barrier2.wait()
        phase["issue"] = client.req("POST", f"/parcels/{parcel_id}/certificates",
                                    {"base_version": 7, "occurred_at": "2026-05-20"},
                                    token=CLERK_A)

    threads = [threading.Thread(target=verify_old), threading.Thread(target=issue_new)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    new_version = phase["issue"][1]
    check(phase["issue"][0] == 201 and new_version["version_no"] == 8,
          "新证签发成功", f"第 {new_version['version_no']} 版")
    verify = phase["verify"][1]
    consistent = (
        (verify["status"] == "valid" and verify["current_version"] == 7)
        or (verify["status"] == "superseded" and verify["current_version"] == 8)
    )
    check(consistent, "核验返回版本自洽",
          f"status={verify['status']} issued_version={verify['issued_version']} "
          f"current_version={verify['current_version']}")
    _, old_now = client.req("POST", "/verify", {"cert_no": old_cert["cert_no"]}, token=BANK)
    check(old_now["status"] == "superseded" and old_now["current_version"] == 8,
          "旧证核验：已取代，返回签发版本与当前版本",
          f"issued_version={old_now['issued_version']} current_version={old_now['current_version']}")

    section("6. 完整审计回放")
    _, audit = client.req("GET", f"/parcels/{parcel_id}/audit", token=REVIEWER)
    entries = audit["audit"]
    for e in entries:
        print(f"    #{e['seq']:>2} {e['action']:<16} 经办={e['actor']:<10} {json.dumps(e['detail'], ensure_ascii=False)}")
    committed = [e for e in entries if e["action"] == "change_committed"]
    blocked_entries = [e for e in entries if e["action"] == "change_blocked"]
    verifies = [e for e in entries if e["action"] == "verify"]
    check(len(committed) == 8, "8 次提交全部留痕")
    check(len(blocked_entries) == 1
          and blocked_entries[0]["detail"]["reason_code"] == "VERSION_CONFLICT",
          "阻断留痕含理由")
    check(len(verifies) >= 2, "机构核验留痕")
    chained = all(curr["prev_hash"] == prev["hash"] for prev, curr in zip(entries, entries[1:]))
    check(chained, "审计哈希链连续")

    section("7. 村集体视角：边界调整确认记录（脱敏）")
    _, boundary_version = client.req("GET", f"/parcels/{parcel_id}/versions/3", token=VILLAGE)
    confirmed = boundary_version["evidence"]["confirmed_by"]
    member_ids = [m["member_id"] for m in boundary_version["snapshot"]["members"]]
    check(set(confirmed) == set(member_ids),
          "边界调整经全体相关人确认，村集体可当场说明",
          f"确认人 {len(confirmed)} 人")
    masked = boundary_version["snapshot"]["members"][0]["id_number"]
    check("*" in masked, "敏感身份信息已按角色脱敏", masked)

    section("8. 复核队列（重启前）")
    _, pending = client.req("GET", "/reviews?status=pending", token=REVIEWER)
    check(len(pending["reviews"]) == 8, "8 项复核任务待办",
          "、".join(r["review_id"] for r in pending["reviews"][:3]) + " …")
    replay.pending_ids = [r["review_id"] for r in pending["reviews"]]
    replay.parcel_id = parcel_id
    replay.old_cert_no = old_cert["cert_no"]


def after_restart(client: Client):
    _, pending = client.req("GET", "/reviews?status=pending", token=REVIEWER)
    check([r["review_id"] for r in pending["reviews"]] == replay.pending_ids,
          "恢复服务后未完成复核仍在原队列（顺序一致）",
          f"{len(pending['reviews'])} 项")
    _, parcel = client.req("GET", f"/parcels/{replay.parcel_id}", token=REVIEWER)
    check(parcel["current_version"] == 8, "版本时间线完整恢复", "当前第 8 版")
    _, old = client.req("POST", "/verify", {"cert_no": replay.old_cert_no}, token=BANK)
    check(old["status"] == "superseded", "证照状态恢复一致")
    _, integrity = client.req("GET", f"/parcels/{replay.parcel_id}/integrity", token=REVIEWER)
    check(integrity["ok"] and integrity["versions_checked"] == 8,
          "版本哈希链校验通过（不可覆盖）")
    for item in pending["reviews"]:
        client.req("POST", f"/reviews/{item['review_id']}/decision",
                   {"decision": "approved", "note": "验收复核"}, token=REVIEWER)
    _, left = client.req("GET", "/reviews?status=pending", token=REVIEWER)
    check(left["reviews"] == [], "恢复后可继续完成复核")


if __name__ == "__main__":
    main()
