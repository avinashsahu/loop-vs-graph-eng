import copy
import unittest

from fundamental_evidence import FundamentalEvidence
from fundamental_research import evaluate_fundamental_research
from llm import FundamentalAssessment


def _evidence(financial_history):
    return FundamentalEvidence(
        {
            "version": "fundamental-evidence-v6",
            "symbol": "TEST",
            "company_name": "Test Limited",
            "coverage": {"complete": True, "missing": []},
            "freshness": {
                "as_of": "2026-07-29",
                "financial_period": financial_history["periods"][0]["period_end"],
                "financial_age_days": 29,
                "financial_stale": False,
            },
            "financial_history": financial_history,
            "facts": [
                {
                    "id": "ANNOUNCEMENT_TEST",
                    "kind": "announcement",
                    "date": "2026-07-20",
                    "category": "Outcome of board meeting",
                    "text": "Routine quarterly results approved.",
                }
            ],
        }
    )


def _non_financial_history():
    return {
        "status": "ready",
        "profile": "non_financial",
        "subtype": "ind_as",
        "periods": [
            {
                "period_end": "2026-06-30",
                "revenue": 140.0,
                "operating_profit": 24.0,
                "pat": 15.0,
                "operating_margin_pct": 17.14,
            },
            {
                "period_end": "2026-03-31",
                "revenue": 130.0,
                "operating_profit": 22.0,
                "pat": 14.0,
                "operating_margin_pct": 16.92,
                "assets": 300.0,
                "equity": 150.0,
                "total_debt": 30.0,
                "debt_to_equity": 0.2,
                "operating_cash_flow": 20.0,
                "return_on_equity_pct": 18.0,
            },
            {
                "period_end": "2025-12-31",
                "revenue": 125.0,
                "operating_profit": 20.0,
                "pat": 13.0,
                "operating_margin_pct": 16.0,
            },
            {
                "period_end": "2025-09-30",
                "revenue": 120.0,
                "operating_profit": 19.0,
                "pat": 12.0,
                "operating_margin_pct": 15.83,
            },
            {
                "period_end": "2025-03-31",
                "revenue": 110.0,
                "operating_profit": 17.0,
                "pat": 11.0,
                "operating_margin_pct": 15.45,
                "assets": 270.0,
                "equity": 130.0,
                "total_debt": 35.0,
                "debt_to_equity": 0.27,
                "return_on_equity_pct": 17.0,
            },
        ],
    }


def _banking_history():
    return {
        "status": "ready",
        "profile": "banking_nbfc",
        "subtype": "bank",
        "periods": [
            {
                "period_end": "2026-06-30",
                "net_interest_income": 220.0,
                "operating_profit": 150.0,
                "provisions": 24.0,
                "pat": 92.0,
                "gross_npa_pct": 2.1,
                "net_npa_pct": 0.6,
                "return_on_assets_pct": 1.3,
            },
            {
                "period_end": "2026-03-31",
                "net_interest_income": 210.0,
                "operating_profit": 142.0,
                "provisions": 25.0,
                "pat": 88.0,
                "gross_npa_pct": 2.2,
                "net_npa_pct": 0.7,
                "return_on_assets_pct": 1.2,
            },
            {
                "period_end": "2025-12-31",
                "net_interest_income": 205.0,
                "operating_profit": 138.0,
                "provisions": 26.0,
                "pat": 84.0,
                "gross_npa_pct": 2.3,
                "net_npa_pct": 0.8,
                "return_on_assets_pct": 1.1,
            },
            {
                "period_end": "2025-09-30",
                "net_interest_income": 198.0,
                "operating_profit": 132.0,
                "provisions": 27.0,
                "pat": 80.0,
                "gross_npa_pct": 2.4,
                "net_npa_pct": 0.9,
                "return_on_assets_pct": 1.0,
            },
        ],
    }


