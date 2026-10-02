from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from discovery_lab.api import JsonApplication
from discovery_lab.clock import FrozenClock
from discovery_lab.errors import Forbidden, NotFound
from discovery_lab.jsonio import load_json
from discovery_lab.service import TaxonomyLabService
from discovery_lab.storage import connect, transaction


ROOT = Path(__file__).resolve().parents[1]
MOMENT = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


def seed_claimable_job(service: TaxonomyLabService) -> None:
    """登记用户、协议与批次，封存后产生一个可领取的分析工作项。"""
    for user_id, role in (
        ("operator", "operator"),
        ("stat", "statistician"),
        ("stat-b", "statistician"),
        ("approver", "approver"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
    service.register_device("operator", "scope-a", "A 型", "厂商")
    service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
    service.publish_evidence_protocol("stat", evidence_protocol)
    service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
    service.start_batch("operator", "batch-a", 1)
    service.seal_batch("stat", "batch-a", 2)


class ClaimJobAuthorizationTests(unittest.TestCase):
    """领取前必须在同一事务内校验操作者存在、启用且角色允许执行分析。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(MOMENT)
        self.service = TaxonomyLabService(self.connection, self.clock)
        seed_claimable_job(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def _queue_snapshot(self) -> tuple[dict[str, object], int]:
        job = dict(self.connection.execute("SELECT * FROM analysis_jobs").fetchone())
        audits = self.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
        return job, audits

    def _assert_queue_unchanged(self, snapshot: tuple[dict[str, object], int]) -> None:
        self.assertEqual(self._queue_snapshot(), snapshot)

    def test_unknown_user_cannot_claim(self) -> None:
        snapshot = self._queue_snapshot()
        self.clock.advance(seconds=1)
        with self.assertRaises(NotFound) as caught:
            self.service.claim_job("ghost", 30)
        self.assertIn("ghost", str(caught.exception))
        self._assert_queue_unchanged(snapshot)

    def test_deactivated_user_cannot_claim(self) -> None:
        with transaction(self.connection, immediate=True):
            self.connection.execute("UPDATE users SET active=0 WHERE user_id='stat'")
        snapshot = self._queue_snapshot()
        self.clock.advance(seconds=1)
        with self.assertRaises(Forbidden) as caught:
            self.service.claim_job("stat", 30)
        self.assertIn("停用", str(caught.exception))
        self._assert_queue_unchanged(snapshot)

    def test_review_only_and_other_roles_cannot_claim(self) -> None:
        snapshot = self._queue_snapshot()
        self.clock.advance(seconds=1)
        for user_id in ("auditor", "approver", "operator"):
            with self.assertRaises(Forbidden):
                self.service.claim_job(user_id, 30)
        self._assert_queue_unchanged(snapshot)

    def test_statistician_claims_job_and_queue_drains(self) -> None:
        job = self.service.claim_job("stat", 30)
        self.assertIsNotNone(job)
        self.assertEqual(job["state"], "leased")
        self.assertEqual(job["lease_owner"], "stat")
        self.assertEqual(job["attempts"], 1)
        self.assertIsNone(self.service.claim_job("stat-b", 30))


class ClaimJobApiTests(unittest.TestCase):
    """HTTP 层对领取失败返回一致的业务错误结构。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TaxonomyLabService(self.connection, FrozenClock(MOMENT))
        seed_claimable_job(self.service)
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def _claim(self, worker_id: str):
        payload = json.dumps({"worker_id": worker_id, "lease_seconds": 30}).encode()
        return self.app.handle("POST", "/jobs/claim", body=payload)

    def test_unknown_user_error_shape(self) -> None:
        response = self._claim("ghost")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")

    def test_deactivated_user_error_shape(self) -> None:
        with transaction(self.connection, immediate=True):
            self.connection.execute("UPDATE users SET active=0 WHERE user_id='stat'")
        response = self._claim("stat")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_review_only_role_error_shape(self) -> None:
        response = self._claim("auditor")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_legitimate_claim_response(self) -> None:
        response = self._claim("stat")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["job"]["lease_owner"], "stat")
        self.assertEqual(response.body["job"]["state"], "leased")


class ClaimJobConcurrencyTests(unittest.TestCase):
    """并发领取同一工作项时最多只有一个请求成功。"""

    def test_concurrent_claims_have_single_winner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="claim-race-") as temporary:
            database = Path(temporary) / "claim.sqlite3"
            seed_connection = connect(database)
            try:
                seed_claimable_job(TaxonomyLabService(seed_connection, FrozenClock(MOMENT)))
            finally:
                seed_connection.close()

            barrier = threading.Barrier(2)
            results: list[object] = [None, None]
            errors: list[BaseException] = []

            def claim(index: int, worker_id: str) -> None:
                connection = None
                try:
                    connection = connect(database)
                    service = TaxonomyLabService(connection, FrozenClock(MOMENT))
                    barrier.wait(timeout=10)
                    results[index] = service.claim_job(worker_id, 30)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    if connection is not None:
                        connection.close()

            threads = [
                threading.Thread(target=claim, args=(0, "stat")),
                threading.Thread(target=claim, args=(1, "stat-b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            claimed = [result for result in results if result is not None]
            self.assertEqual(len(claimed), 1)

            verify = connect(database)
            try:
                row = verify.execute("SELECT * FROM analysis_jobs").fetchone()
                self.assertEqual(row["state"], "leased")
                self.assertEqual(row["attempts"], 1)
                self.assertEqual(row["lease_owner"], claimed[0]["lease_owner"])
            finally:
                verify.close()


if __name__ == "__main__":
    unittest.main()
