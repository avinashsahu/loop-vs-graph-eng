import unittest
from unittest.mock import patch

import fundamentals


class FundamentalSnapshotTests(unittest.TestCase):
    def test_cached_snapshot_reloads_independently_warmed_disclosures(self):
        cached = {
            "complete": True,
            "material_disclosures": {"status": "pending"},
        }
        warmed = {
            "status": "ready",
            "events": [],
            "credit_ratings": [],
        }

        with (
            patch.object(fundamentals.cache, "read", return_value=cached),
            patch.object(
                fundamentals,
                "get_material_disclosures",
                return_value=warmed,
            ) as disclosure_read,
        ):
            result = fundamentals.get_fundamental_snapshot("TEST")

        self.assertIs(result, cached)
        self.assertIs(result["material_disclosures"], warmed)
        disclosure_read.assert_called_once_with("TEST")


if __name__ == "__main__":
    unittest.main()