def _nbfc_history():
    quarterly = [
        {
            "period_end": period,
            "revenue": 100.0 + index,
            "finance_cost": 20.0,
            "impairment": 4.0,
            "pat": 15.0,
        }
        for index, period in enumerate(
            (
                "2026-06-30",
                "2026-03-31",
                "2025-12-31",
                "2025-09-30",
            )
        )
    ]
    quarterly[1]["funding_leverage"] = {
        "ratio": 3.5,
        "method": "derived_funding_liabilities_to_equity",
        "balance_sheet_reconciled": True,
        "funding_components_reconciled": True,
    }
    quarterly[0]["credit_cost"] = quarterly[0].pop("impairment")
    return {
        "status": "ready",
        "profile": "banking_nbfc",
        "subtype": "nbfc",
        "selected_scope": "standalone",
        "periods": [
            *quarterly,
            {
                "period_end": "2025-03-31",
                "revenue": 95.0,
                "finance_cost": 19.0,
                "impairment": 4.0,
                "pat": 14.0,
                "funding_leverage": {
                    "ratio": 3.2,
                    "method": "derived_funding_liabilities_to_equity",
                    "balance_sheet_reconciled": True,
                    "funding_components_reconciled": True,
                },
            },
        ],
    }


class FundamentalResearchTests(unittest.TestCase):
    def test_non_financial_profile_reviews_missing_cash_flow_without_model(self):
        history = _non_financial_history()
        calls = []

        decision = evaluate_fundamental_research(
            _evidence(history),
            lambda *args: calls.append(args),
        )

        self.assertEqual(decision.verdict, "REVIEW")
        self.assertIn("operating_cash_flow:2025-03-31", decision.missing)
        self.assertEqual(calls, [])

        complete = copy.deepcopy(history)
        complete["periods"][-1]["operating_cash_flow"] = 18.0
        complete["periods"][1]["debt_to_equity"] = 3.0
        deterministic_reject = evaluate_fundamental_research(
            _evidence(complete),
            lambda *args: calls.append(args),
        )
        self.assertEqual(deterministic_reject.verdict, "REJECT")
        self.assertIn("LEVERAGE_ABOVE_POLICY", deterministic_reject.checks)
        self.assertEqual(calls, [])

    def test_banking_profile_pass_is_scoped_and_model_sees_only_qualitative_evidence(self):
        seen = {}

        def interpret(prompt, evidence_ids):
            seen["prompt"] = prompt
            seen["ids"] = evidence_ids
            return FundamentalAssessment(
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                reason="No qualitative red flag in the supplied announcement.",
                evidence_ids=("ANNOUNCEMENT_TEST",),
                missing=(),
            )

        decision = evaluate_fundamental_research(
            _evidence(_banking_history()),
            interpret,
        )

        self.assertEqual(decision.verdict, "PASS")
        self.assertIn("policy-required evidence", decision.reason)
        self.assertIn("not an assessment of overall company quality", decision.reason)
        self.assertTrue(decision.model_invoked)
        self.assertEqual(seen["ids"], ("ANNOUNCEMENT_TEST",))
        self.assertIn("ANNOUNCEMENT_TEST", seen["prompt"])
        self.assertNotIn("gross_npa_pct", seen["prompt"])
        self.assertNotIn("net_interest_income", seen["prompt"])

    def test_nbfc_quarters_do_not_require_debt_to_equity(self):
        decision = evaluate_fundamental_research(
            _evidence(_nbfc_history()),
            lambda _prompt, _ids: FundamentalAssessment(
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                reason="No qualitative red flag.",
                evidence_ids=("ANNOUNCEMENT_TEST",),
                missing=(),
            ),
        )

        self.assertEqual(decision.verdict, "PASS")
        self.assertNotIn(
            "debt_to_equity:2026-06-30",
            decision.missing,
        )

        inconsistent = copy.deepcopy(_nbfc_history())
        inconsistent["periods"][1]["funding_leverage"][
            "balance_sheet_reconciled"
        ] = False
        review = evaluate_fundamental_research(
            _evidence(inconsistent),
            lambda *_args: self.fail(
                "qualitative model must not run with inconsistent annual leverage"
            ),
        )
        self.assertEqual(review.verdict, "REVIEW")
        self.assertIn("funding_leverage:2026-03-31", review.missing)

    def test_structured_disclosure_policy_runs_without_the_model(self):
        review_evidence = _evidence(_banking_history())
        review_evidence.payload["facts"].append(
            {
                "id": "RATING_DOWNGRADE",
                "kind": "credit_rating_action",
                "action_direction": "downgrade",
                "policy_verdict": "REVIEW",
                "policy_reason_code": "CREDIT_RATING_CAUTION",
            }
        )
        decision = evaluate_fundamental_research(
            review_evidence,
            lambda *_args: self.fail(
                "structured rating caution must bypass the model"
            ),
        )
        self.assertEqual(decision.verdict, "REVIEW")
        self.assertEqual(decision.reason_code, "CREDIT_RATING_CAUTION")
        self.assertEqual(decision.evidence_ids, ("RATING_DOWNGRADE",))

        reject_evidence = _evidence(_banking_history())
        reject_evidence.payload["facts"].append(
            {
                "id": "RATING_DEFAULT",
                "kind": "credit_rating_action",
                "action_direction": "default",
                "policy_verdict": "REJECT",
                "policy_reason_code": "ADVERSE_CORPORATE_EVENT",
            }
        )
        rejected = evaluate_fundamental_research(
            reject_evidence,
            lambda *_args: self.fail(
                "structured rating default must bypass the model"
            ),
        )
        self.assertEqual(rejected.verdict, "REJECT")
        self.assertEqual(rejected.evidence_ids, ("RATING_DEFAULT",))

    def test_governance_exception_and_encumbrance_increase_require_review(self):
        governance_evidence = _evidence(_banking_history())
        governance_evidence.payload["facts"].append(
            {
                "id": "GOVERNANCE_BOARD",
                "kind": "governance_exception",
                "code": "board_composition_non_compliance",
                "policy_verdict": "REVIEW",
                "policy_reason_code": "GOVERNANCE_DISCLOSURE_CAUTION",
            }
        )
        governance = evaluate_fundamental_research(
            governance_evidence,
            lambda *_args: self.fail(
                "structured governance caution must bypass the model"
            ),
        )
        self.assertEqual(governance.verdict, "REVIEW")
        self.assertEqual(
            governance.reason_code, "GOVERNANCE_DISCLOSURE_CAUTION"
        )
        self.assertEqual(governance.evidence_ids, ("GOVERNANCE_BOARD",))

        encumbrance_evidence = _evidence(_banking_history())
        encumbrance_evidence.payload["facts"].append(
            {
                "id": "SHAREHOLDING_TREND",
                "kind": "calculated_shareholding_trend",
                "changes_bps": {
                    "promoter_encumbered_qoq": 250,
                    "promoter_encumbered_4q": 600,
                },
            }
        )
        encumbrance = evaluate_fundamental_research(
            encumbrance_evidence,
            lambda *_args: self.fail(
                "encumbrance caution must bypass the model"
            ),
        )
        self.assertEqual(encumbrance.verdict, "REVIEW")
        self.assertEqual(
            encumbrance.reason_code, "PROMOTER_ENCUMBRANCE_CAUTION"
        )
        self.assertEqual(encumbrance.evidence_ids, ("SHAREHOLDING_TREND",))

    def test_missing_optional_governance_does_not_reject(self):
        evidence = _evidence(_banking_history())
        evidence.payload["facts"].append(
            {
                "id": "GOVERNANCE_COVERAGE",
                "kind": "governance_coverage",
                "status": "pending",
                "periods_available": 0,
                "optional": True,
            }
        )
        decision = evaluate_fundamental_research(
            evidence,
            lambda *_args: FundamentalAssessment(
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                reason="No material qualitative red flag.",
                evidence_ids=("ANNOUNCEMENT_TEST",),
                missing=(),
            ),
        )
        self.assertEqual(decision.verdict, "PASS")


if __name__ == "__main__":
    unittest.main()
