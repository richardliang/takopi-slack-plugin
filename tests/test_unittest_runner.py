import os
import unittest


class PytestRunner(unittest.TestCase):
    def test_pytest_suite(self) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            self.skipTest("pytest runner executes this suite directly")
        import pytest

        result = pytest.main(["-q"])
        self.assertEqual(result, 0)
