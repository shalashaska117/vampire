# Additional master tests, 24 September 2026

These results test upstream `86f6979ac662402591a142e16442a9a416efdab0`.
PR #971 was subsequently merged as `0257b80f8`; the runs below do not include it.
PR #973 is not applied. All changes in this testing checkout are under
`checks/testing/`. Raw logs remain under `build/testing/results/`.

The new behavior suite contains 229 cases, plus the option-discovery check.
It covers finite-model options, shutdown, stdin, time and memory limits,
TPTP transformation round trips, portfolio schedules, and SMT proof obligations.

| Run | Pass | Fail | Inconclusive |
| --- | ---: | ---: | ---: |
| Release behavior, including discovery | 205 | 5 | 20 |
| Release portfolio schedules only | 45 | 0 | 0 |
| No-Z3 corpus, including discovery | 205 | 0 | 0 |
| Coverage portfolio schedules only | 45 | 0 | 0 |
| Targeted Valgrind memory regressions | 0 | 20 | 0 |

All 42 harness regression tests pass. Earlier trial runs remain on disk,
including incorrect quantifier-free SMT schedule fixtures and an interrupted
ASan run that used Vampire's default address-space limit. They are not the
validation results above. The corrected schedules use quantified UF input.

## Findings

Five factoring cases print non-SMT SAT-proof lines inside the SMT proof region.
Z3 rejects those scripts. The smallest fixture is:

```tptp
cnf(a,axiom,(p(X)|p(Y))).
cnf(b,axiom,(~p(X)|~p(Y))).
```

The affected cases use LRS or Otter with AVATAR on/off, and DISCOUNT with AVATAR
on. Of the 36 proof cases, 13 pass the emitted-obligation check and 18 remain
inconclusive because Vampire explicitly skips unsupported proof rules.
These checks do not certify the whole proof or all introduced definitions.

Two finite-model option cases remain inconclusive: sort expansion gives up on
a singleton-domain contradiction, and SMT enumeration reaches its five-second
limit on that contradiction. All 36 TPTP round trips and all 12 stdin cases pass.
Round trips reuse Vampire as the solver; shared implementation errors may escape
them. Their secondary processes are not Valgrind-wrapped.

The coverage build aborts at `--memory_limit 20` while creating a thread:
`std::system_error: Resource temporarily unavailable`. The Release case passes.
This remains a failed resource-handling test.

All 20 targeted Valgrind cases return the expected logical answer but have memory
errors. The four finite-model cases reproduce uninitialized reads in
`IntUnionFind::root` and sort inference. A two-formula reproducer is:

```tptp
fof(domain,axiom,![X]:(X=a)).
fof(serial,axiom,![X]:(?[Y]:r(X,Y))).
```

The shutdown repetitions also reproduce invalid reads from freed options.
Allocation/read locations identify recurring diagnostics; they do not establish
the root cause of every leak or uninitialized value.

At 2026-09-24T11:55:49.523265+00:00, the separate ASan run had completed 1602/5466
cases: {'fail': 1600, 'inconclusive': 2}.
This is an interim count. It has reported heap-use-after-free during shutdown,
including timer accesses to `Options::instructionLimit`, and leaks. The run
retains leak detection, avoids abort-handler hangs, and removes the default
address-space limit from primary solver commands. Explicit memory-limit cases
keep their requested limits. Its snapshot predates the corrected SMT schedule
fixtures and five file-schedule cases; use `phase2-schedules-uf` for those results.

## Coverage

Counters accumulated on the same binary from the original master campaign and
the added behavior tests. No counters from another source revision were merged.

| Metric | Before | After |
| --- | ---: | ---: |
| Lines | 66.62% | 74.43% |
| Functions, including named aliases | 77.18% | 78.31% |
| Branches | 36.14% | 42.14% |

The earlier JSON report used source-location function groups and reported
82.26% function coverage. lcov's displayed summary counts named aliases,
including template instances, separately. The harness now counts those aliases
and lists unexecuted ones in coverage gaps. Group coverage rose from 82.26% to
84.55%, but the stricter function figures in the table match lcov's summary.
The 100% gate now requires both measures. No errors or branches are suppressed.

## Reproduce

```sh
python3 -m unittest discover -s checks/testing -p 'test_*.py'
python3 checks/testing/run.py run --suite behavior --build build/testing/release \
  --z3 /path/to/z3 --output build/testing/results/new-behavior
python3 checks/testing/run.py run --suite behavior --build build/testing/memcheck \
  --filter 'shutdown|fmb-memory' --memcheck --jobs 4 --timeout 45 \
  --output build/testing/results/new-memory-repros
```

Each result includes commands, inputs, logs, logical and memory outcomes, and a
snapshot of the harness. SMT checks also save the emitted script and Z3 output.
Failures and inconclusive cases still make the run exit nonzero.
