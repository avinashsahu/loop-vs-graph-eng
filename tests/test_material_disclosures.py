import unittest
from datetime import date

from material_disclosures import MaterialDisclosureService


class _Store:
    def __init__(self):
        self.payload = None

    def read(self, _symbol, _ttl_seconds):
        return self.payload

    def write(self, _symbol, payload):
        self.payload = payload


class _Source:
    def announcements(self, _symbol, _start, _end):
        return [
            {
                "an_dt": "30-Jul-2026 10:00:00",
                "desc": "SEBI Order",
                "attchmntText": "SEBI imposed a monetary penalty.",
                "attchmntFile": "https://example.test/order.pdf",
                "seq_id": "1",
            },
            {
                "an_dt": "30-Jul-2026 09:55:00",
                "desc": "SEBI Order",
                "attchmntText": "Duplicate revision of the same SEBI penalty.",
                "attchmntFile": "https://example.test/order-revision.pdf",
                "seq_id": "1-revision",
            },
            {
                "an_dt": "29-Jul-2026 10:00:00",
                "desc": "Analyst meet",
                "attchmntText": "Schedule of investor conference call.",
                "attchmntFile": "https://example.test/call.pdf",
                "seq_id": "2",
            },
            {
                "an_dt": "29-Jul-2026 09:00:00",
                "desc": "Newspaper Publication",
                "attchmntText": "Routine newspaper advertisement.",
                "attchmntFile": "https://example.test/newspaper.pdf",
                "seq_id": "2-newspaper",
            },
            {
                "an_dt": "28-Jul-2026 10:00:00",
                "desc": "Issue of Securities",
                "attchmntText": "Allotment of non-convertible debentures.",
                "attchmntFile": "https://example.test/ncd.pdf",
                "seq_id": "3",
            },
        ]

    def credit_ratings(self, _symbol, _start, _end):
        return [
            {
                "creditAgencyName": "CRISIL",
                "ratingAssigned": "Working Capital",
                "creditRating": "CRISIL AA",
                "outlook": "Negative",
                "currentAction": "Downgraded",
                "dateOfCurrentCredit": "28-Jul-2026",
                "amount": "125.5",
                "detailsOfRatingLink": "https://example.test/rating",
                "isin": "INE000000001",
            },
            {
                "creditAgencyName": "ICRA",
                "ratingAssigned": "Term Loan",
                "creditRating": "[ICRA]D",
                "outlook": None,
                "currentAction": "Assigned",
                "dateOfCurrentCredit": "27-Jul-2026",
                "amount": "50",
                "detailsOfRatingLink": "https://example.test/default",
                "isin": "INE000000002",
            },
            {
                "creditAgencyName": "CARE",
                "ratingAssigned": "Bank Facilities",
                "creditRating": "CARE BB",
                "outlook": "Stable",
                "currentAction": "Withdrawn - Issuer Not Cooperating",
                "dateOfCurrentCredit": "26-Jul-2026",
                "amount": "75",
                "detailsOfRatingLink": "https://example.test/non-cooperation",
                "isin": "INE000000003",
            },
        ]


class MaterialDisclosureTests(unittest.TestCase):
    def test_warm_filters_routine_items_and_normalizes_rating_policy(self):
        store = _Store()
        service = MaterialDisclosureService(_Source(), store)

        result = service.warm("TEST", as_of=date(2026, 7, 31))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["event_type"], "regulatory_action")
        self.assertTrue(result["events"][0]["id"].startswith("MATERIAL_"))
        self.assertEqual(
            result["events"][0]["source"]["attachment_status"],
            "referenced",
        )
        actions = {
            rating["action_direction"]: rating
            for rating in result["credit_ratings"]
        }
        self.assertEqual(actions["default"]["policy_verdict"], "REJECT")
        self.assertEqual(actions["downgrade"]["policy_verdict"], "REVIEW")
        self.assertEqual(
            actions["non_cooperation"]["policy_verdict"],
            "REVIEW",
        )
        self.assertEqual(actions["downgrade"]["amount_crore"], 125.5)
        self.assertIs(service.get("TEST"), result)

    def test_one_source_failure_keeps_available_material_evidence(self):
        class PartialSource(_Source):
            def credit_ratings(self, _symbol, _start, _end):
                raise ConnectionError("rating endpoint unavailable")

        store = _Store()
        result = MaterialDisclosureService(PartialSource(), store).warm(
            "TEST",
            as_of=date(2026, 7, 31),
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["credit_ratings"], [])
        self.assertIn("credit_ratings:ConnectionError", result["errors"])
        self.assertIs(store.payload, result)


if __name__ == "__main__":
    unittest.main()
