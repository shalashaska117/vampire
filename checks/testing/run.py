#!/usr/bin/env python3
"""Run Vampire's tests and retain commands, outputs, and machine-readable results."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import random
import re
import shlex
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from report import TerminalReporter

ROOT = Path(__file__).resolve().parents[2]
SZS = re.compile(r'^% SZS status (\w+)', re.MULTILINE)


@dataclass
class Case:
    name: str
    command: list
    cwd: str
    check: str = 'exit'
    expected: str = ''
    source: str = ''
    allow_error_exit: bool = False


def checked_output(command, cwd=ROOT):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def unit_cases(build):
    data = json.loads(checked_output(['ctest', '--test-dir', str(build), '--show-only=json-v1']))
    return [Case('unit/' + t['name'], t['command'], str(ROOT)) for t in data['tests']]


def corpus_cases(binary):
    """Import literal assertions; retain unsupported shell constructs as explicit gaps."""
    cases, skipped = [], []
    script = (ROOT / 'checks/sanity').read_text().replace('\\\n', ' ')
    kinds = {'check_szs_status': 'szs', 'check_rejected': 'contains',
             'check_output_contains': 'contains', 'check_output_contains_once': 'once',
             'check_exact_output': 'exact'}
    for number, line in enumerate(script.splitlines(), 1):
        line = line.strip()
        if not any(line.startswith(k + ' ') for k in kinds):
            continue
        tokens = shlex.split(line, comments=True)
        if len(tokens) < 3 or any('$' in t and not t.startswith('$distinct') for t in tokens[2:]):
            skipped.append({'line': number, 'command': line, 'reason': 'requires shell expansion'})
            continue
        kind, expected, *args = tokens
        sources = [t for t in args if (ROOT / 'checks' / t).is_file()]
        # Tight sanity timeouts test release performance. This suite tests correctness.
        args += ['-t', '10']
        if kinds[kind] == 'exact':
            expected = (ROOT / 'checks' / expected).read_text()
        cases.append(Case(f'corpus/{number:03d}-{Path(sources[-1]).stem if sources else kind}',
                          [str(binary), *args], str(ROOT / 'checks'), kinds[kind], expected,
                          sources[-1] if sources else '', kind == 'check_rejected'))
    return cases, skipped


def boolean_formula(rng, depth):
    if not depth or rng.randrange(5) == 0:
        return rng.choice(['p', 'q', 'r', True, False])
    op = rng.choice(['not', 'and', 'or', 'xor', '=>', '=', 'ite', 'let'])
    arity = 1 if op == 'not' else 3 if op == 'ite' else 2
    return (op, *(boolean_formula(rng, depth - 1) for _ in range(arity)))


def evaluate(expr, env):
    if isinstance(expr, bool):
        return expr
    if isinstance(expr, str):
        return env[expr]
    op, *children = expr
    values = [evaluate(c, env) for c in children]
    if op == 'not': return not values[0]
    if op == 'and': return all(values)
    if op == 'or': return any(values)
    if op == 'xor': return values[0] != values[1]
    if op == '=>': return not values[0] or values[1]
    if op == '=': return values[0] == values[1]
    if op == 'ite': return values[1] if values[0] else values[2]
    # Rendered let uses its bound expression twice, with opposite polarity.
    if op == 'let': return (not values[0] or values[1]) and (values[0] or not values[1])
    raise ValueError(op)


def smt(expr):
    if isinstance(expr, bool): return str(expr).lower()
    if isinstance(expr, str): return expr
    op, *children = expr
    args = [smt(c) for c in children]
    if op == 'let':
        return f'(let ((b {args[0]})) (and (=> b {args[1]}) (or b (not {args[1]}))))'
    return f'({op} {" ".join(args)})'


def write_input(path, text):
    if path.exists():
        if path.read_text() != text:
            raise ValueError(f'input changed since the earlier run: {path}')
    else:
        path.write_text(text)


def generated_cases(binary, folder, seed):
    folder.mkdir(parents=True, exist_ok=True)
    cases = []
    # Every subset of the nine non-tautological clauses over two variables.
    clauses = list(itertools.product((0, 1, -1), repeat=2))
    for mask in range(1 << len(clauses)):
        selected = [c for n, c in enumerate(clauses) if mask & (1 << n)]
        sat = any(all(any(lit and (lit > 0) == value for lit, value in zip(c, values))
                      for c in selected) for values in itertools.product((False, True), repeat=2))
        lines = []
        for n, clause in enumerate(selected):
            literals = [('' if lit > 0 else '~') + var for var, lit in zip(('p', 'q'), clause) if lit]
            lines.append(f'cnf(c{n},axiom,({" | ".join(literals) if literals else "$false"})).')
        path = folder / f'cnf-{mask:03d}.p'
        write_input(path, '\n'.join(lines or ['cnf(empty,axiom,$true).']) + '\n')
        for strategy in ('lrs', 'discount', 'otter'):
            cases.append(Case(f'generated/cnf-{mask:03d}-{strategy}',
                [str(binary), '-sa', strategy, '-t', '5', '-p', 'off', str(path)], str(ROOT),
                'szs', 'Satisfiable' if sat else 'Unsatisfiable', str(path)))
    rng = random.Random(seed)
    for index in range(96):
        expr = boolean_formula(rng, 4)
        sat = any(evaluate(expr, dict(zip(('p', 'q', 'r'), values)))
                  for values in itertools.product((False, True), repeat=3))
        path = folder / f'bool-{index:03d}.smt2'
        write_input(path, '(set-logic QF_UF)\n' + ''.join(f'(declare-const {v} Bool)\n' for v in ('p', 'q', 'r'))
                        + f'(assert {smt(expr)})\n(check-sat)\n')
        for newcnf, inline in (('off', 'off'), ('on', 'off'), ('on', 'on')):
            cases.append(Case(f'generated/bool-{index:03d}-cnf-{newcnf}-inline-{inline}',
                [str(binary), '-newcnf', newcnf, '-ile', inline, '-t', '5', '-p', 'off', str(path)],
                str(ROOT), 'szs', 'Satisfiable' if sat else 'Unsatisfiable', str(path)))
    for number in (0, 1, -1, 2**31-1, 2**31, -(2**31), 2**63-1, 2**63, -(2**63), 10**100):
        for satisfiable in (True, False):
            path = folder / f'int-{number}-{satisfiable}.smt2'
            numeral = str(number) if number >= 0 else f'(- {-number})'
            relation = '=' if satisfiable else 'distinct'
            write_input(path, f'(set-logic QF_LIA)\n(assert ({relation} (+ {numeral} 1) {number + 1 if number + 1 >= 0 else "(- " + str(-(number + 1)) + ")"}))\n(check-sat)\n')
            cases.append(Case(f'generated/int-{number}-{satisfiable}',
                [str(binary), '-t', '5', '-p', 'off', str(path)], str(ROOT), 'szs',
                'Satisfiable' if satisfiable else 'Unsatisfiable', str(path)))
    return cases


def feature_cases(binary, folder):
    folder.mkdir(parents=True, exist_ok=True)
    cases = []
    # Explicit domain closure makes these quantifier/cardinality answers exact.
    for size in (1, 2, 3):
        constants = [f'a{i}' for i in range(size)]
        domain = ' | '.join(f'X = {c}' for c in constants)
        distinct = ' & '.join(f'{a} != {b}' for a, b in itertools.combinations(constants, 2)) or '$true'
        for different in (False, True):
            relation = '!=' if different else '='
            problem = (f'fof(domain,axiom, ![X]:({domain})).\n'
                       f'fof(distinct,axiom, ({distinct})).\n'
                       f'fof(witness,axiom, ![X]: ?[Y]: (X {relation} Y)).\n')
            path = folder / f'finite-{size}-{different}.p'
            write_input(path, problem)
            expected = 'Unsatisfiable' if size == 1 and different else 'Satisfiable'
            for newcnf in ('off', 'on'):
                cases.append(Case(f'features/finite-{size}-{different}-cnf-{newcnf}',
                    [str(binary), '-sa', 'fmb', '-newcnf', newcnf, '-t', '10', str(path)],
                    str(ROOT), 'szs', expected, str(path)))
    problem = ROOT / 'checks/Problems/PUZ/PUZ001+1.p'
    for cores in (1, 2):
        cases.append(Case(f'features/portfolio-{cores}',
            [str(binary), '--mode', 'casc', '--cores', str(cores), '-t', '10', str(problem)],
            str(ROOT / 'checks'), 'szs', 'Theorem', str(problem)))
    for proof in ('tptp', 'on'):
        cases.append(Case(f'features/proof-{proof}',
            [str(binary), '--proof', proof, '-t', '10', str(problem)],
            str(ROOT / 'checks'), 'szs', 'Theorem', str(problem)))
    return cases


def run_case(case, output, timeout, memcheck):
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', case.name)
    folder = output / name
    if folder.exists():
        folder.rename(folder.with_name(folder.name + f'.interrupted-{time.time_ns()}'))
    folder.mkdir()
    command = list(case.command)
    if memcheck:
        command = ['valgrind', '--tool=memcheck', '--leak-check=full', '--show-leak-kinds=all',
                   '--errors-for-leak-kinds=definite,indirect,possible', '--track-origins=yes',
                   '--error-exitcode=97', '--trace-children=yes', '--xml=yes',
                   f'--xml-file={folder}/valgrind.%p.xml', *command]
    (folder / 'command.json').write_text(json.dumps({'argv': command, 'cwd': case.cwd}, indent=2))
    start = time.monotonic()
    timed_out = False
    with (folder / 'stdout.log').open('w') as stdout, (folder / 'stderr.log').open('w') as stderr:
        process = subprocess.Popen(command, cwd=case.cwd, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
    stdout = (folder / 'stdout.log').read_text(errors='replace')
    stderr = (folder / 'stderr.log').read_text(errors='replace')
    statuses = SZS.findall(stdout)
    resource_limited = bool(re.search(r'Termination reason: (Time limit|Memory limit|Instruction limit|Activation limit)', stdout))
    outcome, reason = 'pass', ''
    if timed_out:
        outcome, reason = 'inconclusive', 'wall timeout'
    elif code < 0:
        outcome, reason = 'fail', f'signal {-code}'
    elif code == 97:
        outcome, reason = 'fail', 'Valgrind error exit'
    elif 'Assertion violation' in stdout + stderr or 'runtime error:' in stderr:
        outcome, reason = 'fail', 'assertion or sanitizer error'
    elif case.check == 'exit' and code != 0:
        outcome, reason = ('inconclusive' if resource_limited else 'fail'), f'exit {code}'
    elif case.check == 'szs' and case.expected not in statuses:
        outcome = 'inconclusive' if resource_limited or any(s in ('Timeout', 'ResourceOut', 'GaveUp', 'MemoryOut') for s in statuses) else 'fail'
        reason = f'expected {case.expected}; got {statuses} (exit {code})'
    elif case.check == 'contains' and case.expected not in stdout:
        outcome, reason = 'fail', f'missing expected diagnostic: {case.expected}'
    elif case.check == 'once' and stdout.count(case.expected) != 1:
        outcome, reason = 'fail', 'expected substring exactly once'
    elif case.check == 'exact' and stdout != case.expected:
        outcome, reason = 'fail', 'output differs'
    # Rejection tests can return nonzero; successful proof/output tests cannot.
    elif code != 0 and not case.allow_error_exit and not (case.check == 'szs' and case.expected in ('GaveUp', 'Timeout', 'ResourceOut', 'MemoryOut') and code == 1):
        outcome, reason = ('inconclusive' if resource_limited else 'fail'), f'exit {code}'
    errors = []
    if memcheck:
        xmls = list(folder.glob('valgrind.*.xml'))
        for path in xmls:
            try:
                tree = ET.parse(path)
                for error in tree.findall('error'):
                    kind = error.findtext('kind')
                    if kind != 'Leak_StillReachable':
                        errors.append({'kind': kind, 'message': error.findtext('what') or error.findtext('xwhat/text'), 'file': str(path)})
            except ET.ParseError:
                if not timed_out:
                    errors.append({'kind': 'invalid-xml', 'file': str(path)})
        if not xmls:
            errors.append({'kind': 'missing-xml'})
        if errors and not timed_out:
            outcome, reason = 'fail', 'Valgrind reported errors'
    result = {**asdict(case), 'outcome': outcome, 'reason': reason, 'exit': code,
              'seconds': round(time.monotonic() - start, 3), 'statuses': statuses,
              'valgrind_errors': errors, 'artifacts': str(folder)}
    (folder / 'result.json').write_text(json.dumps(result, indent=2))
    return result


def inventory(build, output):
    corpus, skipped = corpus_cases(build / 'vampire')
    inputs = sorted(p for p in (ROOT / 'checks').rglob('*') if p.suffix in ('.p', '.smt2', '.ax', '.out'))
    used = {str(ROOT / 'checks' / c.source) for c in corpus if c.source}
    data = {'commit': checked_output(['git', 'rev-parse', 'HEAD']),
            'units': [c.name for c in unit_cases(build)],
            'literal_sanity_assertions': len(corpus), 'dynamic_sanity_assertions': skipped,
            'inputs': [{'path': str(p.relative_to(ROOT)), 'bytes': p.stat().st_size,
                        'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                        'direct_assertion': str(p) in used} for p in inputs]}
    output.write_text(json.dumps(data, indent=2))
    print(f'{len(data["units"])} unit suites, {len(corpus)} literal sanity assertions, {len(inputs)} corpus files')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inventory', 'run'))
    parser.add_argument('--build', type=Path, default=ROOT / 'build/testing/coverage')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--suite', choices=('units', 'corpus', 'generated', 'features', 'sanity', 'all'), default='all')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--solver-timeout', type=int, help='override solver seconds (0 disables its timer); Memcheck defaults to 0')
    parser.add_argument('--seed', type=int, default=764971)
    parser.add_argument('--filter', default='')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--memcheck', action='store_true')
    parser.add_argument('--verbose', action='store_true', help='print every passing test as well as failures')
    parser.add_argument('--resume', action='store_true', help='resume unfinished cases in a matching run directory')
    args = parser.parse_args()
    args.build = args.build.resolve()
    args.output = args.output.resolve()
    if args.action == 'inventory':
        args.output.parent.mkdir(parents=True, exist_ok=True)
        inventory(args.build, args.output)
        return 0
    if args.output.exists() and not args.resume:
        parser.error('output already exists; choose a new directory or use --resume')
    if args.resume and not (args.output / 'run.json').exists():
        parser.error('--resume requires a run.json in the output directory')
    if args.jobs < 1 or args.timeout <= 0 or (args.limit is not None and args.limit < 1):
        parser.error('jobs, timeout, and limit must be positive')
    args.output.mkdir(parents=True, exist_ok=True)
    binary = args.build / 'vampire'
    if args.resume:
        previous = json.loads((args.output / 'run.json').read_text())
        current_arguments = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
        for key in ('build', 'suite', 'seed', 'filter', 'limit', 'memcheck', 'solver_timeout'):
            if previous['arguments'].get(key) != current_arguments.get(key):
                parser.error(f'cannot resume with a different {key}')
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (binary, args.build / 'vtest') if p.is_file()}
        if previous['binary_sha256'] != hashes:
            parser.error('cannot resume after changing a binary')
    cases, skipped = [], []
    if args.suite in ('all', 'units'): cases += unit_cases(args.build)
    if args.suite in ('all', 'corpus'):
        corpus, skipped = corpus_cases(binary)
        cases += corpus
    if args.suite in ('all', 'generated'): cases += generated_cases(binary, args.output / 'inputs', args.seed)
    if args.suite in ('all', 'features'): cases += feature_cases(binary, args.output / 'feature-inputs')
    if args.suite == 'sanity':
        if args.memcheck:
            parser.error('use --suite corpus with --memcheck; sanity includes tight release timing checks')
        cases = [Case('sanity', ['sh', 'checks/sanity', os.path.relpath(binary, ROOT)], str(ROOT))]
    if args.filter: cases = [c for c in cases if re.search(args.filter, c.name)]
    if args.limit is not None: cases = cases[:args.limit]
    if not cases: parser.error('no tests selected')
    solver_timeout = args.solver_timeout if args.solver_timeout is not None else (0 if args.memcheck else None)
    if solver_timeout is not None:
        for case in cases:
            if case.name.startswith(('corpus/', 'generated/', 'features/')):
                case.command += ['-t', str(solver_timeout)]
    metadata = {'commit': checked_output(['git', 'rev-parse', 'HEAD']),
                'dirty': checked_output(['git', 'status', '--porcelain']),
                'started': datetime.now(timezone.utc).isoformat(), 'seed': args.seed,
                'arguments': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                'selected': len(cases), 'skipped_shell_assertions': skipped}
    metadata['binary_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (binary, args.build / 'vtest') if p.is_file()}
    all_cases = [asdict(case) for case in cases]
    (args.output / 'cases.json').write_text(json.dumps(all_cases, indent=2))
    results = []
    if args.resume:
        previous = json.loads((args.output / 'run.json').read_text())
        for key in ('build', 'suite', 'seed', 'filter', 'limit', 'memcheck', 'solver_timeout'):
            if previous['arguments'].get(key) != metadata['arguments'].get(key):
                parser.error(f'cannot resume with a different {key}')
        if previous['binary_sha256'] != metadata['binary_sha256']:
            parser.error('cannot resume after changing a binary')
        by_name = {case.name: case for case in cases}
        for path in args.output.glob('*/result.json'):
            if '.interrupted-' in path.parent.name:
                continue
            try:
                result = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if result['name'] in by_name:
                if result['command'] != by_name[result['name']].command:
                    parser.error(f'command changed for {result["name"]}')
                results.append(result)
        done = {result['name'] for result in results}
        cases = [case for case in cases if case.name not in done]
        metadata['previously_completed'] = len(results)
        print(f'Resuming {len(cases)} cases; preserving {len(results)} completed results', flush=True)
        if (args.output / 'summary.json').exists():
            (args.output / 'summary.json').rename(args.output / f'summary-before-resume-{time.time_ns()}.json')
        (args.output / f'resume-{time.time_ns()}.json').write_text(json.dumps(metadata, indent=2))
    (args.output / 'source.diff').write_text(checked_output(['git', 'diff', 'HEAD']))
    if not args.resume:
        (args.output / 'run.json').write_text(json.dumps(metadata, indent=2))
    total = len(cases) + len(results)
    reporter = TerminalReporter(all_cases, results, args.output, metadata)
    with (args.output / 'results.jsonl').open('w') as journal, ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for result in results:
            journal.write(json.dumps(result) + '\n')
        journal.flush()
        futures = {pool.submit(run_case, c, args.output, args.timeout, args.memcheck): c for c in cases}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                case = futures[future]
                folder = args.output / re.sub(r'[^A-Za-z0-9_.-]', '_', case.name)
                result = {**asdict(case), 'outcome': 'fail', 'reason': f'harness error: {error}', 'artifacts': str(folder)}
                folder.mkdir(exist_ok=True)
                (folder / 'result.json').write_text(json.dumps(result, indent=2))
            results.append(result)
            journal.write(json.dumps(result) + '\n')
            journal.flush()
            reporter.record(result)
    totals = dict(Counter(r['outcome'] for r in results))
    summary = {**metadata, 'totals': totals, 'results': sorted(results, key=lambda r: r['name'])}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2))
    reporter.finish()
    return int(any(r['outcome'] != 'pass' for r in results))


if __name__ == '__main__':
    sys.exit(main())
