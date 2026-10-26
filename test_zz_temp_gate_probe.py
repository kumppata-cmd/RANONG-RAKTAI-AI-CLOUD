"""TEMPORARY — proves the CI test gate fails the build on a failing test.

This file exists only to verify TECHNICAL_DEBT_REGISTER TD-2 acceptance
criteria ("prove by temporarily breaking one test"). It is deleted
immediately after the CI run is observed, and is never merged to main.
"""
import unittest


class TestCIGateActuallyBites(unittest.TestCase):
    def test_deliberate_failure_to_prove_gate(self):
        self.assertEqual(
            1, 2,
            "DELIBERATE FAILURE — if CI is green with this present, "
            "the test gate is not wired up correctly.",
        )
