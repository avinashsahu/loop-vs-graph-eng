import unittest
from unittest.mock import patch

import financial_results


def _filing(
    *,
    scope,
    period,
    url,
    subtype="Original",
    broadcast="30-Jul-2026 19:00:00",
    audited="Un-Audited",
):
    return {
        "type": "Integrated Filing- Financials",
        "type_Sub": subtype,
        "consolidated": scope.title(),
        "qe_Date": period,
        "broadcast_Date": broadcast,
        "audited": audited,
        "xbrl": url,
    }


class FinancialScopeTests(unittest.TestCase):
    def test_target_profiles_choose_explicit_scope(self):
        scopes = ("standalone", "consolidated")
        cases = (
            (
                "BAJFINANCE",
                "Bajaj Finance Limited",
                "operating_nbfc",
                "standalone",
                "regulated_entity_metrics",
            ),
            (
                "BAJAJFINSV",
                "Bajaj Finserv Limited",
                "financial_holding_group",
                "consolidated",
                "group_economics",
            ),
            (
                "JIOFIN",
                "Jio Financial Services Limited",
                "financial_holding_group",
                "consolidated",
                "group_economics",
            ),
        )

        for symbol, name, expected_profile, expected_scope, expected_reason in cases:
            with self.subTest(symbol=symbol):
                profile = financial_results._entity_profile(
                    symbol,
                    name,
                    "nbfc",
                    scopes,
                )
                scope, reason = financial_results._select_scope(profile, scopes)
                self.assertEqual(profile, expected_profile)
                self.assertEqual(scope, expected_scope)
                self.assertEqual(reason, expected_reason)

    def test_history_retains_both_scopes_and_prefers_revision(self):
        rows = [
            _filing(
                scope="consolidated",
                period="30-JUN-2026",
                url="https://example.test/NBFC/consolidated-june.xml",
            ),
            _filing(
                scope="standalone",
                period="30-JUN-2026",
                url="https://example.test/NBFC/standalone-original.xml",
            ),
            _filing(
                scope="standalone",
                period="30-JUN-2026",
                url="https://example.test/NBFC/standalone-revision.xml",
                subtype="Revision",
                broadcast=None,
                audited="Audited",
            ),
            _filing(
                scope="consolidated",
                period="31-MAR-2026",
                url="https://example.test/NBFC/consolidated-march.xml",
                audited="Audited",
            ),
        ]

        with (
            patch.object(
                financial_results,
                "_get_json",
                return_value={"data": rows},
            ),
            patch.object(
                financial_results,
                "_get_bytes",
                return_value=b"<xbrl/>",
            ) as get_bytes,
            patch.object(
                financial_results,
                "_parse_period",
                side_effect=lambda _xml, period, _subtype: {
                    "period_end": period,
                    "pat": 1.0,
                    **(
                        {
                            "funding_leverage": {
                                "ratio": 1.0,
                                "method": "reported_debt_to_equity",
                            }
                        }
                        if period.endswith("-03-31")
                        else {}
                    ),
                },
            ),
        ):
            history = financial_results.get_financial_history(
                "JIOFIN",
                company_name="Jio Financial Services Limited",
            )

        self.assertEqual(history["status"], "ready")
        self.assertEqual(history["selected_scope"], "consolidated")
        self.assertEqual(history["scope_selection_reason"], "group_economics")
        self.assertEqual(
            history["available_scopes"],
            ["standalone", "consolidated"],
        )
        self.assertEqual(get_bytes.call_count, 2)
        standalone = history["scope_histories"]["standalone"]
        self.assertEqual(standalone["status"], "metadata_only")
        self.assertEqual(
            standalone["sources"][0]["revision_type"],
            "Revision",
        )
        self.assertEqual(
            standalone["sources"][0]["url"],
            "https://example.test/NBFC/standalone-revision.xml",
        )
        self.assertEqual(standalone["sources"][0]["audit_status"], "Audited")
        self.assertEqual(
            history["scope_histories"]["consolidated"]["periods"],
            history["periods"],
        )
        leverage = history["periods"][1]["funding_leverage"]
        self.assertEqual(leverage["scope"], "consolidated")
        self.assertEqual(leverage["period_end"], "2026-03-31")
        self.assertEqual(
            leverage["source_url"],
            "https://example.test/NBFC/consolidated-march.xml",
        )
        self.assertTrue(leverage["source_sha256"])
        self.assertTrue(
            all(source["scope"] == "consolidated" for source in history["sources"])
        )

    def test_nbfc_annual_funding_leverage_requires_reconciled_balance_sheet(self):
        xml = b"""
        <xbrl>
          <RevenueFromOperations contextRef="OneD">100</RevenueFromOperations>
          <Income contextRef="OneD">120</Income>
          <Expenses contextRef="OneD">90</Expenses>
          <FinanceCosts contextRef="OneD">20</FinanceCosts>
          <ImpairmentOnFinancialInstruments contextRef="OneD">4</ImpairmentOnFinancialInstruments>
          <ProfitLossForPeriod contextRef="OneD">15</ProfitLossForPeriod>
          <Assets contextRef="OneI">500</Assets>
          <Liabilities contextRef="OneI">400</Liabilities>
          <FinancialLiabilities contextRef="OneI">380</FinancialLiabilities>
          <Equity contextRef="OneI">100</Equity>
          <DebtSecurities contextRef="OneI">100</DebtSecurities>
          <Borrowings contextRef="OneI">150</Borrowings>
          <Deposits contextRef="OneI">80</Deposits>
          <SubordinatedLiabilities contextRef="OneI">20</SubordinatedLiabilities>
          <OtherFinancialLiabilities contextRef="OneI">30</OtherFinancialLiabilities>
        </xbrl>
        """

        period = financial_results._parse_period(
            xml,
            "2026-03-31",
            "nbfc",
        )

        leverage = period["funding_leverage"]
        self.assertEqual(leverage["ratio"], 3.5)
        self.assertEqual(
            leverage["method"],
            "derived_funding_liabilities_to_equity",
        )
        self.assertEqual(leverage["funding_liabilities"], 350.0)
        self.assertTrue(leverage["balance_sheet_reconciled"])
        self.assertTrue(leverage["funding_components_reconciled"])
        self.assertEqual(
            leverage["components"],
            {
                "debt_securities": 100.0,
                "borrowings": 150.0,
                "deposits": 80.0,
                "subordinated_liabilities": 20.0,
            },
        )

    def test_nbfc_reported_ratio_is_preferred_when_reconciled(self):
        xml = b"""
        <xbrl>
          <Assets contextRef="OneI">500</Assets>
          <Liabilities contextRef="OneI">400</Liabilities>
          <FinancialLiabilities contextRef="OneI">380</FinancialLiabilities>
          <Equity contextRef="OneI">100</Equity>
          <DebtSecurities contextRef="OneI">100</DebtSecurities>
          <Borrowings contextRef="OneI">150</Borrowings>
          <OtherFinancialLiabilities contextRef="OneI">130</OtherFinancialLiabilities>
          <DebtEquityRatio contextRef="OneD">2.75</DebtEquityRatio>
        </xbrl>
        """

        leverage = financial_results._parse_period(
            xml,
            "2026-03-31",
            "nbfc",
        )["funding_leverage"]

        self.assertEqual(leverage["ratio"], 2.75)
        self.assertEqual(leverage["method"], "reported_debt_to_equity")
        self.assertEqual(leverage["reported_debt_to_equity"], 2.75)

    def test_two_annual_periods_are_kept_when_manifest_has_them(self):
        periods = (
            "30-JUN-2026",
            "31-MAR-2026",
            "31-DEC-2025",
            "30-SEP-2025",
            "30-JUN-2025",
            "31-MAR-2025",
        )
        rows = [
            _filing(
                scope="standalone",
                period=period,
                url=f"https://example.test/NBFC/{period}.xml",
            )
            for period in periods
        ]

        selected = financial_results._select_filings(rows)["standalone"]

        self.assertEqual(
            [row["period_end"] for row in selected],
            [
                "2026-06-30",
                "2026-03-31",
                "2025-12-31",
                "2025-09-30",
                "2025-03-31",
            ],
        )


if __name__ == "__main__":
    unittest.main()
