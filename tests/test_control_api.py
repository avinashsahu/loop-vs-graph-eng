import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import app_scheduler
import webapp.backend.main as main_module
from webapp.backend.main import app


class HealthTests(unittest.TestCase):
    def test_health_endpoint_reports_ok(self):
        client = TestClient(app)
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class JobsEndpointTests(unittest.TestCase):
    def test_get_jobs_returns_job_status_summary(self):
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            overrides_path = Path(temporary) / "overrides.json"
            with (
                patch.object(app_scheduler, "STATE_PATH", state_path),
                patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path),
            ):
                client = TestClient(app)
                response = client.get("/api/jobs")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertIsInstance(body, list)
                names = {job["name"] for job in body}
                self.assertIn("bhavcopy", names)
                self.assertIn("intraday_recheck", names)


class JobControlActionTests(unittest.TestCase):
    def test_toggle_writes_an_enabled_override(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post(
                    "/api/jobs/toggle", json={"job": "cleanup", "enabled": False}
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(
                    app_scheduler.load_overrides()["enabled_overrides"]["cleanup"],
                    False,
                )

    def test_toggle_rejects_unknown_job(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post(
                    "/api/jobs/toggle", json={"job": "not_a_real_job", "enabled": True}
                )
                self.assertEqual(response.status_code, 404)

    def test_run_now_queues_a_force_run_request(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post("/api/jobs/run-now", json={"job": "cleanup"})
                self.assertEqual(response.status_code, 204)
                self.assertIn("cleanup", app_scheduler.load_overrides()["force_run"])


class ScanTriggerTests(unittest.TestCase):
    def test_post_scans_rejects_empty_symbol_list(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post("/api/scans", json={"symbols": []})
                self.assertEqual(response.status_code, 400)

    def test_post_scans_then_get_scans_reports_queued_status(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                post_response = client.post(
                    "/api/scans", json={"symbols": ["RELIANCE"]}
                )
                self.assertEqual(post_response.status_code, 201)
                request_id = post_response.json()["request_id"]

                get_response = client.get(f"/api/scans/{request_id}")
                self.assertEqual(get_response.status_code, 200)
                body = get_response.json()
                self.assertEqual(body["status"], "queued")
                self.assertEqual(body["decisions"], [])

    def test_get_scans_returns_404_for_unknown_id(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.get("/api/scans/not-a-real-id")
                self.assertEqual(response.status_code, 404)

    def test_get_scans_reads_decisions_once_done(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            db_path = Path(temporary) / "evaluation.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                """
                CREATE TABLE decisions (
                    decision_id TEXT PRIMARY KEY, decision_timestamp TEXT,
                    scan_label TEXT, symbol TEXT, status TEXT, disposition TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                ("id1", "2026-08-04T10:00:00+05:30", "adhoc-abc", "RELIANCE",
                 "ok", "PROPOSE"),
            )
            connection.commit()
            connection.close()
            with (
                patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path),
                patch.object(main_module, "EVALUATION_DB_PATH", db_path),
            ):
                overrides = app_scheduler.load_overrides()
                overrides["ad_hoc_results"] = {
                    "abc": {
                        "status": "done",
                        "scan_label": "adhoc-abc",
                        "finished_at": "2026-08-04T10:00:05+05:30",
                    }
                }
                app_scheduler.save_overrides(overrides)

                client = TestClient(app)
                response = client.get("/api/scans/abc")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["status"], "done")
                self.assertEqual(len(body["decisions"]), 1)
                self.assertEqual(body["decisions"][0]["symbol"], "RELIANCE")
                self.assertEqual(body["decisions"][0]["disposition"], "PROPOSE")


if __name__ == "__main__":
    unittest.main()
