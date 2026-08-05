import json
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app_scheduler
import evaluation
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


class DecisionsEndpointTests(unittest.TestCase):
    def _seed_db(self, db_path):
        connection = sqlite3.connect(db_path)
        connection.execute(
            """
            CREATE TABLE decisions (
                decision_id TEXT PRIMARY KEY, decision_timestamp TEXT,
                decision_date TEXT, scan_label TEXT, symbol TEXT, status TEXT,
                disposition TEXT, reason_stage TEXT, reason_code TEXT,
                entry_price REAL, stop_price REAL, target_price REAL,
                shares INTEGER, technical_score REAL, technical_verdict TEXT,
                fundamental_verdict TEXT, risk_verdict TEXT,
                sentiment_verdict TEXT, model_backend TEXT, model_name TEXT,
                llm_max_tokens INTEGER, fundamental_llm_max_tokens INTEGER,
                policy_version TEXT, risk_plan_valid INTEGER,
                raw_record_json TEXT, created_at TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO decisions (
                decision_id, decision_timestamp, decision_date, scan_label,
                symbol, status, disposition, raw_record_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("id1", "2026-08-01T09:00:00+05:30", "2026-08-01", "overnight_1",
                 "RELIANCE", "ok", "PROPOSE", '{"note": "first"}', "2026-08-01T09:00:01+05:30"),
                ("id2", "2026-08-02T09:00:00+05:30", "2026-08-02", "overnight_2",
                 "HDFCBANK", "ok", "REJECT", '{"note": "second"}', "2026-08-02T09:00:01+05:30"),
            ],
        )
        connection.commit()
        connection.close()

    def test_list_decisions_returns_paginated_results(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["total"], 2)
                self.assertEqual(len(body["results"]), 2)
                self.assertNotIn("raw_record_json", body["results"][0])

    def test_list_decisions_filters_by_symbol(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions", params={"symbol": "RELIANCE"})
                body = response.json()
                self.assertEqual(body["total"], 1)
                self.assertEqual(body["results"][0]["symbol"], "RELIANCE")

    def test_get_decision_returns_parsed_evidence(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions/id1")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["symbol"], "RELIANCE")
                self.assertEqual(body["evidence"], {"note": "first"})

    def test_get_decision_returns_404_for_unknown_id(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions/not-a-real-id")
                self.assertEqual(response.status_code, 404)


class ShareholdingCoverageTests(unittest.TestCase):
    def test_coverage_reports_universe_members(self):
        fake_store = Mock()
        fake_store.list_universe.return_value = [
            {
                "symbol": "RELIANCE",
                "active": 1,
                "last_status": "complete",
                "last_attempt": 1754270400,
                "completed_at": 1754270400,
                "periods": 5,
            },
            {
                "symbol": "TCS",
                "active": 1,
                "last_status": "pending",
                "last_attempt": 0,
                "completed_at": 0,
                "periods": 0,
            },
        ]
        fake_store.queued_symbols.return_value = ["TCS"]
        with patch.object(main_module, "_shareholding_store", return_value=fake_store):
            client = TestClient(app)
            response = client.get(
                "/api/coverage/shareholding", params={"universe": "NIFTY TOTAL MKT"}
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["total"], 2)
            by_symbol = {row["symbol"]: row for row in body["results"]}
            self.assertEqual(by_symbol["RELIANCE"]["last_status"], "complete")
            self.assertFalse(by_symbol["RELIANCE"]["queued"])
            self.assertTrue(by_symbol["TCS"]["queued"])
            self.assertIsNone(by_symbol["TCS"]["last_attempt"])

    def test_coverage_returns_503_when_aerospike_unreachable(self):
        with patch.object(
            main_module,
            "_shareholding_store",
            side_effect=RuntimeError("connection refused"),
        ):
            client = TestClient(app)
            response = client.get("/api/coverage/shareholding")
            self.assertEqual(response.status_code, 503)


class CacheCoverageTests(unittest.TestCase):
    def test_disclosures_coverage_lists_cached_symbols_by_staleness(self):
        with TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            now = time.time()
            (cache_dir / "material_disclosures_v1_RELIANCE.json").write_text(
                json.dumps({"fetched_at": now - 3600, "data": {}})
            )
            (cache_dir / "material_disclosures_v1_TCS.json").write_text(
                json.dumps({"fetched_at": now - 3600 * 400, "data": {}})
            )
            with patch.object(main_module, "CACHE_DIR", cache_dir):
                client = TestClient(app)
                response = client.get("/api/coverage/cache/disclosures")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["total"], 2)
                self.assertEqual(body["results"][0]["symbol"], "TCS")
                self.assertFalse(body["results"][0]["fresh"])
                self.assertTrue(body["results"][1]["fresh"])

    def test_coverage_cache_returns_404_for_unknown_system(self):
        client = TestClient(app)
        response = client.get("/api/coverage/cache/not_a_real_system")
        self.assertEqual(response.status_code, 404)

    def test_coverage_cache_handles_missing_directory(self):
        with TemporaryDirectory() as temporary:
            missing_dir = Path(temporary) / "does-not-exist"
            with patch.object(main_module, "CACHE_DIR", missing_dir):
                client = TestClient(app)
                response = client.get("/api/coverage/cache/governance")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total"], 0)


class CalibrationReportTests(unittest.TestCase):
    def test_report_reflects_recorded_decisions(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            ledger = evaluation.EvaluationLedger(str(db_path))
            ledger.record_decision(
                {
                    "timestamp": "2026-08-01T09:00:00+05:30",
                    "scan_label": "overnight_1",
                    "symbol": "RELIANCE",
                    "status": "proposed",
                }
            )
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/calibration-report")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["decisions"]["total"], 1)
                self.assertEqual(body["decisions"]["status_counts"], {"proposed": 1})
                self.assertIn("methodology", body)

    def test_report_handles_missing_database(self):
        with TemporaryDirectory() as temporary:
            missing_db = Path(temporary) / "does-not-exist.db"
            with patch.object(main_module, "EVALUATION_DB_PATH", missing_db):
                client = TestClient(app)
                response = client.get("/api/calibration-report")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["decisions"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
