import unittest
from io import BytesIO

import digest
from document_research import (
    DocumentResearchService,
    _extract_facts,
    _select_documents,
)
from fundamental_evidence import build_fundamental_evidence
from fundamental_research import evaluate_fundamental_research
from llm import FundamentalAssessment
from shareholding import ShareholdingHistory, ShareholdingPeriod


class _Source:
    def __init__(self, documents, payloads):
        self.documents = documents
        self.payloads = payloads
        self.downloads = []

    def list_documents(self, symbol, start, end):
        return self.documents

    def download(self, document):
        self.downloads.append(document["document_id"])
        payload = self.payloads[document["document_id"]]
        if isinstance(payload, Exception):
            raise payload
        return payload


class _Store:
    def __init__(self):
        self.records = {}

    def read(self, symbol, ttl_seconds):
        return self.records.get(symbol.upper())

    def write(self, symbol, payload):
        self.records[symbol.upper()] = payload


class DocumentResearchTests(unittest.TestCase):
    def test_selects_latest_document_per_type(self):
        selected = _select_documents(
            "HDFCBANK",
            [
                {
                    "desc": "Investor Presentation",
                    "attchmntText": "old presentation",
                    "attchmntFile": "https://example.invalid/old.pdf",
                    "an_dt": "01-Jan-2026 10:00:00",
                },
                {
                    "desc": "Investor Presentation",
                    "attchmntText": "new presentation",
                    "attchmntFile": "https://example.invalid/new.pdf",
                    "an_dt": "18-Jul-2026 15:18:05",
                },
                {
                    "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
                    "attchmntText": "Transcript of earnings call",
                    "attchmntFile": "https://example.invalid/transcript.pdf",
                    "an_dt": "24-Jul-2026 15:52:03",
                },
                {
                    "desc": "Board Meeting",
                    "attchmntText": "routine",
                    "attchmntFile": "https://example.invalid/board.pdf",
                    "an_dt": "20-Jul-2026 10:00:00",
                },
            ],
        )
        self.assertEqual(
            [item["doc_type"] for item in selected],
            ["investor_presentation", "earnings_transcript"],
        )
        self.assertIn("new.pdf", selected[0]["source_url"])

    def test_extracts_bank_and_auditor_facts_with_provenance(self):
        document = {
            "document_id": "DOC1",
            "doc_type": "investor_presentation",
            "source_url": "https://example.invalid/bank.pdf",
            "reporting_period": "2026-06-30",
        }
        text = (
            "[page 2]\nGross NPA stood at 1.24% while NIM was 3.50%. "
            "AUM reached Rs. 12,345 crore. "
            "[page 4]\nThe auditor issued a qualified opinion on inventory valuation."
        )
        facts = _extract_facts(document, text)
        codes = {fact["code"] for fact in facts}
        self.assertIn("bank_gnpa", codes)
        self.assertIn("bank_nim", codes)
        self.assertIn("bank_aum", codes)
        self.assertIn("auditor_qualification", codes)
        auditor = next(
            fact for fact in facts if fact["code"] == "auditor_qualification"
        )
        self.assertEqual(auditor["policy_verdict"], "REVIEW")
        self.assertEqual(auditor["page"], 4)
        self.assertTrue(auditor["id"].startswith("RESEARCH_"))
        gnpa = next(fact for fact in facts if fact["code"] == "bank_gnpa")
        self.assertEqual(gnpa["value"], 1.24)
        self.assertEqual(gnpa["extraction_method"], "deterministic_regex")

    def test_one_failed_document_does_not_stop_symbol_warm(self):
        documents = [
            {
                "document_id": "bad",
                "doc_type": "annual_report",
                "source_url": "https://example.invalid/bad.pdf",
                "nse_subject": "Annual Report",
                "announced_at": "01-Jun-2026 10:00:00",
                "reporting_period": "2026-03-31",
            },
            {
                "document_id": "good",
                "doc_type": "investor_presentation",
                "source_url": "https://example.invalid/good.pdf",
                "nse_subject": "Investor Presentation",
                "announced_at": "18-Jul-2026 15:18:05",
                "reporting_period": "2026-06-30",
            },
        ]
        source = _Source(
            documents,
            {
                "bad": RuntimeError("download failed"),
                "good": _minimal_pdf(),
            },
        )
        store = _Store()
        result = DocumentResearchService(source, store).warm("HDFCBANK")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["document_counts"]["failed"], 1)
        self.assertEqual(result["document_counts"]["ready"], 1)
        self.assertTrue(any(error.startswith("bad:") for error in result["errors"]))

    def test_live_get_is_cache_only(self):
        source = _Source([], {})
        store = _Store()
        service = DocumentResearchService(
            source, store, download_missing=False
        )
        pending = service.get("INFY")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(source.downloads, [])

    def test_bank_and_non_financial_research_flow_into_slack(self):
        bank_evidence = build_fundamental_evidence(
            "HDFCBANK",
            {
                "complete": True,
                "as_of": "2026-07-29",
                "company_name": "HDFC Bank Limited",
                "corp_actions": [],
                "peer_comparison": [
                    {"symbol": "HDFCBANK", "eps": 10.0, "pat": 100.0, "pe": 18.0}
                ],
                "material_disclosures": {
                    "status": "ready",
                    "events": [],
                    "credit_ratings": [],
                },
                "peer_comparison_quarter": "2026-06-30",
                "financial_history": _banking_history(),
                "document_research": {
                    "status": "ready",
                    "document_counts": {
                        "ready": 1,
                        "failed": 0,
                        "discovered": 1,
                    },
                    "facts": [
                        {
                            "id": "RESEARCH_BANK_NIM",
                            "kind": "document_research_fact",
                            "code": "bank_nim",
                            "doc_type": "investor_presentation",
                            "document_id": "DOC_BANK",
                            "source_url": "https://example.invalid/bank.pdf",
                            "page": 2,
                            "excerpt": "NIM was 3.50%.",
                            "numeric": True,
                            "value": 3.5,
                            "unit": "%",
                            "extraction_method": "deterministic_regex",
                            "optional": True,
                        }
                    ],
                },
            },
            _shareholding("HDFCBANK"),
        )
        bank_decision = evaluate_fundamental_research(
            bank_evidence,
            lambda *_args: FundamentalAssessment(
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                reason="No qualitative red flag.",
                evidence_ids=(),
                missing=(),
            ),
        )
        self.assertEqual(bank_decision.verdict, "PASS")
        bank_summary = digest._fundamental_summary(
            {
                "fundamental_assessment": bank_decision.to_dict(),
                "fundamental_evidence": bank_evidence.payload,
            }
        )
        self.assertIn("1 long-form filing(s) available for reference.", bank_summary)

        nonfin_evidence = build_fundamental_evidence(
            "INFY",
            {
                "complete": True,
                "as_of": "2026-07-29",
                "company_name": "Infosys Limited",
                "corp_actions": [],
                "peer_comparison": [
                    {"symbol": "INFY", "eps": 20.0, "pat": 200.0, "pe": 25.0}
                ],
                "material_disclosures": {
                    "status": "ready",
                    "events": [],
                    "credit_ratings": [],
                },
                "peer_comparison_quarter": "2026-06-30",
                "financial_history": _non_financial_history(),
                "document_research": {
                    "status": "ready",
                    "document_counts": {
                        "ready": 1,
                        "failed": 0,
                        "discovered": 1,
                    },
                    "facts": [
                        {
                            "id": "RESEARCH_AUDITOR",
                            "kind": "document_research_fact",
                            "code": "auditor_qualification",
                            "doc_type": "annual_report",
                            "document_id": "DOC_INFY",
                            "source_url": "https://example.invalid/ar.pdf",
                            "page": 4,
                            "excerpt": "The auditor issued a qualified opinion.",
                            "numeric": False,
                            "extraction_method": "deterministic_regex",
                            "policy_verdict": "REVIEW",
                            "policy_reason_code": "GOVERNANCE_DISCLOSURE_CAUTION",
                            "optional": True,
                        }
                    ],
                },
            },
            _shareholding("INFY"),
        )
        nonfin_decision = evaluate_fundamental_research(
            nonfin_evidence,
            lambda *_args: self.fail("auditor qualification must bypass model"),
        )
        self.assertEqual(nonfin_decision.verdict, "REVIEW")
        self.assertEqual(
            nonfin_decision.reason_code, "GOVERNANCE_DISCLOSURE_CAUTION"
        )
        nonfin_summary = digest._fundamental_summary(
            {
                "fundamental_assessment": nonfin_decision.to_dict(),
                "fundamental_evidence": nonfin_evidence.payload,
            }
        )
        self.assertIn("1 long-form filing(s) available for reference.", nonfin_summary)

    def test_missing_optional_research_is_coverage_note_not_reject(self):
        evidence = build_fundamental_evidence(
            "INFY",
            {
                "complete": True,
                "as_of": "2026-07-29",
                "company_name": "Infosys Limited",
                "corp_actions": [],
                "peer_comparison": [
                    {"symbol": "INFY", "eps": 20.0, "pat": 200.0, "pe": 25.0}
                ],
                "material_disclosures": {
                    "status": "ready",
                    "events": [],
                    "credit_ratings": [],
                },
                "peer_comparison_quarter": "2026-06-30",
                "financial_history": _non_financial_history(),
                "document_research": {
                    "status": "pending",
                    "document_counts": {
                        "ready": 0,
                        "failed": 0,
                        "discovered": 0,
                    },
                    "facts": [],
                },
            },
            _shareholding("INFY"),
        )
        decision = evaluate_fundamental_research(
            evidence,
            lambda *_args: FundamentalAssessment(
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                reason="ok",
                evidence_ids=(),
                missing=(),
            ),
        )
        self.assertEqual(decision.verdict, "PASS")
        summary = digest._fundamental_summary(
            {
                "fundamental_assessment": decision.to_dict(),
                "fundamental_evidence": evidence.payload,
            }
        )
        self.assertNotIn("Additional research:", summary)
        self.assertNotIn("not yet refreshed", summary)
        self.assertNotIn("Governance coverage:", summary)


