from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from discovery_lab.clock import FrozenClock
from discovery_lab.errors import Conflict, Forbidden, InvalidState, NotFound
from discovery_lab.jsonio import load_json
from discovery_lab.service import TaxonomyLabService
from discovery_lab.storage import connect


ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("stat-a", "statistician"),
            ("stat-b", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", self.evidence_protocol)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)

    def tearDown(self) -> None:
        self.connection.close()

    def test_complete_workflow(self) -> None:
        imported = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(imported["inserted"], 6)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("stat", 30)
        analysis = self.service.complete_job("stat", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "满足规则")
        report = self.service.report("auditor", "batch-a")
        self.assertEqual(report["batch"]["state"], "decided")
        self.assertEqual(report["analysis"]["result"]["conclusion"], "pass")

    def test_idempotent_replay_and_conflict(self) -> None:
        first = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        second = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(first, second)
        changed = [dict(item) for item in self.rows]
        changed[0] = dict(changed[0])
        changed[0]["indicators"] = dict(changed[0]["indicators"])
        changed[0]["indicators"]["completion_seconds"] = "99"
        with self.assertRaises(Conflict):
            self.service.import_evidence_items("operator", "batch-a", "key-1", changed)
        count = self.connection.execute("SELECT count(*) FROM evidence_items").fetchone()[0]
        self.assertEqual(count, 6)

    def test_import_rolls_back_when_one_source_row_duplicates(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows[:1])
        with self.assertRaises(Conflict):
            self.service.import_evidence_items("operator", "batch-a", "key-2", self.rows[:2])
        count = self.connection.execute("SELECT count(*) FROM evidence_items").fetchone()[0]
        self.assertEqual(count, 1)

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.seal_batch("operator", "batch-a", 2)
        with self.assertRaises(Forbidden):
            self.service.report("operator", "batch-a")

    def test_exclusion_review_and_revoke_leave_history(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        evidence_item_id = self.connection.execute(
            "SELECT evidence_item_id FROM evidence_items ORDER BY evidence_item_id LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", evidence_item_id, "现场记录失效")
        reviewed = self.service.review_exclusion("stat", requested["exclusion_id"], True, "观察材料充分")
        self.assertEqual(reviewed["status"], "approved")
        revoked = self.service.revoke_exclusion("operator", requested["exclusion_id"], "已找回原始记录")
        self.assertEqual(revoked["status"], "revoked")
        events = self.connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='evidence_item' AND entity_id=? ORDER BY event_id",
            (str(evidence_item_id),),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["exclusion.requested", "exclusion.revoked"])

    def test_failed_job_returns_to_queue_after_delay(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("stat-a", 10)
        failed = self.service.fail_job("stat-a", job["job_id"], "临时计算失败", retry_seconds=5)
        self.assertEqual(failed["state"], "queued")
        self.assertIsNone(self.service.claim_job("stat-b", 10))
        self.clock.advance(seconds=5)
        retried = self.service.claim_job("stat-b", 10)
        self.assertEqual(retried["job_id"], job["job_id"])
        self.assertEqual(retried["attempts"], 2)

    def test_lease_can_be_reclaimed_after_expiry(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        first = self.service.claim_job("stat-a", 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("stat-b", 10)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["lease_owner"], "stat-b")
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat-a", first["job_id"], "stat")


class ClaimJobAuthorizationTests(unittest.TestCase):
    """领取分析任务必须在同一事务内确认身份、启用状态与分析角色。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", self.evidence_protocol)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        self.baseline_audit_count = self.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]

    def tearDown(self) -> None:
        self.connection.close()

    def _snapshot_job(self) -> dict:
        return dict(self.connection.execute("SELECT * FROM analysis_jobs").fetchone())

    def _assert_job_untouched(self, before: dict) -> None:
        # 校验失败时任务状态、领取人、租约均不得变化，审计链也不得追加事件。
        self.assertEqual(self._snapshot_job(), before)
        audit_count = self.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
        self.assertEqual(audit_count, self.baseline_audit_count)

    def test_unknown_user_cannot_claim(self) -> None:
        before = self._snapshot_job()
        with self.assertRaises(NotFound):
            self.service.claim_job("ghost", 30)
        self._assert_job_untouched(before)

    def test_disabled_user_cannot_claim(self) -> None:
        self.connection.execute("UPDATE users SET active=0 WHERE user_id='stat'")
        before = self._snapshot_job()
        with self.assertRaises(Forbidden):
            self.service.claim_job("stat", 30)
        self._assert_job_untouched(before)

    def test_unauthorized_role_cannot_claim(self) -> None:
        for reviewer in ("operator", "approver", "auditor"):
            with self.subTest(reviewer=reviewer):
                before = self._snapshot_job()
                with self.assertRaises(Forbidden):
                    self.service.claim_job(reviewer, 30)
                self._assert_job_untouched(before)

    def test_authorized_statistician_can_claim(self) -> None:
        claimed = self.service.claim_job("stat", 30)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["state"], "leased")
        self.assertEqual(claimed["lease_owner"], "stat")
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNotNone(claimed["lease_expires_at"])

    def test_second_claim_while_lease_held_returns_none(self) -> None:
        self.assertIsNotNone(self.service.claim_job("stat", 30))
        self.clock.advance(seconds=5)  # 租约未到期，同一合法用户再次领取也拿不到任务
        self.assertIsNone(self.service.claim_job("stat", 30))

    def test_concurrent_claims_allow_only_one_winner(self) -> None:
        # 使用真实文件连接与两个独立连接，迫使两个线程走真实的 BEGIN IMMEDIATE 锁竞争。
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "concurrent.sqlite3")
            seeding_connection = connect(path)
            try:
                seeding = TaxonomyLabService(seeding_connection, self.clock)
                for user_id, role in (
                    ("operator", "operator"),
                    ("stat-a", "statistician"),
                    ("stat-b", "statistician"),
                ):
                    seeding.create_user(user_id, user_id, role)
                seeding.register_device("operator", "scope-a", "A 型", "厂商")
                seeding.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
                seeding.publish_evidence_protocol("stat-a", self.evidence_protocol)
                seeding.create_batch("operator", "batch-b", "demo-taxonomy-v1", 1, "build-a")
                seeding.start_batch("operator", "batch-b", 1)
                seeding.import_evidence_items("operator", "batch-b", "key-1", self.rows)
                seeding.seal_batch("stat-a", "batch-b", 2)
            finally:
                seeding_connection.close()

            # 连接必须在各自线程内创建（sqlite3 默认禁止跨线程使用连接）。
            outcomes: dict[str, object] = {}
            barrier = threading.Barrier(2)

            def attempt(name: str, bucket: str) -> None:
                connection = connect(path)
                try:
                    service = TaxonomyLabService(connection, self.clock)  # 幂等建表
                    barrier.wait()
                    outcomes[bucket] = service.claim_job(name, 30)
                except Exception as exc:  # noqa: BLE001 - 记录任何异常供断言
                    outcomes[bucket] = exc
                finally:
                    connection.close()

            threads = [
                threading.Thread(target=attempt, args=("stat-a", "a")),
                threading.Thread(target=attempt, args=("stat-b", "b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            verifying = connect(path)
            try:
                final_row = verifying.execute(
                    "SELECT lease_owner,state,attempts FROM analysis_jobs"
                ).fetchone()
            finally:
                verifying.close()

        values = list(outcomes.values())
        self.assertEqual([type(value).__name__ for value in values].count("dict"), 1, outcomes)
        self.assertIn(None, values, outcomes)  # 败者事务内已看不到可领取任务
        self.assertTrue(all(not isinstance(value, Exception) for value in values), outcomes)
        winner = next(value for value in values if isinstance(value, dict))
        self.assertEqual(final_row["state"], "leased")
        self.assertEqual(final_row["attempts"], 1)
        self.assertEqual(final_row["lease_owner"], winner["lease_owner"])
        self.assertIn(final_row["lease_owner"], ("stat-a", "stat-b"))


if __name__ == "__main__":
    unittest.main()
