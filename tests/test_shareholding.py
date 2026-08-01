import unittest
from unittest.mock import patch

from shareholding import (
    NseShareholdingRequestError,
    NseShareholdingSource,
    ShareholdingError,
    ShareholdingHistoryService,
    select_due_universe_symbols,
)


def _xbrl(
    schema_date,
    period,
    fii,
    dii,
    government,
    other,
    schema_family="in-bse-shp",
    instant_period=None,
    government_member="Governments",
):
    instant_period = instant_period or period
    public = fii + dii + government + other
    members = {
        "InstitutionsForeign": fii,
        "InstitutionsDomestic": dii,
        government_member: government,
        "NonInstitutions": other,
        "PublicShareholding": public,
    }
    contexts = "".join(
        f"""
        <xbrli:context id="{name}_ContextI">
          <xbrli:entity><xbrli:identifier scheme="test">FEDERALBNK</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>{instant_period}</xbrli:instant></xbrli:period>
          <xbrli:scenario>
            <xbrldi:explicitMember dimension="shp:CategoryOfShareholdersAxis">
              shp:{name}Member
            </xbrldi:explicitMember>
          </xbrli:scenario>
        </xbrli:context>
        """
        for name in members
    )
    facts = "".join(
        f"""
        <shp:NumberOfShares contextRef="{name}_ContextI" unitRef="shares">{shares}</shp:NumberOfShares>
        <shp:ShareholdingAsAPercentageOfTotalNumberOfShares
          contextRef="{name}_ContextI">{shares / public * 100}</shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
        """
        for name, shares in members.items()
    )
    return f"""<?xml version="1.0"?>
    <xbrli:xbrl
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:link="http://www.xbrl.org/2003/linkbase"
      xmlns:xlink="http://www.w3.org/1999/xlink"
      xmlns:shp="https://www.sebi.gov.in/xbrl/SHP_Exchange_Specific/{schema_date}/{schema_family}">
      <link:schemaRef xlink:href="{schema_family}-{schema_date}.xsd"/>
      {contexts}
      {facts}
    </xbrli:xbrl>""".encode()


class _Source:
    def __init__(self, filings):
        self.filings = [
            {
                "url": f"https://example.invalid/{filing['record_id']}.xml",
                **filing,
            }
            for filing in filings
        ]
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
        self.manifests = {}
        self.fresh = True

    def get(self, record_id):
        return self.records.get(record_id)

    def put(self, record_id, record):
        self.records[record_id] = record

    def update_normalized(self, record_id, period):
        self.records[record_id]["normalized"] = period.__dict__

    def get_manifest(self, symbol, *, allow_stale=False):
        return self.manifests.get(symbol)

    def manifest_is_fresh(self, symbol):
        return self.fresh and symbol in self.manifests

    def put_manifest(self, symbol, filings):
        self.manifests[symbol] = filings

    def enqueue_warm(self, symbol, record_ids):
        pass

    def queued_symbols(self):
        return []

    def complete_warm(self, symbol):
        pass


