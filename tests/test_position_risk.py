import unittest

import position_risk


class PositionRiskTests(unittest.TestCase):
    def test_position_is_capped_by_loss_at_an_atr_based_stop(self):
        result = position_risk.size_position(
            principal=100_000.0,
            entry_price=100.0,
            atr=5.0,
            max_loss_pct=1.0,
            max_allocation_pct=25.0,
            atr_stop_multiple=2.0,
        )

        self.assertIsInstance(result, position_risk.PositionPlan)
        self.assertEqual(result.shares, 100)
        self.assertEqual(result.stop_price, 90.0)
        self.assertEqual(result.target_price, 120.0)
        self.assertEqual(result.reward_risk_ratio, 2.0)
        self.assertEqual(result.planned_profit_at_target, 2_000.0)
        self.assertEqual(result.capital_required, 10_000.0)
        self.assertEqual(result.max_loss_at_stop, 1_000.0)
        self.assertEqual(result.risk_budget, 1_000.0)
        self.assertEqual(result.binding_constraint, "risk_budget")

    def test_position_is_rejected_when_constraints_allow_zero_shares(self):
        result = position_risk.size_position(
            principal=10_000.0,
            entry_price=1_000.0,
            atr=50.0,
            max_loss_pct=1.0,
            max_allocation_pct=1.0,
            atr_stop_multiple=2.0,
        )

        self.assertIsInstance(result, position_risk.RiskRejection)
        self.assertEqual(result.reason_code, "ZERO_SHARES")

    def test_invalid_atr_is_rejected(self):
        result = position_risk.size_position(
            principal=100_000.0,
            entry_price=100.0,
            atr=float("nan"),
            max_loss_pct=1.0,
            max_allocation_pct=10.0,
            atr_stop_multiple=2.0,
        )

        self.assertIsInstance(result, position_risk.RiskRejection)
        self.assertEqual(result.reason_code, "INVALID_INPUT")

    def test_nonpositive_reward_risk_ratio_is_rejected(self):
        result = position_risk.size_position(
            principal=100_000.0,
            entry_price=100.0,
            atr=5.0,
            max_loss_pct=1.0,
            max_allocation_pct=10.0,
            atr_stop_multiple=2.0,
            reward_risk_ratio=0.0,
        )

        self.assertIsInstance(result, position_risk.RiskRejection)
        self.assertEqual(result.reason_code, "INVALID_INPUT")
        self.assertIn("reward_risk_ratio", result.message)

    def test_excessive_reward_risk_ratio_is_rejected(self):
        result = position_risk.size_position(
            principal=100_000.0,
            entry_price=100.0,
            atr=5.0,
            max_loss_pct=1.0,
            max_allocation_pct=10.0,
            atr_stop_multiple=2.0,
            reward_risk_ratio=11.0,
        )

        self.assertIsInstance(result, position_risk.RiskRejection)
        self.assertEqual(result.reason_code, "REWARD_RISK_RATIO_TOO_HIGH")

    def test_overflowing_share_quotas_are_rejected_without_raising(self):
        result = position_risk.size_position(
            principal=1e308,
            entry_price=1e-308,
            atr=1e-310,
            max_loss_pct=1.0,
            max_allocation_pct=100.0,
            atr_stop_multiple=2.0,
            reward_risk_ratio=2.0,
        )

        self.assertIsInstance(result, position_risk.RiskRejection)
        self.assertEqual(result.reason_code, "INVALID_DERIVED_PLAN")


if __name__ == "__main__":
    unittest.main()
