# Vampire testing baseline, 24 September 2026

The testing branch combines PR #971 and PR #973. The tested Vampire code is
`891f528a6`; the testing tools were added afterward. This is a baseline for
further testing, not a claim that every input or option combination is covered.

## Correctness

| Section | Passed | Inconclusive | Failed |
| --- | ---: | ---: | ---: |
| Debug unit suites | 105 | 0 | 0 |
| Imported release assertions | 203 | 0 | 0 |
| Generated release cases | 1833 | 11 | 0 |
| Finite-model, portfolio, and proof-output cases | 16 | 0 | 0 |
| Full release sanity suite, idle machine | 1 | 0 | 0 |
| Python harness/reporting tests | 15 | 0 | 0 |

The eleven generated cases hit solver limits: one Boolean formula with the old
clausifier and ten satisfiable ground-arithmetic inputs. Z3 independently agreed
with all 116 generated SMT expectations. No wrong answer was observed in this
campaign. This does not establish general solver correctness.

An earlier sanity run under build/test load timed out on HWV087_2's one-second
limit. The unmodified suite passed after the heavy work finished. The instrumented
run also timed out on three TPTP cases; their release assertions passed. Three
unit suites passed when rerun with the project's 180-second coverage allowance.

## Coverage

| Metric | Hit | Total | Coverage |
| --- | ---: | ---: | ---: |
| Lines | 50543 | 76320 | 66.2% |
| Functions | 7726 | 9397 | 82.2% |
| Branches | 59397 | 166049 | 35.8% |

Toolchain: GCC/gcov 15.2, lcov 2.5, Valgrind 3.26, and the repository's pinned
Z3 4.14 build. Coverage combines the zero-hit capture with executed counters.
The source scope and tool settings are documented in [README.md](README.md).
The C++ lambda-overlap workaround was enabled and recorded in `settings.json`;
raw function and line counters were preserved. Compiler-disabled translation
units without executable counters remain visible as capture warnings.

## Memory checks

All 2,168 cases were attempted with `CHECK_LEAKS=ON`: 2,139 failed the strict
Valgrind checks and 29 were inconclusive. These are case counts, not distinct
bugs. Many cases share the same leak or shutdown stack. Reachable allocations
were retained as information, and no project suppression file was applied.

The inconclusive cases are 13 unit suites, three TPTP assertions, one Boolean
case, ten satisfiable arithmetic cases, and two portfolio cases. They reached
the 120-second wall limit. Some child processes reported memory errors before
the timeout; the saved XML retains those observations. A timeout prevents a
complete final leak check.

One confirmed invalid-read trace is:

```text
Shell::Options::instructionLimit()   Shell/Options.hpp:1397
timer_thread()                     Lib/Timer.cpp:148

freed by Lib::Environment::~Environment() at Lib/Environment.cpp:70
```

The timer thread reads `options` after environment cleanup frees it. This was
observed in the cleanup-enabled debug build. A single release comparison reported
leaks but did not reproduce the invalid read; that one run does not establish
its absence in release builds.

Recurring leak allocations include `Kernel::OperatorType::getTypeFromKey` at
`Kernel/OperatorType.cpp:66`. Allocation sites need ownership/lifetime review
before assigning root causes. The XML, grouped stacks, commands, and per-case
logs are retained under `build/testing/results/`.

Memcheck changes a child's exit code when it finds an error. Portfolio results
must therefore be interpreted with the worker logs; the two portfolio cases
passed without Valgrind and were inconclusive under it.

## Read the results

```sh
python3 checks/testing/report.py build/testing/results/memcheck-summary
python3 checks/testing/report.py build/testing/results/memcheck-summary --failures
python3 checks/testing/report.py build/testing/results/memcheck-summary --failures --tail 5 --commands
```

`memcheck-summary/failures.txt` lists every non-passing test, its input or stack
location, error types, log directory, and reproduction command. Future runs
produce the same section summaries as they execute. `--watch` follows an active
run, and `--verbose` on the runner prints passing tests individually too.

Remaining work includes larger independent semantic oracles, parser fuzzing,
proof/model validation, more option combinations, the UBSan/no-Z3 profiles,
and additional compilers and platforms. Coverage gaps determine where to add
tests next. Confirmed memory defects have not been fixed or filed upstream as
part of this testing setup.
