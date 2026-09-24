#!/usr/bin/env python3
"""Readable live and saved reports for a Vampire test campaign."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shlex
import sys
import time
import xml.etree.ElementTree as ET


def section(case):
    name = case['name']
    if name.startswith('unit/'): return 'Unit tests'
    if name.startswith('generated/cnf-'): return 'Generated CNF'
    if name.startswith('generated/bool-'): return 'Boolean expressions'
    if name.startswith('generated/int-'): return 'Integer boundaries'
    if name.startswith('features/finite-'): return 'Finite models'
    if name.startswith('features/portfolio-'): return 'Portfolio workers'
    if name.startswith('features/proof-'): return 'Proof output'
    if name == 'sanity': return 'Release sanity'
    source = case.get('source', '')
    for prefix, label in [('parse/', 'Parser'), ('Problems/', 'TPTP regressions'),
                          ('theory/', 'Theories and FOOL'), ('hol/', 'Higher-order logic'),
                          ('induction/', 'Induction'), ('term-algebra/', 'Datatypes'),
                          ('synthesis/', 'Synthesis'), ('ucore/', 'Unsat cores'), ('fmb/', 'Finite models')]:
        if source.startswith(prefix): return label
    return 'Other corpus checks'


def location(result):
    errors = result.get('valgrind_errors', [])
    # Allocation/read stack locations help navigation; they do not prove a root cause.
    for error in errors[:1]:
        if not error.get('file'): continue
        try:
            tree = ET.parse(error['file'])
            for item in tree.findall('error'):
                if item.findtext('kind') != error['kind']: continue
                for frame in item.findall('stack/frame'):
                    filename = frame.findtext('file', '')
                    if filename.endswith(('.cpp', '.hpp')):
                        directory = frame.findtext('dir', '')
                        return f'{Path(directory) / filename}:{frame.findtext("line", "?")}'
        except (OSError, ET.ParseError):
            pass
    if result.get('source'): return result['source']
    if result['name'].startswith('unit/'):
        unit = result['name'].removeprefix('unit/')
        matches = list((Path(__file__).resolve().parents[2] / 'UnitTests').rglob('t' + unit + '.cpp'))
        if matches: return str(matches[0])
    return result['name']


def failure_detail(result, commands=False):
    tag = 'FAIL' if result['outcome'] == 'fail' else 'INCONCLUSIVE'
    lines = [f'[{tag}] {result["name"]} ({result.get("seconds", 0):.2f}s)',
             f'  Reason: {result.get("reason", "unknown")}',
             f'  Where:  {location(result)}']
    if result.get('check') == 'szs':
        lines.append(f'  Answer: expected {result["expected"]}; observed {", ".join(result.get("statuses", [])) or "no SZS status"}')
    errors = Counter(e['kind'] for e in result.get('valgrind_errors', []))
    if errors:
        lines.append('  Memory: ' + ', '.join(f'{kind}={count}' for kind, count in sorted(errors.items())))
    artifacts = result.get('artifacts')
    if artifacts:
        lines.append(f'  Logs:   {artifacts}/')
        command_file = Path(artifacts) / 'command.json'
        if commands and command_file.exists():
            command = json.loads(command_file.read_text())
            lines.append(f'  Cwd:    {command["cwd"]}')
            lines.append(f'  Run:    {shlex.join(command["argv"])}')
    return '\n'.join(lines)


def table(cases, results):
    planned = Counter(section(case) for case in cases)
    counts = defaultdict(Counter)
    for result in results: counts[section(result)][result['outcome']] += 1
    labels = list(dict.fromkeys([*planned, *counts]))
    lines = [f'{"Section":<25} {"Done/total":>12} {"Pass":>6} {"Fail":>6} {"Inconcl.":>9}']
    for label in labels:
        row = counts[label]
        done = sum(row.values())
        total = planned.get(label, '?')
        lines.append(f'{label:<25} {str(done) + "/" + str(total):>12} {row["pass"]:>6} {row["fail"]:>6} {row["inconclusive"]:>9}')
    total = Counter(result['outcome'] for result in results)
    lines.append(f'{"TOTAL":<25} {str(len(results)) + "/" + str(len(cases) or "?"):>12} {total["pass"]:>6} {total["fail"]:>6} {total["inconclusive"]:>9}')
    return '\n'.join(lines)


class TerminalReporter:
    def __init__(self, cases, previous, output, metadata=None):
        self.cases = cases
        self.results = list(previous)
        self.output = output
        self.started = set()
        self.last_progress = time.monotonic()
        info = (metadata or {}).get('arguments', {})
        self.verbose = info.get('verbose', False)
        mode = 'Valgrind Memcheck' if info.get('memcheck') else 'Correctness'
        print(f'Vampire test campaign | {mode}\nBuild: {info.get("build", "unknown")}\nWorkers: {info.get("jobs", "?")} | Wall timeout: {info.get("timeout", "?")}s\nResults: {output}\n', flush=True)
        print(table(cases, previous), flush=True)
        self.transcript = (output / 'terminal.log').open('a')
        self.transcript.write(table(cases, previous) + '\n')

    def record(self, result):
        self.results.append(result)
        label = section(result)
        lines = []
        if label not in self.started:
            self.started.add(label)
            lines.append(f'\n=== {label} ===')
        if result['outcome'] != 'pass':
            lines.append(f'[{label}] ' + failure_detail(result))
        elif self.verbose or len(self.results) % 100 == 0 or time.monotonic() - self.last_progress >= 10:
            lines.append(f'[PASS] [{label}] {result["name"]} ({len(self.results)}/{len(self.cases)} completed)')
            self.last_progress = time.monotonic()
        if lines:
            text = '\n'.join(lines)
            print(text, flush=True)
            self.transcript.write(text + '\n')
            self.transcript.flush()

    def finish(self):
        failures = [r for r in self.results if r['outcome'] != 'pass']
        groups = defaultdict(list)
        for result in failures: groups[section(result)].append(result)
        with (self.output / 'failures.txt').open('w') as out:
            for label, rows in groups.items():
                out.write(f'=== {label}: {len(rows)} non-passing tests ===\n\n')
                for result in sorted(rows, key=lambda r: r['name']):
                    out.write(failure_detail(result, commands=True) + '\n\n')
        text = '\n=== Final section summary ===\n' + table(self.cases, self.results)
        text += f'\nFailure details and commands: {self.output / "failures.txt"}'
        text += f'\nMachine-readable results: {self.output / "summary.json"}'
        print(text, flush=True)
        self.transcript.write(text + '\n')
        self.transcript.close()


def read_results(output):
    results = []
    journal = output / 'results.jsonl'
    if journal.exists():
        for line in journal.read_text().splitlines():
            try: results.append(json.loads(line))
            except json.JSONDecodeError: pass  # the running process may be mid-write
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--failures', action='store_true', help='show every non-passing test with log paths')
    parser.add_argument('--commands', action='store_true', help='include saved reproduction commands')
    parser.add_argument('--tail', type=int, help='show only the most recent N non-passing tests (watch default: 5)')
    args = parser.parse_args()
    if args.interval <= 0 or (args.tail is not None and args.tail < 0): parser.error('interval must be positive and tail nonnegative')
    manifest = args.output / 'cases.json'
    cases = json.loads(manifest.read_text()) if manifest.exists() else []
    while True:
        if manifest.exists(): cases = json.loads(manifest.read_text())
        results = read_results(args.output)
        if args.watch and sys.stdout.isatty(): print('\033[2J\033[H', end='')
        print(f'Vampire campaign: {args.output}\n{table(cases, results)}', flush=True)
        failures = [r for r in results if r['outcome'] != 'pass']
        if args.failures or args.watch:
            limit = args.tail if args.tail is not None else (5 if args.watch else None)
            shown = failures if limit is None else failures[-limit:] if limit else []
            for result in shown: print('\n' + failure_detail(result, commands=args.commands), flush=True)
        if not args.watch or (args.output / 'summary.json').exists(): break
        time.sleep(args.interval)


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: pass
