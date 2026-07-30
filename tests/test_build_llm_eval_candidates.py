import json
import tempfile
import unittest
from pathlib import Path

from build_llm_eval_candidates import build_candidates


class CandidateBuilderTests(unittest.TestCase):
    def test_deduplicates_snapshots_and_stratifies_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "data": {
                    "company_name": "Example Limited",
                    "corp_announcements": [
                        {
                            "symbol": "EXAMPLE",
                            "desc": "Credit Rating- Revision",
                            "an_dt": "30-Jul-2026",
                            "attchmntText": (
                                "Example Limited has informed the Exchange "
                                "about Credit Rating- Revision"
                            ),
                            "attchmntFile": "https://example.test/rating.pdf",
                        }
                    ],
                    "corp_actions": [
                        {
                            "symbol": "EXAMPLE",
                            "subject": "Interim Dividend - Rs 2 Per Share",
                            "exDate": "01-Aug-2026",
                            "recDate": "01-Aug-2026",
                        }
                    ],
                }
            }
            for name in ("fundamentals_EXAMPLE_1.json", "fundamentals_EXAMPLE_2.json"):
                (root / name).write_text(json.dumps(payload))

            candidates = build_candidates(sorted(root.glob("*.json")))

        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {item["event_family"] for item in candidates},
            {"credit_rating", "routine_distribution_or_meeting"},
        )
        self.assertTrue(
            all(item["label_status"] == "needs_human_review" for item in candidates)
        )


if __name__ == "__main__":
    unittest.main()
