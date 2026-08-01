import unittest

from governance_filings import (
    GovernanceHistoryService,
    _parse_governance_xbrl,
    _select_governance_filings,
)


def _governance_xbrl(
    *,
    board_compliant="true",
    audit_compliant="true",
    cyber_incident="false",
    director_disqualified="false",
    pending_complaints="0",
    violation=None,
    include_appointment=True,
):
    appointment = ""
    if include_appointment:
        appointment = """
        <gov:DateOfAppointmentOfDirector contextRef="D1">2024-01-15</gov:DateOfAppointmentOfDirector>
        <gov:NameOftheDirector contextRef="D1">Example Director</gov:NameOftheDirector>
        """
    violation_fact = ""
    if violation:
        violation_fact = f"""
        <gov:DetailsOfTheViolationOrContraventionCommittedOrAllegedToBeCommitted
          contextRef="Main">{violation}</gov:DetailsOfTheViolationOrContraventionCommittedOrAllegedToBeCommitted>
        """
    return f"""<?xml version="1.0"?>
    <xbrli:xbrl
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:link="http://www.xbrl.org/2003/linkbase"
      xmlns:xlink="http://www.w3.org/1999/xlink"
      xmlns:gov="https://www.nseindia.com/xbrl/governance">
      <link:schemaRef xlink:href="in-capmkt-ent-2024-12-31.xsd"/>
      <xbrli:context id="Main">
        <xbrli:entity><xbrli:identifier scheme="test">RELIANCE</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
      </xbrli:context>
      <xbrli:context id="D1">
        <xbrli:entity><xbrli:identifier scheme="test">RELIANCE</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
      </xbrli:context>
      <gov:TheCompositionOfBoardOfDirectorsIsInTermsOfSebiRegulations2015
        contextRef="Main">{board_compliant}</gov:TheCompositionOfBoardOfDirectorsIsInTermsOfSebiRegulations2015>
      <gov:TheCompositionOfAuditCommitteeIsInTermsOfSebiRegulations2015
        contextRef="Main">{audit_compliant}</gov:TheCompositionOfAuditCommitteeIsInTermsOfSebiRegulations2015>
      <gov:WhetherAsPerSubRegulation2baOfRegulation27OfSEBILODRThereHasBeenCyberSecurityIncidentsDuringTheQuarter
        contextRef="Main">{cyber_incident}</gov:WhetherAsPerSubRegulation2baOfRegulation27OfSEBILODRThereHasBeenCyberSecurityIncidentsDuringTheQuarter>
      <gov:WhetherTheDirectorIsDisqualified contextRef="D1">{director_disqualified}</gov:WhetherTheDirectorIsDisqualified>
      <gov:NoOfInvestorComplaints contextRef="Main">{pending_complaints}</gov:NoOfInvestorComplaints>
      {appointment}
      {violation_fact}
    </xbrli:xbrl>""".encode()


class _Source:
    def __init__(self, filings):
        self.filings = filings
        self.downloads = []

    def list_filings(self, symbol):
        return self.filings

    def download(self, filing):
        self.downloads.append(filing["record_id"])
        if filing.get("error"):
            raise filing["error"]
        return filing["xml"]


class _Store:
    def __init__(self):
        self.records = {}

    def read(self, symbol, ttl_seconds):
        return self.records.get(symbol.upper())

    def write(self, symbol, payload):
        self.records[symbol.upper()] = payload


class GovernanceFilingTests(unittest.TestCase):
    def test_selects_revised_governance_over_original(self):
        filings = _select_governance_filings(
            [
                {
                    "type": "Integrated Filing- Governance",
                    "qe_Date": "30-JUN-2026",
                    "xbrl": "https://example.invalid/a.xml",
                    "seq_Id": "1",
                    "type_Sub": "New",
                    "broadcast_Date": "01-Jul-2026 10:00:00",
                },
                {
                    "type": "Integrated Filing- Governance",
                    "qe_Date": "30-JUN-2026",
                    "xbrl": "https://example.invalid/b.xml",
                    "seq_Id": "2",
                    "type_Sub": "Revision",
                    "broadcast_Date": "02-Jul-2026 10:00:00",
                },
                {
                    "type": "Integrated Filing- Financials",
                    "qe_Date": "30-JUN-2026",
                    "xbrl": "https://example.invalid/c.xml",
                    "seq_Id": "3",
                    "type_Sub": "New",
                    "broadcast_Date": "03-Jul-2026 10:00:00",
                },
            ]
        )
        self.assertEqual([f["record_id"] for f in filings], ["2"])
        self.assertTrue(filings[0]["revised"])

    def test_parses_exceptions_and_ignores_director_rotation(self):
        period = _parse_governance_xbrl(
            record_id="179404",
            period="2026-06-30",
            payload=_governance_xbrl(
                board_compliant="false",
                pending_complaints="3",
                violation="Penalty for delayed filing.",
                cyber_incident="true",
            ),
            source_url="https://example.invalid/gov.xml",
            revised=False,
            published_at="28-Jul-2026 18:43:22",
        )
        codes = {item["code"] for item in period["exceptions"]}
        self.assertIn("board_composition_non_compliance", codes)
        self.assertIn("investor_grievance_pending", codes)
        self.assertIn("governance_violation_or_contravention", codes)
        self.assertIn("cyber_security_incident", codes)
        self.assertTrue(
            all(item["id"].startswith("GOVERNANCE_") for item in period["exceptions"])
        )
        self.assertTrue(
            all(
                item["policy_reason_code"] == "GOVERNANCE_DISCLOSURE_CAUTION"
                for item in period["exceptions"]
            )
        )

    def test_ordinary_compliant_filing_has_no_exceptions(self):
        period = _parse_governance_xbrl(
            record_id="1",
            period="2026-06-30",
            payload=_governance_xbrl(),
            source_url="https://example.invalid/gov.xml",
            revised=False,
            published_at=None,
        )
        self.assertEqual(period["exceptions"], [])

    def test_one_malformed_filing_does_not_stop_warmer(self):
        source = _Source(
            [
                {
                    "record_id": "bad",
                    "period": "2026-03-31",
                    "url": "https://example.invalid/bad.xml",
                    "xml": b"<not-xml",
                },
                {
                    "record_id": "good",
                    "period": "2026-06-30",
                    "url": "https://example.invalid/good.xml",
                    "xml": _governance_xbrl(board_compliant="false"),
                },
            ]
        )
        store = _Store()
        result = GovernanceHistoryService(source, store).warm("RELIANCE")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["periods_available"], 1)
        self.assertEqual(len(result["exceptions"]), 1)
        self.assertTrue(any(error.startswith("bad:") for error in result["errors"]))
        self.assertEqual(source.downloads, ["bad", "good"])

    def test_live_get_is_cache_only(self):
        source = _Source([])
        store = _Store()
        service = GovernanceHistoryService(
            source, store, download_missing=False
        )
        pending = service.get("RELIANCE")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(source.downloads, [])
        store.write(
            "RELIANCE",
            {
                "status": "ready",
                "symbol": "RELIANCE",
                "periods": [],
                "exceptions": [],
                "errors": [],
                "periods_available": 0,
            },
        )
        ready = service.get("RELIANCE")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(source.downloads, [])


if __name__ == "__main__":
    unittest.main()
