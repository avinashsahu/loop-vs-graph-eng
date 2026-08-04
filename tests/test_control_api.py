import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import app_scheduler
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


if __name__ == "__main__":
    unittest.main()
