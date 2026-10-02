from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from discovery_lab.api import JsonApplication
from discovery_lab.clock import FrozenClock
from discovery_lab.jsonio import load_json
from discovery_lab.service import TaxonomyLabService


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TaxonomyLabService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")


class ClaimJobApiTests(unittest.TestCase):
    """领取端点必须把身份、启用状态与角色校验映射为一致的业务错误。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, clock)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", protocol)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "key-1", rows)
        self.service.seal_batch("stat", "batch-a", 2)

    def tearDown(self) -> None:
        self.connection.close()

    def _claim(self, worker_id: str):
        body = json.dumps({"worker_id": worker_id, "lease_seconds": 30}).encode()
        return self.app.handle("POST", "/jobs/claim", body=body)

    def test_unknown_user_is_not_found(self) -> None:
        response = self._claim("ghost")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")
        self.assertTrue(response.body["error"]["message"])

    def test_disabled_user_is_forbidden(self) -> None:
        self.connection.execute("UPDATE users SET active=0 WHERE user_id='stat'")
        response = self._claim("stat")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")
        self.assertIn("停用", response.body["error"]["message"])

    def test_read_only_roles_are_forbidden(self) -> None:
        for reviewer in ("operator", "approver", "auditor"):
            with self.subTest(reviewer=reviewer):
                response = self._claim(reviewer)
                self.assertEqual(response.status, 403)
                self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_rejected_claim_leaves_job_untouched(self) -> None:
        before = dict(self.connection.execute("SELECT * FROM analysis_jobs").fetchone())
        audit_before = self.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
        self.assertEqual(self._claim("ghost").status, 404)
        self.assertEqual(self._claim("auditor").status, 403)
        self.connection.execute("UPDATE users SET active=0 WHERE user_id='stat'")
        self.assertEqual(self._claim("stat").status, 403)
        self.assertEqual(dict(self.connection.execute("SELECT * FROM analysis_jobs").fetchone()), before)
        audit_after = self.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
        self.assertEqual(audit_after, audit_before)

    def test_authorized_claim_succeeds(self) -> None:
        response = self._claim("stat")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["job"]["state"], "leased")
        self.assertEqual(response.body["job"]["lease_owner"], "stat")

    def test_missing_worker_id_is_validation_error(self) -> None:
        response = self.app.handle("POST", "/jobs/claim", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