def _minimal_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _shareholding(symbol: str) -> ShareholdingHistory:
    periods = []
    for index, period in enumerate(
        ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")
    ):
        periods.append(
            ShareholdingPeriod(
                record_id=str(1000 + index),
                period=period,
                schema_version="2025-10-31",
                fii_pct=20,
                dii_pct=30,
                government_pct=0,
                promoter_pct=40,
                other_public_pct=10,
                public_shares=1000,
                component_shares=1000,
                reconciled=True,
                checksum="abc",
            )
        )
    return ShareholdingHistory(
        symbol=symbol,
        periods=tuple(periods),
        status="ready",
        latest_period=periods[0].period,
        latest_record_id=periods[0].record_id,
        periods_available=5,
        complete=True,
        changes_bps={},
        trend_labels={},
    )


def _banking_history():
    quarter = {
        "net_interest_income": 220.0,
        "operating_profit": 150.0,
        "provisions": 24.0,
        "pat": 92.0,
        "gross_npa_pct": 2.1,
        "net_npa_pct": 0.6,
        "return_on_assets_pct": 1.3,
    }
    return {
        "status": "ready",
        "profile": "banking_nbfc",
        "subtype": "bank",
        "selected_scope": "standalone",
        "scope_selection_reason": "regulated_entity_metrics",
        "available_scopes": ["standalone"],
        "periods": [
            {"period_end": period, **quarter}
            for period in (
                "2026-06-30",
                "2026-03-31",
                "2025-12-31",
                "2025-09-30",
            )
        ],
    }


def _non_financial_history():
    return {
        "status": "ready",
        "profile": "non_financial",
        "subtype": "ind_as",
        "selected_scope": "consolidated",
        "scope_selection_reason": "group_economics",
        "available_scopes": ["consolidated", "standalone"],
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
                "operating_cash_flow": 18.0,
                "return_on_equity_pct": 17.0,
            },
        ],
    }


if __name__ == "__main__":
    unittest.main()
