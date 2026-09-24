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
from diagnostics import ASSERTION, sanitizer_messages, combine

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
    stdin_text: str = None


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
        if path.read_bytes() != text.encode():
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


def run_case(case, output, timeout, memcheck, z3="z3", asan=False):
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
    stdin_path = folder / 'stdin.txt'
    if case.stdin_text is not None: stdin_path.write_text(case.stdin_text)
    with (stdin_path if case.stdin_text is not None else Path(os.devnull)).open('r') as stdin, (folder / 'stdout.log').open('w') as stdout, (folder / 'stderr.log').open('w') as stderr:
        process = subprocess.Popen(command, cwd=case.cwd, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True)
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
    elif ASSERTION.search(stdout + stderr):
        outcome, reason = 'fail', 'assertion or sanitizer error'
    elif case.check == 'exit' and code != 0:
        outcome, reason = ('inconclusive' if resource_limited else 'fail'), f'exit {code}'
    elif case.check in ('szs', 'smt-proof') and case.expected not in statuses:
        outcome = 'inconclusive' if resource_limited or any(s in ('Timeout', 'ResourceOut', 'GaveUp', 'MemoryOut') for s in statuses) else 'fail'
        reason = f'expected {case.expected}; got {statuses} (exit {code})'
    elif case.check == 'unsupported':
        outcome, reason = ('inconclusive', case.expected) if case.expected in stdout + stderr else ('fail', 'unsupported-feature diagnostic changed')
    elif case.check == 'reject-regex' and (code not in (1, 4) or re.search(case.expected, stdout + stderr) is None):
        outcome, reason = 'fail', 'missing expected unavailable-option rejection'
    elif case.check == 'reject' and (code not in (1, 4) or case.expected not in stdout + stderr):
        outcome, reason = 'fail', f'expected a user-error exit and diagnostic: {case.expected}; exit {code}'
    elif case.check == 'option-boundary' and not (
            (code == 0 and 'Usage: vampire' in stdout) or
            (code in (1, 4) and f'is an invalid value for {case.expected}' in stdout + stderr)):
        outcome, reason = 'fail', f'option boundary neither accepted nor explicitly rejected (exit {code})'
    elif case.check in ('szs', 'smt-proof') and case.expected in ('Satisfiable', 'Unsatisfiable', 'Theorem', 'CounterSatisfiable') and any(
            status in ({'Satisfiable', 'CounterSatisfiable'} if case.expected in ('Unsatisfiable', 'Theorem')
                       else {'Unsatisfiable', 'Theorem', 'ContradictoryAxioms'}) for status in statuses):
        outcome, reason = 'fail', f'conflicting SZS answers: {statuses}'
    elif case.check == 'contains' and case.expected not in stdout:
        outcome, reason = 'fail', f'missing expected diagnostic: {case.expected}'
    elif case.check == 'once' and stdout.count(case.expected) != 1:
        outcome, reason = 'fail', 'expected substring exactly once'
    elif case.check == 'exact' and stdout != case.expected:
        outcome, reason = 'fail', 'output differs'
    # Rejection tests can return nonzero; successful proof/output tests cannot.
    elif code != 0 and not case.allow_error_exit and not (case.check in ('szs', 'smt-proof') and case.expected in ('GaveUp', 'Timeout', 'ResourceOut', 'MemoryOut') and code == 1):
        outcome, reason = ('inconclusive' if resource_limited else 'fail'), f'exit {code}'
    sanitizers = sanitizer_messages(stdout, stderr)
    if code in (97, 98) and (memcheck or sanitizers) and reason == f'exit {code}':
        if case.check in ('szs', 'smt-proof') and case.expected in statuses:
            outcome, reason = 'pass', ''
        else:
            outcome, reason = 'inconclusive', 'instrumentation replaced the program exit code'
    if outcome == 'pass' and case.check in ('smt-proof', 'roundtrip'):
        from validation import validate_smt_script, validate_roundtrip
        if case.check == 'smt-proof':
            outcome, reason = validate_smt_script(stdout, folder, z3, min(timeout, 30))
        else:
            outcome, reason = validate_roundtrip(stdout, folder, case.command[0], case.expected, min(timeout, 30))
    semantic_outcome, semantic_reason = outcome, reason
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
                errors.append({'kind': 'invalid-xml', 'file': str(path)})
        if not xmls:
            errors.append({'kind': 'missing-xml'})
    outcome, reason, memory_outcome = combine(semantic_outcome, semantic_reason, sanitizers, errors, timed_out)
    if not memcheck and not asan and not sanitizers: memory_outcome = 'not-instrumented'
    result = {**asdict(case), 'outcome': outcome, 'reason': reason, 'exit': code,
              'seconds': round(time.monotonic() - start, 3), 'statuses': statuses,
              'valgrind_errors': errors, 'artifacts': str(folder),
              'semantic_outcome': semantic_outcome, 'semantic_reason': semantic_reason,
              'memory_outcome': memory_outcome, 'sanitizer_messages': sanitizers, 'wall_timeout': timed_out,
              'option_value_accepted': ('Usage: vampire' in stdout) if case.check == 'option-boundary' else None}
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
    parser.add_argument('--suite', choices=('units', 'corpus', 'generated', 'features', 'edges', 'options', 'behavior', 'sanity', 'all'), default='all')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--solver-timeout', type=int, help='override solver seconds (0 disables its timer); Memcheck defaults to 0')
    parser.add_argument('--seed', type=int, default=764971)
    parser.add_argument('--filter', default='')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--memcheck', action='store_true')
    parser.add_argument('--asan', action='store_true', help='ASan environment and unlimited default solver memory; explicit memory tests keep their limits')
    settings_file = ROOT / 'build/testing/local-tools.json'
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    parser.add_argument('--z3', default=settings.get('z3', 'z3'))
    parser.add_argument('--verbose', action='store_true', help='print every passing test as well as failures')
    parser.add_argument('--resume', action='store_true', help='resume unfinished cases in a matching run directory')
    args = parser.parse_args()
    if args.asan and args.memcheck: parser.error('run ASan and Valgrind separately')
    if args.asan:
        os.environ.setdefault('ASAN_OPTIONS', 'detect_leaks=1:halt_on_error=1:abort_on_error=0:exitcode=98')
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
        for key in ('build', 'suite', 'seed', 'filter', 'limit', 'memcheck', 'solver_timeout', 'z3', 'asan'):
            if previous['arguments'].get(key) != current_arguments.get(key):
                parser.error(f'cannot resume with a different {key}')
        harness_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / 'checks/testing').glob('*.py')}
        environment = {key: os.environ.get(key) for key in ('ASAN_OPTIONS', 'UBSAN_OPTIONS', 'LSAN_OPTIONS')}
        if previous.get('harness_sha256') != harness_hashes or previous.get('environment') != environment:
            parser.error('cannot resume after changing the harness or instrumentation environment')
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (binary, args.build / 'vtest') if p.is_file()}
        if previous['binary_sha256'] != hashes:
            parser.error('cannot resume after changing a binary')
    cases, skipped = [], []
    discovered, discovery_errors = None, []
    if args.suite in ('all', 'corpus', 'options', 'behavior'):
        from option_cases import catalogue
        try:
            discovered = catalogue(binary, args.output / 'discovery')
        except (ValueError, OSError) as error:
            discovery_errors.append(str(error))
            print(f'[FAIL] Option discovery: {error}; continuing independent suites', flush=True)
        cases.append(Case('discovery/options', [str(binary), '--show_options', 'on',
            '--show_experimental_options', 'on', '--show_options_line_wrap', 'off'], str(ROOT), 'contains', '--mode'))
    if args.suite in ('all', 'units'): cases += unit_cases(args.build)
    if args.suite in ('all', 'corpus'):
        corpus, skipped = corpus_cases(binary)
        cases += corpus
    if args.suite in ('all', 'generated'): cases += generated_cases(binary, args.output / 'inputs', args.seed)
    if args.suite in ('all', 'features'): cases += feature_cases(binary, args.output / 'feature-inputs')
    if args.suite in ('all', 'edges'):
        from edge_cases import edge_cases
        cases += edge_cases(binary, args.output / 'edge-inputs', args.seed, Case, ROOT, corpus_cases, write_input)
    if args.suite in ('all', 'options') and discovered is not None:
        from option_cases import option_cases
        cases += option_cases(binary, args.output / 'option-inputs', Case, ROOT, write_input, discovered)
    if args.suite in ('all', 'behavior'):
        from behavior_cases import behavior_cases
        cases += behavior_cases(binary, args.output / 'behavior-inputs', Case, ROOT, write_input, discovered[0] if discovered else [])
    if args.suite == 'sanity':
        if args.memcheck:
            parser.error('use --suite corpus with --memcheck; sanity includes tight release timing checks')
        cases = [Case('sanity', ['sh', 'checks/sanity', os.path.relpath(binary, ROOT)], str(ROOT))]
    capability_rejections = []
    if discovered is not None:
        names = {entry['name'] for entry in discovered[0]}
        sat = next((entry for entry in discovered[0] if entry['short'] == 'sas'), None)
        for case in cases:
            if not case.name.startswith('corpus/') or case.check != 'szs': continue
            command = case.command
            diagnostic = None
            if sat and 'z3' not in sat['values']:
                if '-sas' in command and command[command.index('-sas') + 1] == 'z3':
                    diagnostic = 'z3 is an invalid value for sas'
                elif any('sas=z3' in arg for arg in command):
                    diagnostic = 'value z3 for option sas not known'
            if diagnostic is None and 'theory_instantiation' not in names:
                if '-thi' in command: diagnostic = 'thi is not a valid short option'
                elif any('thi=' in arg for arg in command): diagnostic = 'option thi not known'
            if diagnostic and any('thi=' in arg for arg in command) and 'theory_instantiation' not in names:
                case.check, case.expected, case.allow_error_exit = 'reject-regex', '(?:' + re.escape(diagnostic) + '|' + re.escape('option thi not known') + ')', True
                capability_rejections.append(case.name)
                continue
            if diagnostic:
                case.check, case.expected, case.allow_error_exit = 'reject', diagnostic, True
                capability_rejections.append(case.name)
    if args.filter: cases = [c for c in cases if re.search(args.filter, c.name)]
    if args.limit is not None: cases = cases[:args.limit]
    if not cases: parser.error('no tests selected')
    solver_timeout = args.solver_timeout if args.solver_timeout is not None else (0 if args.memcheck else None)
    if solver_timeout is not None:
        for case in cases:
            if case.name.startswith(('corpus/', 'generated/', 'features/', 'edge/', 'behavior/')):
                case.command += ['-t', str(solver_timeout)]
    if args.asan:
        for case in cases:
            if case.command[0] == str(binary) and not any(flag in case.command for flag in ('-m', '--memory_limit')):
                case.command += ['-m', '0']
    metadata = {'commit': checked_output(['git', 'rev-parse', 'HEAD']),
                'dirty': checked_output(['git', 'status', '--porcelain']),
                'started': datetime.now(timezone.utc).isoformat(), 'seed': args.seed,
                'arguments': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                'selected': len(cases), 'skipped_shell_assertions': skipped,
                'discovery_errors': discovery_errors, 'capability_rejections': capability_rejections,
                'environment': {key: os.environ.get(key) for key in ('ASAN_OPTIONS', 'UBSAN_OPTIONS', 'LSAN_OPTIONS')},
                'harness_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / 'checks/testing').glob('*.py')}}
    snapshot = args.output / ('harness-snapshot-' + str(time.time_ns()))
    snapshot.mkdir()
    for path in (ROOT / 'checks/testing').glob('*.py'):
        (snapshot / path.name).write_bytes(path.read_bytes())
    metadata['harness_snapshot'] = str(snapshot)
    metadata['binary_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (binary, args.build / 'vtest') if p.is_file()}
    all_cases = [asdict(case) for case in cases]
    (args.output / 'cases.json').write_text(json.dumps(all_cases, indent=2))
    results = []
    if args.resume:
        previous = json.loads((args.output / 'run.json').read_text())
        for key in ('build', 'suite', 'seed', 'filter', 'limit', 'memcheck', 'solver_timeout', 'z3', 'asan'):
            if previous['arguments'].get(key) != metadata['arguments'].get(key):
                parser.error(f'cannot resume with a different {key}')
        if previous.get('harness_sha256') != metadata['harness_sha256'] or previous.get('environment') != metadata['environment']:
            parser.error('cannot resume after changing the harness or instrumentation environment')
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
        futures = {pool.submit(run_case, c, args.output, args.timeout, args.memcheck, args.z3, args.asan): c for c in cases}
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
    return int(bool(discovery_errors) or any(r['outcome'] != 'pass' for r in results))


if __name__ == '__main__':
    sys.exit(main())
