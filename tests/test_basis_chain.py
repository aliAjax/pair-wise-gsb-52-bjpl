import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
CASE_MANAGER = Actor("case-manager-1", "case_manager")
SPECIALIST = Actor("specialist-1", "specialist")
PARENT = Actor("parent-1", "parent_rep")
ADMIN = Actor("admin-1", "administrator")


class BasisChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference="IEP-90001", data=None):
        return self.service.create(CASE_MANAGER, reference, data or dict(CREATE_DATA))

    def _active_record(self, reference="IEP-90001", data=None):
        record = self._create(reference, data)
        record = self.service.act(PARENT, record["id"], record["version"], "consent", {"guardian_confirmed": True, "consent_scope": "个别化服务"})
        return self.service.act(CASE_MANAGER, record["id"], record["version"], "activate", {})

    def _log_batch(self, record, batch_id, entries):
        return self.service.act(SPECIALIST, record["id"], record["version"], "log_service", {"batch_id": batch_id, "entries": entries})

    def _review(self, record, note="阶段复盘"):
        return self.service.act(ADMIN, record["id"], record["version"], "review", {"progress_note": note})

    def _amend(self, record, service_minutes):
        return self.service.act(CASE_MANAGER, record["id"], record["version"], "amend", {"amendment_reason": "调整服务时长", "updated_goals": ["目标A"], "service_minutes": service_minutes})

    @staticmethod
    def _entries(record):
        return {item["entry_id"]: item for item in record["payload"]["service_log"]}

    def test_basis_change_invalidates_unconfirmed_and_keeps_confirmed_snapshot(self):
        record = self._active_record()
        record = self._log_batch(record, "B-1", [
            {"entry_id": "E-1", "session_minutes": 100, "provider": "SP-1"},
            {"entry_id": "E-2", "session_minutes": 100, "provider": "SP-1"},
            {"entry_id": "E-3", "session_minutes": 100, "provider": "SP-1"},
        ])
        self.assertEqual(record["payload"]["delivered_minutes"], 420)
        record = self.service.act(CASE_MANAGER, record["id"], record["version"], "confirm_service", {"entry_ids": ["E-1"]})
        record = self._review(record)
        record = self._amend(record, 250)
        payload = record["payload"]
        self.assertEqual(payload["basis_version"], 2)
        entries = self._entries(record)
        # 已确认记录保留当时快照：版本、额度与分钟数不变
        self.assertTrue(entries["E-1"]["confirmed"])
        self.assertEqual(entries["E-1"]["status"], "valid")
        self.assertEqual(entries["E-1"]["basis_version"], 1)
        self.assertEqual(entries["E-1"]["quota"], 600)
        # 未确认记录失效：基线120+已确认100=220，新额度250放不下每条100分钟
        self.assertEqual(entries["E-2"]["status"], "invalid")
        self.assertEqual(entries["E-3"]["status"], "invalid")
        self.assertEqual(payload["delivered_minutes"], 220)
        self.assertEqual(payload["missing_minutes"], 30)
        # 旧复查结论标记失效，不再当作当前结论
        self.assertTrue(payload["review_stale"])
        self.assertEqual(payload["review_conclusion"]["basis_version"], 1)
        self.assertEqual(payload["superseded_review"]["basis_version"], 1)

    def test_unconfirmed_entries_recalculated_when_quota_grows(self):
        record = self._active_record()
        record = self._log_batch(record, "B-1", [
            {"entry_id": "E-1", "session_minutes": 100, "provider": "SP-1"},
            {"entry_id": "E-2", "session_minutes": 100, "provider": "SP-1"},
            {"entry_id": "E-3", "session_minutes": 100, "provider": "SP-1"},
        ])
        record = self._review(record)
        record = self._amend(record, 250)
        entries = self._entries(record)
        self.assertEqual(entries["E-1"]["status"], "valid")
        self.assertEqual(entries["E-1"]["basis_version"], 2)
        self.assertEqual(entries["E-2"]["status"], "invalid")
        self.assertEqual(record["payload"]["delivered_minutes"], 220)
        record = self._review(record, "第二次复查")
        record = self._amend(record, 1000)
        payload = record["payload"]
        self.assertEqual(payload["basis_version"], 3)
        self.assertEqual(payload["delivered_minutes"], 420)
        entries = self._entries(record)
        for entry_id in ("E-1", "E-2", "E-3"):
            self.assertEqual(entries[entry_id]["status"], "valid")
            self.assertEqual(entries[entry_id]["basis_version"], 3)
            self.assertEqual(entries[entry_id]["quota"], 1000)

    def test_batch_retry_only_completes_missing_entries(self):
        record = self._active_record()
        record = self._log_batch(record, "B-1", [
            {"entry_id": "E-1", "session_minutes": 60, "provider": "SP-1"},
            {"entry_id": "E-2", "session_minutes": 60, "provider": "SP-1"},
        ])
        self.assertEqual(record["payload"]["delivered_minutes"], 240)
        # 写入失败后从完整批次恢复：重交包含已完成记录的整个批次
        record = self._log_batch(record, "B-1", [
            {"entry_id": "E-1", "session_minutes": 60, "provider": "SP-1"},
            {"entry_id": "E-2", "session_minutes": 60, "provider": "SP-1"},
            {"entry_id": "E-3", "session_minutes": 60, "provider": "SP-1"},
            {"entry_id": "E-4", "session_minutes": 60, "provider": "SP-1"},
        ])
        payload = record["payload"]
        # 只补未完成记录，服务分钟不重复累计
        self.assertEqual(payload["delivered_minutes"], 360)
        self.assertEqual(len(payload["service_log"]), 4)
        timeline = self.service.timeline(CASE_MANAGER, record["id"])
        second_log = [event for event in timeline if event["action"] == "log_service"][-1]
        self.assertEqual(second_log["details"]["applied_entries"], ["E-3", "E-4"])
        self.assertEqual(second_log["details"]["skipped_duplicates"], ["E-1", "E-2"])

    def test_latecomer_sees_new_basis_on_conflict(self):
        record = self._active_record()
        record = self._review(record)
        stale_version = record["version"]
        # 两个老师同时基于同一版本提交修订，先到者生效
        first = self._amend(record, 300)
        self.assertEqual(first["payload"]["basis_version"], 2)
        with self.assertRaises(Conflict) as ctx:
            self.service.act(CASE_MANAGER, record["id"], stale_version, "amend", {"amendment_reason": "另一位老师的修订", "updated_goals": ["目标B"], "service_minutes": 400})
        # 后到者立即看到新依据
        details = ctx.exception.details
        self.assertEqual(details["current_version"], first["version"])
        self.assertEqual(details["current_basis_version"], 2)
        self.assertEqual(details["current_service_minutes"], 300)

    def test_version_race_on_log_service_reports_current_basis(self):
        record = self._active_record()
        stale_version = record["version"]
        first = self.service.act(SPECIALIST, record["id"], stale_version, "log_service", {"entry_id": "E-1", "session_minutes": 30, "provider": "SP-1"})
        with self.assertRaises(Conflict) as ctx:
            self.service.act(SPECIALIST, record["id"], stale_version, "log_service", {"entry_id": "E-2", "session_minutes": 40, "provider": "SP-2"})
        details = ctx.exception.details
        self.assertEqual(details["current_version"], first["version"])
        self.assertEqual(details["current_basis_version"], 1)
        self.assertEqual(details["current_service_minutes"], 600)

    def test_stats_and_audit_show_adopted_versions(self):
        record = self._active_record("IEP-90001")
        record = self._log_batch(record, "B-1", [{"entry_id": "E-1", "session_minutes": 60, "provider": "SP-1"}])
        record = self._review(record)
        record = self._amend(record, 500)
        other = self._create("IEP-90002", dict(CREATE_DATA, student_id="S-200"))
        stats = self.service.stats(CASE_MANAGER)
        by_id = {item["id"]: item for item in stats["records"]}
        self.assertEqual(by_id[record["id"]]["basis_version"], 2)
        self.assertEqual(by_id[other["id"]]["basis_version"], 1)
        self.assertEqual(stats["basis_versions"], {"1": 1, "2": 1})
        self.assertEqual(stats["states"], {"active": 1, "draft": 1})
        timeline = self.service.timeline(CASE_MANAGER, record["id"])
        self.assertTrue(all("basis_version" in event["details"] for event in timeline))
        self.assertEqual(timeline[0]["details"]["basis_version"], 1)
        self.assertEqual(timeline[-1]["details"]["basis_version"], 2)

    def test_confirm_rejects_unknown_or_invalid_entries(self):
        record = self._active_record()
        record = self._log_batch(record, "B-1", [
            {"entry_id": "E-1", "session_minutes": 100, "provider": "SP-1"},
            {"entry_id": "E-2", "session_minutes": 100, "provider": "SP-1"},
        ])
        record = self._review(record)
        record = self._amend(record, 250)
        with self.assertRaises(ValidationError):
            self.service.act(CASE_MANAGER, record["id"], record["version"], "confirm_service", {"entry_ids": ["E-2"]})
        with self.assertRaises(ValidationError):
            self.service.act(CASE_MANAGER, record["id"], record["version"], "confirm_service", {"entry_ids": ["E-9"]})

    def test_over_quota_batch_is_atomic(self):
        record = self._active_record()
        with self.assertRaises(ValidationError):
            self._log_batch(record, "B-1", [
                {"entry_id": "E-1", "session_minutes": 400, "provider": "SP-1"},
                {"entry_id": "E-2", "session_minutes": 200, "provider": "SP-1"},
            ])
        fresh = self.service.get_record(CASE_MANAGER, record["id"])
        self.assertEqual(fresh["payload"]["delivered_minutes"], 120)
        self.assertEqual(fresh["payload"]["service_log"], [])
