#!/usr/bin/env python3
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from report import TerminalReporter, failure_detail, section, table


class TerminalOutput(unittest.TestCase):
    def test_parser_is_a_separate_section(self):
        self.assertEqual(section({'name': 'corpus/001-test', 'source': 'parse/test.p'}), 'Parser')

    def test_inconclusive_does_not_count_as_pass(self):
        cases = [{'name': f'generated/cnf-{i}'} for i in range(3)]
        results = [{**cases[0], 'outcome': 'pass'}, {**cases[1], 'outcome': 'inconclusive'}]
        output = table(cases, results)
        self.assertRegex(output, r'Generated CNF\s+2/3\s+1\s+0\s+1')
        self.assertRegex(output, r'TOTAL\s+2/3\s+1\s+0\s+1')

    def test_failure_has_test_input_answer_and_logs(self):
        result = {'name': 'generated/cnf-7', 'source': 'inputs/cnf-7.p', 'outcome': 'fail',
                  'reason': 'wrong answer', 'check': 'szs', 'expected': 'Unsatisfiable',
                  'statuses': ['Satisfiable'], 'artifacts': '/tmp/case-7'}
        output = failure_detail(result)
        for value in ('generated/cnf-7', 'inputs/cnf-7.p', 'expected Unsatisfiable',
                      'observed Satisfiable', '/tmp/case-7/'):
            self.assertIn(value, output)

    def test_memory_report_points_to_stack_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            xml = Path(temporary) / 'valgrind.xml'
            xml.write_text('<valgrindoutput><error><kind>InvalidRead</kind><stack><frame>'
                           '<dir>/src/Kernel</dir><file>Term.cpp</file><line>123</line>'
                           '</frame></stack></error></valgrindoutput>')
            result = {'name': 'unit/Term', 'outcome': 'fail', 'reason': 'memory error',
                      'valgrind_errors': [{'kind': 'InvalidRead', 'file': str(xml)}]}
            output = failure_detail(result)
            self.assertIn('/src/Kernel/Term.cpp:123', output)
            self.assertIn('InvalidRead=1', output)

    def test_resumed_results_are_not_double_counted(self):
        cases = [{'name': 'unit/A'}, {'name': 'unit/B'}]
        previous = [{**cases[0], 'outcome': 'pass'}]
        failed = {**cases[1], 'outcome': 'fail', 'reason': 'exit 2'}
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()) as stream:
            reporter = TerminalReporter(cases, previous, Path(temporary))
            reporter.record(failed)
            reporter.finish()
            self.assertRegex(stream.getvalue(), r'TOTAL\s+2/2\s+1\s+1\s+0')
            self.assertIn('unit/B', (Path(temporary) / 'failures.txt').read_text())


if __name__ == '__main__': unittest.main()
