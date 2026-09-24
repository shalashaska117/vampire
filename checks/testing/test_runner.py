#!/usr/bin/env python3
"""Check result classification so the harness cannot turn failures into passes."""
import itertools
from pathlib import Path
import sys
import tempfile
import unittest

import run


class ResultClassification(unittest.TestCase):
    def classify(self, source, check='exit', expected='', timeout=5):
        with tempfile.TemporaryDirectory() as directory:
            case = run.Case('fixture', [sys.executable, '-c', source], directory, check, expected)
            return run.run_case(case, Path(directory), timeout, False)

    def test_nonzero_exit_fails(self):
        self.assertEqual(self.classify('raise SystemExit(2)')['outcome'], 'fail')

    def test_wrong_answer_fails(self):
        self.assertEqual(self.classify("print('% SZS status Satisfiable for input')", 'szs', 'Unsatisfiable')['outcome'], 'fail')

    def test_matching_status_with_error_exit_fails(self):
        self.assertEqual(self.classify("print('% SZS status Unsatisfiable for input'); raise SystemExit(4)", 'szs', 'Unsatisfiable')['outcome'], 'fail')

    def test_expected_gaveup_is_valid(self):
        self.assertEqual(self.classify("print('% SZS status GaveUp for input'); raise SystemExit(1)", 'szs', 'GaveUp')['outcome'], 'pass')

    def test_non_szs_timeout_is_inconclusive(self):
        self.assertEqual(self.classify("print('% Termination reason: Time limit'); raise SystemExit(1)", 'szs', 'Unsatisfiable')['outcome'], 'inconclusive')

    def test_wall_timeout_is_inconclusive(self):
        self.assertEqual(self.classify('import time; time.sleep(5)', timeout=0.1)['outcome'], 'inconclusive')

    def test_signal_fails(self):
        self.assertEqual(self.classify('import os, signal; os.kill(os.getpid(), signal.SIGTERM)')['outcome'], 'fail')

    def test_assertion_cannot_pass_by_printing_expected_output(self):
        result = self.classify("print('% SZS status Unsatisfiable for input'); print('Assertion violation')", 'szs', 'Unsatisfiable')
        self.assertEqual(result['outcome'], 'fail')

    def test_rejection_requires_diagnostic(self):
        self.assertEqual(self.classify('raise SystemExit(1)', 'contains', 'bad input')['outcome'], 'fail')

    def test_boolean_oracle_truth_tables(self):
        for p, q in itertools.product((False, True), repeat=2):
            env = {'p': p, 'q': q}
            self.assertEqual(run.evaluate(('let', 'p', 'q'), env), p == q)
            self.assertEqual(run.evaluate(('ite', 'p', 'q', ('not', 'q')), env), q if p else not q)


if __name__ == '__main__': unittest.main()