class ShareholdingHistoryTests(unittest.TestCase):
    def test_one_missing_archive_keeps_other_verified_quarters(self):
        filings = [
            {
                "record_id": str(index),
                "period": period,
                "xml": _xbrl(
                    "2025-10-31",
                    period,
                    250_000,
                    500_000,
                    0,
                    250_000,
                ),
            }
            for index, period in enumerate(
                (
                    "2026-06-30",
                    "2026-03-31",
                    "2025-12-31",
                    "2025-09-30",
                ),
                start=1,
            )
        ]
        filings.append(
            {
                "record_id": "5",
                "period": "2025-06-30",
                "error": NseShareholdingRequestError("archive returned 404"),
            }
        )

        history = ShareholdingHistoryService(
            _Source(filings), _Store()
        ).get("EXAMPLE", periods=5)

        self.assertEqual(history.periods_available, 4)
        self.assertFalse(history.complete)
        self.assertEqual(
            [period.period for period in history.periods],
            [
                "2026-06-30",
                "2026-03-31",
                "2025-12-31",
                "2025-09-30",
            ],
        )

    def test_maps_legacy_misspelled_government_member(self):
        filing = {
            "record_id": "196330",
            "period": "2025-03-31",
            "xml": _xbrl(
                "2022-09-30",
                "2025-03-31",
                956_639_114,
                1_074_860_351,
                43_390,
                1_330_560_015,
                government_member="Goverments",
            ),
        }

        history = ShareholdingHistoryService(
            _Source([filing]), _Store()
        ).get("CANBK", periods=1)

        self.assertTrue(history.periods[0].reconciled)
        self.assertEqual(history.periods[0].government_pct, 0.0013)

    @patch("shareholding.time.sleep")
    @patch("shareholding.get_request", return_value=None)
    def test_source_does_not_multiply_the_scraper_retry_loop(
        self, get_request, _sleep
    ):
        source = NseShareholdingSource(
            request_delay_seconds=0,
            jitter_seconds=0,
        )

        with self.assertRaises(NseShareholdingRequestError):
            source.download({"url": "https://example.invalid/missing.xml"})

        get_request.assert_called_once()

    def test_nonquarter_and_missing_xbrl_filings_do_not_displace_history(self):
        quarterly_filings = [
            {
                "record_id": str(record_id),
                "period": period,
                "xml": _xbrl(
                    "2025-10-31",
                    period,
                    250_000,
                    500_000,
                    0,
                    250_000,
                ),
            }
            for record_id, period in enumerate(
                (
                    "2025-06-30",
                    "2025-09-30",
                    "2025-12-31",
                    "2026-03-31",
                    "2026-06-30",
                ),
                start=100,
            )
        ]
        event_filing = {
            "record_id": "999",
            "period": "2026-06-18",
            "xml": _xbrl(
                "2025-10-31",
                "2026-06-18",
                250_000,
                500_000,
                0,
                250_000,
            ),
        }
        missing_xbrl_filing = {
            "record_id": "1000",
            "period": "2026-09-30",
            "url": "https://nsearchives.nseindia.com/corporate/xbrl/-",
        }
        source = _Source(
            [*quarterly_filings, event_filing, missing_xbrl_filing]
        )

        history = ShareholdingHistoryService(source, _Store()).get(
            "EXAMPLE", periods=5
        )

        self.assertTrue(history.complete)
        self.assertEqual(
            [period.period for period in history.periods],
            [
                "2026-06-30",
                "2026-03-31",
                "2025-12-31",
                "2025-09-30",
                "2025-06-30",
            ],
        )
        self.assertNotIn("999", source.downloads)

    def test_universe_backfill_prefers_never_warmed_then_stale_active_symbols(self):
        records = [
            {
                "universe": "NIFTY TOTAL MKT",
                "symbol": "NEW",
                "active": 1,
                "completed_at": 0,
            },
            {
                "universe": "NIFTY TOTAL MKT",
                "symbol": "STALE",
                "active": 1,
                "completed_at": 100,
            },
            {
                "universe": "NIFTY TOTAL MKT",
                "symbol": "FRESH",
                "active": 1,
                "completed_at": 950,
            },
            {
                "universe": "NIFTY TOTAL MKT",
                "symbol": "INCOMPLETE_RECENT",
                "active": 1,
                "completed_at": 0,
                "last_status": "incomplete",
                "last_attempt": 900,
            },
            {
                "universe": "NIFTY TOTAL MKT",
                "symbol": "INCOMPLETE_STALE",
                "active": 1,
                "completed_at": 0,
                "last_status": "incomplete",
                "last_attempt": 400,
            },
            {
                "universe": "NIFTY TOTAL MKT",
                "symbol": "REMOVED",
                "active": 0,
                "completed_at": 0,
            },
            {
                "universe": "OTHER",
                "symbol": "OTHER",
                "active": 1,
                "completed_at": 0,
            },
        ]

        self.assertEqual(
            select_due_universe_symbols(
                records,
                universe="NIFTY TOTAL MKT",
                now_epoch=1_000,
                refresh_after_seconds=500,
                incomplete_retry_seconds=500,
                limit=3,
            ),
            ["NEW", "STALE", "INCOMPLETE_STALE"],
        )

    def test_parses_both_validated_taxonomies_and_reconciles_public_shares(self):
        filings = [
            {
                "record_id": "201186",
                "period": "2025-06-30",
                "xml": _xbrl("2025-05-31", "2025-06-30", 268_650, 481_749, 0, 249_601),
            },
            {
                "record_id": "203018",
                "period": "2025-09-30",
                "xml": _xbrl(
                    "2025-05-31",
                    "2025-09-30",
                    255_437,
                    497_117,
                    0,
                    247_446,
                    instant_period="2025-10-01",
                ),
            },
            {
                "record_id": "205713",
                "period": "2025-12-31",
                "xml": _xbrl(
                    "2025-10-31",
                    "2025-12-31",
                    249_369,
                    511_054,
                    0,
                    239_577,
                    "in-capmkt",
                ),
            },
            {
                "record_id": "209992",
                "period": "2026-03-31",
                "xml": _xbrl(
                    "2025-10-31",
                    "2026-03-31",
                    635_495_027,
                    1_229_167_746,
                    69_754,
                    574_529_069,
                    "in-capmkt",
                ),
            },
            {
                "record_id": "212913",
                "period": "2026-06-30",
                "xml": _xbrl(
                    "2025-10-31",
                    "2026-06-30",
                    277_134,
                    492_796,
                    25,
                    230_045,
                    "in-capmkt",
                ),
            },
        ]

        history = ShareholdingHistoryService(_Source(filings), _Store()).get(
            "FEDERALBNK", periods=5
        )

        self.assertEqual(
            [period.schema_version for period in history.periods],
            [
                "2025-10-31",
                "2025-10-31",
                "2025-10-31",
                "2025-05-31",
                "2025-05-31",
            ],
        )
        self.assertEqual(
            [
                (
                    period.period,
                    period.fii_pct,
                    period.dii_pct,
                    period.government_pct,
                    period.other_public_pct,
                )
                for period in history.periods
            ],
            [
                ("2026-06-30", 27.7134, 49.2796, 0.0025, 23.0045),
                ("2026-03-31", 26.0528, 50.391, 0.0029, 23.5534),
                ("2025-12-31", 24.9369, 51.1054, 0.0, 23.9577),
                ("2025-09-30", 25.5437, 49.7117, 0.0, 24.7446),
                ("2025-06-30", 26.865, 48.1749, 0.0, 24.9601),
            ],
        )
        self.assertTrue(all(period.reconciled for period in history.periods))
        self.assertTrue(history.complete)
        self.assertEqual(history.latest_record_id, "212913")
        self.assertEqual(history.changes_bps["fii_4q"], 85)
        self.assertEqual(history.changes_bps["government_qoq"], 0)
        self.assertIn("government", history.trend_labels)

        gapped_filings = [
            {
                **filing,
                "period": "2025-03-31",
                "xml": _xbrl(
                    "2025-05-31", "2025-03-31", 268_650, 481_749, 0, 249_601
                ),
            }
            if filing["period"] == "2025-06-30"
            else filing
            for filing in filings
        ]
        gapped = ShareholdingHistoryService(_Source(gapped_filings), _Store()).get(
            "FEDERALBNK", periods=5
        )
        self.assertFalse(gapped.complete)

    def test_second_history_fetch_uses_cached_filings_without_xbrl_downloads(self):
        filings = [
            {
                "record_id": "201186",
                "period": "2025-06-30",
                "xml": _xbrl("2025-05-31", "2025-06-30", 268_650, 481_749, 0, 249_601),
            }
        ]
        source = _Source(filings)
        store = _Store()
        service = ShareholdingHistoryService(source, store)

        first = service.get("FEDERALBNK")
        second = service.get("FEDERALBNK")

        self.assertEqual(first, second)
        self.assertEqual(source.downloads, ["201186"])

        store.fresh = False
        stale = ShareholdingHistoryService(
            source, store, download_missing=False
        ).get("FEDERALBNK")
        self.assertEqual(stale.status, "pending")
        self.assertFalse(stale.complete)

    def test_incomplete_xbrl_is_retryable_and_never_successfully_cached(self):
        incomplete = _xbrl(
            "2025-10-31", "2025-12-31", 249_369, 511_054, 0, 239_577
        ).replace(b"shp:InstitutionsForeignMember", b"shp:UnknownMember")
        source = _Source(
            [
                {
                    "record_id": "205713",
                    "period": "2025-12-31",
                    "xml": incomplete,
                }
            ]
        )
        store = _Store()
        service = ShareholdingHistoryService(source, store)

        with self.assertRaises(ShareholdingError):
            service.get("FEDERALBNK")
        with self.assertRaises(ShareholdingError):
            service.get("FEDERALBNK")

        self.assertEqual(source.downloads, ["205713", "205713"])
        self.assertEqual(store.records, {})


if __name__ == "__main__":
    unittest.main()
