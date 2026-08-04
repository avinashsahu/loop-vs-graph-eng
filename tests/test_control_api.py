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


if __name__ == "__main__":
    unittest.main()
