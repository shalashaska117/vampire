#!/usr/bin/env python3
"""Build and run the master test matrix, retaining failures and coverage gaps."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / 'checks/testing'


def coverage_gaps(trace):
    files, current = [], None
    for line in trace.read_text().splitlines():
        if line.startswith('SF:'):
            current = {'path': line[3:], 'lines': [], 'functions': [], 'branches': [], 'totals': {'FAF': 0, 'FAH': 0}}
        elif current is not None:
            key, _, value = line.partition(':')
            if key in ('LF', 'LH', 'FNF', 'FNH', 'BRF', 'BRH'):
                current['totals'][key] = int(value)
            elif key == 'DA' and int(value.split(',')[1]) == 0:
                current['lines'].append(int(value.split(',')[0]))
            elif key in ('FNDA', 'FNA'):
                # lcov 2.x groups template instances at one source location in
                # FNF/FNH. Count every named alias as lcov's summary does.
                count, name = value.split(',', 2)[1:] if key == 'FNA' else value.split(',', 1)
                current['totals']['FAF'] += 1
                current['totals']['FAH'] += int(int(count) > 0)
                if int(count) == 0: current['functions'].append(name)
            elif key == 'BRDA' and value.rsplit(',', 1)[1] in ('-', '0'):
                location, block, branch, _ = value.split(',')
                current['branches'].append({'line': int(location), 'block': block, 'branch': branch})
            elif line == 'end_of_record':
                files.append(current)
                current = None
    totals = {key: sum(f['totals'].get(key, 0) for f in files)
              for key in ('LF', 'LH', 'FNF', 'FNH', 'FAF', 'FAH', 'BRF', 'BRH')}
    percentages = {label: 100 * totals[hit] / totals[found] if totals[found] else 0
                   for label, hit, found in [('lines', 'LH', 'LF'), ('functions', 'FAH', 'FAF'), ('function_groups', 'FNH', 'FNF'),
                                             ('branches', 'BRH', 'BRF')]}
    return {'target_percent': 100, 'percentages': percentages, 'totals': totals,
            'files': sorted(files, key=lambda f: len(f['branches']), reverse=True)}


def option_audit(run_folder):
    data = json.loads((run_folder / 'option-inputs/catalogue.json').read_text())
    results = json.loads((run_folder / 'summary.json').read_text())['results']
    for entry in data['compiled_options']:
        flags = {'--' + entry['name']}
        if entry['short']: flags.add('-' + entry['short'])
        parsing = [r for r in results if r['name'].startswith('options/') and flags.intersection(r['command'])]
        behavior = [r for r in results if not r['name'].startswith('options/') and flags.intersection(r['command'])]
        entry['parser_results'] = {state: sum(r['outcome'] == state for r in parsing)
                                   for state in ('pass', 'fail', 'inconclusive')}
        entry['explicit_solver_cases'] = [r['name'] for r in behavior]
        entry['behavior_status'] = ('explicit solver commands exist; execution coverage still requires inspection'
                                    if behavior else 'no explicit solver case')
    data['counts'] = {'compiled_options': len(data['compiled_options']),
                      'options_with_explicit_solver_cases': sum(bool(e['explicit_solver_cases']) for e in data['compiled_options'])}
    return data


def main():
    settings_path = ROOT / 'build/testing/local-tools.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--memcheck-jobs', type=int, default=8)
    parser.add_argument('--lcov-tool-dir', type=Path, default=settings.get('lcov_tool_dir'))
    parser.add_argument('--z3', default=settings.get('z3', 'z3'))
    parser.add_argument('--profiles', nargs='+', default=['release', 'memcheck', 'ubsan', 'asan', 'no-z3', 'coverage'],
                        choices=['release', 'memcheck', 'ubsan', 'asan', 'no-z3', 'coverage'])
    args = parser.parse_args()
    if args.jobs < 1 or args.memcheck_jobs < 1: parser.error('worker counts must be positive')
    out = args.output.resolve()
    if out.exists(): parser.error('choose a new output directory')
    # Serialize builds and coverage resets for this checkout.
    lock_path = ROOT / 'build/testing/campaign.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open('w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: parser.error('a campaign is already running in this checkout')
    out.mkdir(parents=True)
    env = {**os.environ, 'JOBS': str(args.jobs), 'PYTHONUNBUFFERED': '1',
           'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=1',
           'ASAN_OPTIONS': 'detect_leaks=1:halt_on_error=1:abort_on_error=0:exitcode=98'}
    metadata = {'started': datetime.now(timezone.utc).isoformat(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'profiles': args.profiles, 'target_percent': 100, 'stages': [],
        'harness_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in HERE.glob('*.py')}}
    def save(): (out / 'campaign.json').write_text(json.dumps(metadata, indent=2))
    def stage(name, command, verbose=True):
        record = {'name': name, 'command': [str(a) for a in command], 'log': str(out / (name + '.log')),
                  'started': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
        metadata['stages'].append(record)
        save()
        print(f'\n===== {name} =====\nLog: {record["log"]}', flush=True)
        start, last = time.monotonic(), 0
        try:
            with Path(record['log']).open('w') as log:
                process = subprocess.Popen(record['command'], cwd=ROOT, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    if verbose or time.monotonic() - last > 10:
                        print(line, end='', flush=True)
                        last = time.monotonic()
                code = process.wait()
            record.update(exit=code, status='pass' if code == 0 else 'fail')
        except OSError as error:
            record.update(exit=127, status='fail', error=str(error))
        except KeyboardInterrupt:
            record.update(status='interrupted')
            save()
            raise
        record['seconds'] = round(time.monotonic() - start, 2)
        save()
        print(f'[{record["status"].upper()}] {name}: exit {record["exit"]}; {record["seconds"]}s', flush=True)
        return record['exit'] == 0
    python = sys.executable
    stage('harness-tests', [python, '-m', 'unittest', 'discover', '-s', HERE, '-p', 'test_*.py', '-v'])
    built = set()
    for profile in args.profiles:
        build = ROOT / 'build/testing' / profile
        if not stage('build-' + profile, ['bash', HERE / 'build.sh', profile], verbose=False):
            print(f'[BLOCKED] Tests for {profile}: build failed; see log above.', flush=True)
            continue
        built.add(profile)
        if profile == 'release':
            stage('release-sanity', [python, HERE / 'run.py', 'run', '--suite', 'sanity', '--build', build,
                  '--jobs', '1', '--timeout', '180', '--output', out / 'release-sanity'])
        if profile == 'coverage':
            previous = list(build.rglob('*.gcda'))
            if previous:
                with tarfile.open(out / 'previous-counters.tar.gz', 'w:gz') as archive:
                    for path in previous: archive.add(path, arcname=str(path.relative_to(build)))
            lcov = args.lcov_tool_dir / 'lcov' if args.lcov_tool_dir else 'lcov'
            if not stage('coverage-reset', [lcov, '--zerocounters', '--directory', build]):
                print('[BLOCKED] Coverage run: counter reset failed.', flush=True)
                continue
        run_folder = out / (profile + '-all')
        stage(profile + '-all', [python, HERE / 'run.py', 'run', '--suite', 'all', '--build', build,
              '--jobs', str(args.jobs), '--timeout', '180', '--output', run_folder, '--z3', args.z3,
              *(['--asan'] if profile == 'asan' else [])])
        if profile == 'release' and (run_folder / 'summary.json').exists():
            (out / 'option-audit.json').write_text(json.dumps(option_audit(run_folder), indent=2))
            stage('independent-smt-oracles', [python, HERE / 'check_oracles.py', run_folder / 'summary.json',
                  '--z3', args.z3, '--output', out / 'oracle-results.json'])
            stage('inventory', [python, HERE / 'run.py', 'inventory', '--build', build, '--output', out / 'inventory.json'])
        if profile == 'coverage':
            command = [python, HERE / 'coverage.py', '--build', build, '--output', out / 'lcov',
                       '--jobs', str(args.jobs), '--allow-overlapping-functions']
            if args.lcov_tool_dir: command += ['--tool-dir', args.lcov_tool_dir]
            if stage('coverage-capture', command):
                gaps = coverage_gaps(out / 'lcov/coverage.info')
                (out / 'coverage-gaps.json').write_text(json.dumps(gaps, indent=2))
                complete = all(gaps['totals'][total] > 0 and gaps['totals'][hit] == gaps['totals'][total]
                               for hit, total in [('LH', 'LF'), ('FNH', 'FNF'), ('FAH', 'FAF'), ('BRH', 'BRF')])
                metadata['stages'].append({'name': 'coverage-target', 'status': 'pass' if complete else 'fail',
                                            'percentages': gaps['percentages'], 'target_percent': 100})
                save()
                print(f'[{"PASS" if complete else "FAIL"}] 100% coverage target: {gaps["percentages"]}', flush=True)
                print(f'Uncovered locations: {out / "coverage-gaps.json"}', flush=True)
    if 'memcheck' in built and (ROOT / 'build/testing/memcheck/vampire').exists():
        run_folder = out / 'valgrind-all'
        stage('valgrind-all', [python, HERE / 'run.py', 'run', '--suite', 'all',
              '--build', ROOT / 'build/testing/memcheck', '--memcheck', '--jobs', str(args.memcheck_jobs),
              '--timeout', '120', '--output', run_folder, '--z3', args.z3])
        if (run_folder / 'summary.json').exists():
            stage('memory-triage', [python, HERE / 'triage.py', run_folder, '--output', out / 'memcheck-groups.json'])
    metadata['finished'] = datetime.now(timezone.utc).isoformat()
    metadata['status'] = 'pass' if all(s['status'] == 'pass' for s in metadata['stages']) else 'fail'
    save()
    print('\n===== Campaign summary =====', flush=True)
    for result in metadata['stages']:
        print(f'{result["status"].upper():12} {result["name"]}  {result.get("log", "")}', flush=True)
    print(f'Results: {out}', flush=True)
    return int(metadata['status'] != 'pass')


if __name__ == '__main__': raise SystemExit(main())
