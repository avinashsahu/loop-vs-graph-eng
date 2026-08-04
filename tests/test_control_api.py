import unittest

from fastapi.testclient import TestClient

from webapp.backend.main import app


class HealthTests(unittest.TestCase):
    def test_health_endpoint_reports_ok(self):
        client = TestClient(app)
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
