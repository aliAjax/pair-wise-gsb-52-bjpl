"""依据链测试：计划版本 -> 同意 -> 服务履约 -> 复查。

覆盖：
- 服务时长调整后未确认服务失效并重算，已确认保留快照，复查结论标记过期；
- 两个老师并发提交同一计划，后到者先看到新依据；
- 批次写入失败后用完整批次恢复，只补未完成记录，不重复累计分钟；
- 统计与审计展示每条记录采用的版本。
"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'student_id': 'S-200', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 0, 'review_due_days': 15, 'goals_count': 4, 'consent': False}


class BasisChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.cm = Actor("cm-1", "case_manager")
        self.sp = Actor("sp-1", "specialist")
        self.admin = Actor("admin-1", "administrator")
        self.parent = Actor("parent-1", "parent_rep")

    def tearDown(self):
        self.temp.cleanup()

    def _activate_plan(self, reference="IEP-29001", data=None):
        record = self.service.create(self.cm, reference, data or CREATE_DATA)
        record = self.service.act(self.parent, record["id"], record["version"], "consent",
                                  {'guardian_confirmed': True, 'consent_scope': '言语训练'})
        record = self.service.act(self.cm, record["id"], record["version"], "activate", {})
        return record

    def test_duration_change_voids_pending_keeps_confirmed_snapshot(self):
        record = self._activate_plan()
        # 老师A登记两条服务（待确认），第一条带稳定ref以便单独确认。
        record = self.service.act(self.sp, record["id"], record["version"], "log_service",
                                  {'session_minutes': 100, 'provider': 'SP-1', 'ref': 'sess-1'})
        record = self.service.act(self.sp, record["id"], record["version"], "log_service",
                                  {'session_minutes': 50, 'provider': 'SP-2', 'ref': 'sess-2'})
        # 确认第一条：固化 v1 快照；第二条保持待确认。
        record = self.service.act(self.cm, record["id"], record["version"], "confirm_services",
                                  {'service_refs': ['sess-1']})
        self.assertEqual(record["payload"]["confirmed_minutes"], 100)
        self.assertEqual(record["payload"]["pending_minutes"], 50)

        # 复查一次，结论基于 v1（150/600 未达标）。
        record = self.service.act(self.admin, record["id"], record["version"], "review",
                                  {'progress_note': 'v1复查'})
        review = record["payload"]["reviews"][-1]
        self.assertEqual(review["plan_version"], 1)
        self.assertEqual(review["conclusion"], "未达标")

        # 老师调整服务时长 600 -> 300。
        record = self.service.act(self.cm, record["id"], record["version"], "adjust_plan",
                                  {'service_minutes': 300, 'reason': '学期缩短'})
        self.assertEqual(record["state"], "under_review")
        payload = record["payload"]
        self.assertEqual(payload["plan_version"], 2)
        # 未确认的50分钟失效；已确认的100分钟保留快照。
        self.assertEqual(payload["pending_minutes"], 0)
        self.assertEqual(payload["voided_minutes"], 50)
        self.assertEqual(payload["confirmed_minutes"], 100)
        self.assertEqual(payload["delivered_minutes"], 100)
        confirmed = [e for e in payload["services"] if e["status"] == "confirmed"][0]
        self.assertEqual(confirmed["confirmed_basis_version"], 1)
        self.assertEqual(confirmed["confirmed_basis_minutes"], 600)
        voided = [e for e in payload["services"] if e["status"] == "void"][0]
        self.assertEqual(voided["voided_at_version"], 2)
        # 旧复查仍可见，但已过期。
        self.assertEqual(payload["reviews"][-1]["status"], "stale")
        self.assertEqual(payload["review_status"], "stale")

        # 旧额度300... 实际上新额度300：再录250分钟会超，录150分钟可以。
        with self.assertRaises(ValidationError):
            self.service.act(self.sp, record["id"], record["version"], "log_service",
                             {'session_minutes': 250, 'provider': 'SP-3'})
        record = self.service.act(self.sp, record["id"], record["version"], "log_service",
                                  {'session_minutes': 150, 'provider': 'SP-3'})
        self.assertEqual(record["payload"]["pending_minutes"], 150)

        # 依据过期的复查不能用于结束计划。
        with self.assertRaises(ValidationError):
            self.service.act(self.admin, record["id"], record["version"], "close", {'review_complete': True})
        # 用新依据复查：250/300 仍未达标；补足后再复查应为达标。
        record = self.service.act(self.sp, record["id"], record["version"], "log_service",
                                  {'session_minutes': 50, 'provider': 'SP-3'})
        record = self.service.act(self.cm, record["id"], record["version"], "confirm_services", {})
        # 100(确认快照v1) + 200(v2待确认/已确认) = 300
        record = self.service.act(self.admin, record["id"], record["version"], "recheck",
                                  {'progress_note': 'v2复查'})
        new_review = record["payload"]["reviews"][-1]
        self.assertEqual(new_review["plan_version"], 2)
        self.assertEqual(new_review["conclusion"], "达标")
        self.assertEqual(new_review["status"], "current")
        self.assertEqual(record["payload"]["reviews"][0]["status"], "stale")

        record = self.service.act(self.admin, record["id"], record["version"], "close",
                                  {'review_complete': True})
        self.assertEqual(record["state"], "closed")

    def test_concurrent_teachers_late_writer_sees_new_basis(self):
        record_a = self._activate_plan("IEP-29002")
        record_b = self.service.get_record(self.sp, record_a["id"])
        self.assertEqual(record_a["version"], record_b["version"])

        # 老师甲先调整时长。
        updated = self.service.act(self.cm, record_a["id"], record_a["version"], "adjust_plan",
                                   {'service_minutes': 800, 'reason': '增补课时'})
        self.assertEqual(updated["payload"]["plan_version"], 2)

        # 老师乙仍持旧 record_version=3 提交服务，冲突并拿到新依据。
        with self.assertRaises(Conflict) as caught:
            self.service.act(self.sp, record_b["id"], record_b["version"], "log_service",
                             {'session_minutes': 60, 'provider': 'SP-9'})
        self.assertEqual(caught.exception.details["expected_version"], updated["version"])
        self.assertEqual(caught.exception.details["plan_version"], 2)
        self.assertEqual(caught.exception.details["current_service_minutes"], 800)

    def test_batch_recovery_completes_only_missing_items(self):
        record = self._activate_plan("IEP-29003")
        items = [
            {'ref': 's1', 'session_minutes': 60, 'provider': 'SP-1'},
            {'ref': 's2', 'session_minutes': 70, 'provider': 'SP-1'},
            {'ref': 's3', 'session_minutes': 80, 'provider': 'SP-2'},
        ]
        original_act = self.service.act
        calls = {'n': 0}

        def flaky_act(actor, record_id, expected_version, action, data):
            if action == 'log_service':
                calls['n'] += 1
                if calls['n'] == 2:
                    # 第二条写入后进程崩溃：条目已落库，但批次状态未推进。
                    result = original_act(actor, record_id, expected_version, action, data)
                    raise RuntimeError("simulated crash after write")
            return original_act(actor, record_id, expected_version, action, data)

        self.service.act = flaky_act
        with self.assertRaises(RuntimeError):
            self.service.submit_service_batch(self.sp, record["id"], "batch-1", record["version"], items)
        self.service.act = original_act

        # 崩溃现场：第一条可能未落库（崩溃点在第2条调用前），检查批次为失败态。
        batch = self.service.get_batch(self.sp, "batch-1")
        self.assertIn(batch["state"], {"failed", "processing"})

        # 用完整批次恢复。
        fresh = self.service.get_record(self.sp, record["id"])
        result = self.service.submit_service_batch(self.sp, record["id"], "batch-1", fresh["version"], items)
        self.assertTrue(result["completed"])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["applied_items"], 3)

        final = self.service.get_record(self.sp, record["id"])
        minutes = sum(e["minutes"] for e in final["payload"]["services"])
        self.assertEqual(minutes, 210)  # 60+70+80，没有重复累计

        # 再次用同一完整批次重放：零新增。
        result2 = self.service.submit_service_batch(self.sp, record["id"], "batch-1", final["version"], items)
        self.assertTrue(result2["completed"])
        self.assertEqual(result2["applied_items"], 3)
        final2 = self.service.get_record(self.sp, record["id"])
        self.assertEqual(sum(e["minutes"] for e in final2["payload"]["services"]), 210)

    def test_concurrent_batch_same_batch_id_is_idempotent(self):
        record = self._activate_plan("IEP-29004")
        items = [{'ref': 's1', 'session_minutes': 60, 'provider': 'SP-1'},
                 {'ref': 's2', 'session_minutes': 90, 'provider': 'SP-2'}]
        results = []
        errors = []

        def worker():
            try:
                results.append(self.service.submit_service_batch(
                    self.sp, record["id"], "batch-same", record["version"], items))
            except Exception as exc:  # noqa: BLE001 - 并发下只允许一个批次建单成功
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 无论谁先谁后，最终服务分钟只能累计一次。
        final = self.service.get_record(self.sp, record["id"])
        self.assertEqual(sum(e["minutes"] for e in final["payload"]["services"]), 150)
        self.assertEqual(len(results) + (1 if errors else 0), 2)

    def test_audit_and_stats_show_basis_versions(self):
        record = self._activate_plan("IEP-29005")
        record = self.service.act(self.sp, record["id"], record["version"], "log_service",
                                  {'session_minutes': 120, 'provider': 'SP-1'})
        record = self.service.act(self.cm, record["id"], record["version"], "confirm_services", {})
        record = self.service.act(self.admin, record["id"], record["version"], "review",
                                  {'progress_note': '阶段复查'})

        timeline = self.service.timeline(self.cm, record["id"])
        bases = {(event["action"], event["details"]["basis"]["plan_version"])
                 for event in timeline if "basis" in event["details"]}
        self.assertIn(("consent", 1), bases)
        self.assertIn(("log_service", 1), bases)
        self.assertIn(("confirm_services", 1), bases)
        self.assertIn(("review", 1), bases)
        review_event = [e for e in timeline if e["action"] == "review"][0]
        self.assertEqual(review_event["details"]["event"]["review"]["plan_version"], 1)

        stats = self.service.stats(self.cm)
        basis = next(item for item in stats["records"] if item["reference"] == "IEP-29005")
        self.assertEqual(basis["plan_version"], 1)
        self.assertEqual(basis["consent_basis_version"], 1)
        self.assertEqual(basis["latest_review_version"], 1)
        self.assertEqual(basis["services"]["confirmed"], 1)
        self.assertEqual(basis["services"]["versions"], [1])
        self.assertIn("states", stats)
        self.assertIn("plan_versions", stats)


if __name__ == '__main__':
    unittest.main()
