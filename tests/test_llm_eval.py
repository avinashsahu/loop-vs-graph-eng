import unittest

from llm_eval import EvaluationCase, EvaluationPrediction, score_predictions


class LlmEvaluationTests(unittest.TestCase):
    def test_scores_classification_safety_and_runtime_metrics(self):
        cases = (
            EvaluationCase(
                case_id="routine-dividend",
                expected_verdict="PASS",
                expected_reason_code="NO_MATERIAL_RED_FLAG",
                available_evidence_ids=("DIVIDEND_1",),
                acceptable_evidence_ids=("DIVIDEND_1",),
            ),
            EvaluationCase(
                case_id="regulatory-penalty",
                expected_verdict="REJECT",
                expected_reason_code="GOVERNANCE_OR_REGULATORY",
                available_evidence_ids=("PENALTY_1",),
                acceptable_evidence_ids=("PENALTY_1",),
            ),
            EvaluationCase(
                case_id="ambiguous-litigation",
                expected_verdict="REVIEW",
                expected_reason_code="INSUFFICIENT_EVIDENCE",
                available_evidence_ids=("LITIGATION_1",),
                acceptable_evidence_ids=("LITIGATION_1",),
            ),
        )
        predictions = (
            EvaluationPrediction(
                case_id="routine-dividend",
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                evidence_ids=("DIVIDEND_1",),
                output_valid=True,
                first_pass_valid=True,
                repair_attempted=False,
                latency_ms=100.0,
                response_chars=120,
            ),
            EvaluationPrediction(
                case_id="regulatory-penalty",
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                evidence_ids=("PENALTY_1",),
                output_valid=True,
                first_pass_valid=False,
                repair_attempted=True,
                latency_ms=300.0,
                response_chars=140,
            ),
            EvaluationPrediction(
                case_id="ambiguous-litigation",
                verdict="REVIEW",
                reason_code="INSUFFICIENT_EVIDENCE",
                evidence_ids=("UNKNOWN_1",),
                output_valid=False,
                first_pass_valid=True,
                repair_attempted=False,
                latency_ms=200.0,
                response_chars=160,
                error="TimeoutError: model request timed out",
            ),
        )

        report = score_predictions(cases, predictions).to_dict()

        metrics = report["metrics"]
        false_pass_interval = metrics.pop("false_pass_wilson_95")
        reject_interval = metrics.pop("reject_false_pass_wilson_95")
        review_interval = metrics.pop("review_false_pass_wilson_95")

        self.assertEqual(
            metrics,
            {
                "case_count": 3,
                "exact_verdict_count": 1,
                "exact_verdict_rate": 1 / 3,
                "exact_reason_code_count": 1,
                "exact_reason_code_rate": 1 / 3,
                "false_pass_count": 1,
                "non_pass_case_count": 2,
                "false_pass_rate": 0.5,
                "reject_case_count": 1,
                "reject_false_pass_count": 1,
                "reject_false_pass_rate": 1.0,
                "review_case_count": 1,
                "review_false_pass_count": 0,
                "review_false_pass_rate": 0.0,
                "grounded_citation_count": 2,
                "grounded_citation_rate": 2 / 3,
                "acceptable_citation_count": 2,
                "acceptable_citation_rate": 2 / 3,
                "valid_output_count": 2,
                "valid_output_rate": 2 / 3,
                "runtime_error_count": 1,
                "runtime_error_rate": 1 / 3,
                "first_pass_valid_count": 2,
                "first_pass_valid_rate": 2 / 3,
                "repair_attempt_count": 1,
                "repair_rate": 1 / 3,
                "latency_ms_p50": 200.0,
                "latency_ms_p95": 300.0,
                "response_chars_mean": 140.0,
                "completion_tokens_p50": 0,
                "completion_tokens_p95": 0,
                "reasoning_present_count": 0,
                "reasoning_present_rate": 0.0,
                "reasoning_chars_p50": 0,
                "reasoning_chars_p95": 0,
            },
        )
        self.assertAlmostEqual(false_pass_interval["lower"], 0.094531, places=6)
        self.assertAlmostEqual(false_pass_interval["upper"], 0.905469, places=6)
        self.assertAlmostEqual(reject_interval["lower"], 0.206549, places=6)
        self.assertEqual(reject_interval["upper"], 1.0)
        self.assertEqual(review_interval["lower"], 0.0)
        self.assertAlmostEqual(review_interval["upper"], 0.793451, places=6)
        self.assertEqual(
            [item["case_id"] for item in report["failures"]],
            ["ambiguous-litigation", "regulatory-penalty"],
        )


if __name__ == "__main__":
    unittest.main()
