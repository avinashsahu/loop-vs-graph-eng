import unittest

from scan_engine import (
    Disposition,
    PersistenceReceipt,
    ScanEngine,
    ScanExecution,
    ScanExecutionError,
    ScanPurpose,
    ScanRequest,
    StageTiming,
)


class _FixtureAdapter:
    def __init__(self, *, fail_at=None):
        self.fail_at = fail_at

    def execute(self, request):
        timings = (
            StageTiming("data_validity", 1.2),
            StageTiming("technical", 2.3),
            StageTiming("fundamental", 3.4),
            StageTiming("risk", 0.8),
            StageTiming("disposition", 0.2),
        )
        if self.fail_at:
            raise ScanExecutionError(
                self.fail_at,
                RuntimeError("fixture provider failed"),
                timings[:3],
            )
        return ScanExecution(
            record={
                "timestamp": "2026-07-30T09:00:00+05:30",
                "symbol": request.symbol,
                "status": "proposed",
                "disposition": "PROPOSE",
                "decision_reason": {
                    "stage": "decision",
                    "code": "ALL_GATES_PASSED",
                },
                "technical_assessment": {
                    "status": "ready",
                    "reason_codes": [],
                    "policy_id": "technical-relative-participation-v2",
                    "policy_fingerprint": "technical-fixture",
                    "evidence": {
                        "families": {
                            "trend": 0.4,
                            "momentum": 0.3,
                            "relative_strength": 0.2,
                            "participation": 0.1,
                        }
                    },
                },
                "fundamental_evidence": {
                    "coverage": {"complete": True, "missing": []}
                },
                "fundamental_assessment": {
                    "verdict": "PASS",
                    "reason_code": "NO_MATERIAL_RED_FLAG",
                    "model_invoked": True,
                    "policy_version": "fundamental-sector-policy-v1",
                },
                "risk_plan": {
                    "entry_price": 100.0,
                    "stop_price": 95.0,
                    "target_price": 110.0,
                    "shares": 10,
                },
                "risk_verdict": "GOOD",
                "policy_version": "scan-policy-fixture",
                "model_config": {
                    "backend": "fixture",
                    "name": "qualitative-fixture",
                    "max_tokens": 384,
                },
                "proposal": "fixture proposal",
            },
            timings=timings,
        )


class _MemoryStore:
    def __init__(self):
        self.records = []

    def persist(self, record):
        self.records.append(record)
        return PersistenceReceipt(
            decision_id=f"fixture-{len(self.records)}",
            durable=True,
        )


class ScanEngineTests(unittest.TestCase):
    def test_fixture_scan_and_stage_failure_use_the_same_typed_interface(self):
        store = _MemoryStore()
        request = ScanRequest(
            symbol="ACE",
            principal=100_000,
            scan_label="fixture",
            purpose=ScanPurpose.BATCH,
        )

        result = ScanEngine(_FixtureAdapter(), store).scan(request)

        self.assertEqual(result.disposition, Disposition.PROPOSE)
        self.assertEqual(result.reason_codes[0].code, "ALL_GATES_PASSED")
        self.assertEqual(result.policy.version, "scan-policy-fixture")
        self.assertEqual(result.model.backend, "fixture")
        self.assertGreaterEqual(result.elapsed_ms, 0)
        node_ids = {node.node_id for node in result.decision_graph.nodes}
        self.assertEqual(
            node_ids,
            {
                "data_validity",
                "technical_families",
                "fundamental_coverage",
                "qualitative_evidence",
                "risk",
                "disposition",
                "alert",
                "outcome",
            },
        )
        self.assertNotIn("sma", node_ids)
        self.assertEqual(
            store.records[0]["decision_graph"]["version"],
            "decision-graph-v1",
        )

        failed = ScanEngine(
            _FixtureAdapter(fail_at="fundamental"),
            store,
        ).scan(request)

        self.assertEqual(failed.disposition, Disposition.FAILED)
        self.assertEqual(failed.failure.stage, "fundamental")
        self.assertTrue(failed.failure.durable)
        self.assertEqual(store.records[1]["status"], "failed")
        self.assertEqual(
            store.records[1]["durable_failure"]["error_type"],
            "RuntimeError",
        )


if __name__ == "__main__":
    unittest.main()
