import unittest

import numpy as np

from ta_analysis import _clip


class ClipTests(unittest.TestCase):
    def test_nan_is_neutral(self):
        self.assertEqual(_clip(float("nan")), 0.0)
        self.assertEqual(_clip(np.nan), 0.0)

    def test_valid_values_keep_default_clipping_behavior(self):
        self.assertEqual(_clip(-2.0), -1.0)
        self.assertEqual(_clip(0.25), 0.25)
        self.assertEqual(_clip(2.0), 1.0)

    def test_valid_values_keep_custom_clipping_behavior(self):
        self.assertEqual(_clip(-1.0, lo=0.0, hi=10.0), 0.0)
        self.assertEqual(_clip(4.0, lo=0.0, hi=10.0), 4.0)
        self.assertEqual(_clip(11.0, lo=0.0, hi=10.0), 10.0)


if __name__ == "__main__":
    unittest.main()
