# Vampire testing

This directory adds a repeatable test campaign to the existing unit tests and
`checks/sanity`. Every run saves its commands, inputs, stdout, stderr, binary
hashes, Git revision, and JSON results under a new output directory.

The current branch combines the fixes from upstream PRs #971 and #973. It does
not establish that Vampire is correct for every input or option combination.
Coverage measures executed code; it does not prove correctness.

## Build profiles

Use Linux or WSL with Python 3.9 or newer, CMake, GCC, gcov, lcov, and Valgrind. GCC and gcov
must be from the same toolchain. With GCC 15, use lcov 2.5 or newer; lcov 2.0
can fail on exception-branch metadata. Initialize the CaDiCaL and VIRAS submodules.
For Z3 support, build the pinned Z3 submodule or set `Z3_DIR` to that build's
CMake package directory.

```sh
git submodule update --init cadical viras
export Z3_DIR=/path/to/z3/build
export JOBS=8
bash checks/testing/build.sh coverage
bash checks/testing/build.sh release
bash checks/testing/build.sh memcheck
```

Profiles have separate build directories under `build/testing/`:

| Profile | Purpose |
| --- | --- |
| `coverage` | Debug assertions, cleanup, GCC line/function/branch counters |
| `release` | Existing sanity tests, including timing and LRS replay |
| `memcheck` | Debug assertions and cleanup, without coverage overhead |
| `ubsan` | Undefined-behavior sanitizer; stops on the first diagnostic |
| `no-z3` | Debug build with Z3 disabled |
| `debug` | Debug assertions and cleanup |

The extra profiles are available for separate campaigns. Their availability
does not mean they have been built or tested in a particular report.

## Run and inspect

Run commands from the repository root. Choose a fresh output directory for each
invocation; the runner refuses to overwrite earlier results.

```sh
python3 checks/testing/test_runner.py
python3 checks/testing/run.py inventory --output build/testing/inventory.json
python3 checks/testing/run.py run --suite all --jobs 4 \
  --output build/testing/results/coverage-all
python3 checks/testing/coverage.py --output build/testing/results/lcov
python3 checks/testing/run.py run --suite sanity --jobs 1 --timeout 180 \
  --build build/testing/release --output build/testing/results/sanity
```

Run sanity on an otherwise idle machine. It contains subsecond limits that can
fail under compiler, coverage, or Valgrind load. Such a failure still belongs in
the report, with its command and output.

The suites are:

- `units`: every suite registered with CTest in the selected build.
- `corpus`: literal assertions imported from `checks/sanity`. These keep their
  expected status, diagnostic, or exact output. Solver limits become ten seconds
  so this suite measures correctness separately from release timing.
- `generated`: all 512 subsets of the nine non-tautological clauses over two
  Boolean variables, run with LRS, DISCOUNT, and Otter; 96 seeded Boolean
  expressions checked with three supported clausifier/inlining configurations;
  and 20 signed/arbitrary-precision integer boundary cases. Truth tables and
  Python integer arithmetic supply the expected answers independently of Vampire.
- `sanity`: the unmodified shell suite, including its dynamic loops, timing
  checks, and LRS trace replay. This is separate from `all`.
- `features`: bounded finite-model cardinality, one/two-worker portfolios, and
  proof-output smoke tests. These do not independently certify emitted proofs.
- `all`: units, imported corpus assertions, generated cases, and feature tests.

The default seed is fixed and recorded. Use `--seed` for another reproducible
Boolean corpus. `--filter` selects case names by regular expression. `--limit`
selects a prefix and must be reported as a partial run.

`inventory.json` records all shipped `.p`, `.smt2`, `.ax`, and `.out` files and
their hashes. It identifies files without a directly imported assertion and
lists sanity commands that require shell expansion. A file may be an include
or expected-output fixture; absence of a direct assertion is a review item,
not automatically missing test coverage.

## Valgrind

```sh
python3 checks/testing/run.py run --build build/testing/memcheck \
  --suite all --memcheck --jobs 4 --timeout 180 \
  --output build/testing/results/memcheck-all
```

Memcheck records XML for each process, including children, and checks those
files even when a parent process exits successfully. Invalid memory accesses,
uninitialized reads, and definite, indirect, or possible leaks fail the run.
Reachable allocations remain in the XML as information. No project suppression
file is applied. `CHECK_LEAKS=ON` enables Vampire's optional cleanup paths.

The solver's internal timer is disabled for Memcheck, with a wall limit on each
process group instead. Killing a timed-out group can leave incomplete XML and
prevent final leak analysis; that result is inconclusive. Use a separate
`--solver-timeout` to override this policy.

See the [Memcheck manual](https://valgrind.org/docs/manual/mc-manual.html) for
the distinction between reachable allocations and lost allocations.

## Coverage

The lcov script combines an initial zero-hit capture with the executed capture,
so unexecuted instrumented objects stay in the denominator. It reports lines,
functions, and branches, and creates `html/index.html` plus a machine-readable
per-file summary. The tested build configuration determines which source files
are compiled; this is not coverage of every possible build configuration.

Reports include Vampire implementation, debug support, and the bundled Minisat
and SATSubsumption code. They exclude the test harness, CaDiCaL, VIRAS, Z3,
mini-GMP, and generated build files. Z3 is linked
from its own build and is not included in Vampire's coverage percentage.
See [lcov's capture documentation](https://github.com/linux-test-project/lcov/blob/master/docs/man/geninfo.rst).

Run capture after all instrumented processes have exited. Counters accumulate
across runs. To start a new campaign against the same binary, first run:

```sh
lcov --zerocounters --directory build/testing/coverage
```

Pass `--tool-dir /path/to/lcov/bin` to select matching lcov and genhtml
executables from a local checkout. `--jobs` controls capture parallelism.

Save the previous report first. Do not merge counters from different binaries
or revisions. Coverage data errors fail the capture. An unused source-exclusion pattern is
allowed because optional dependencies and generated files differ between the
initial and executed captures. GCC lines with an unexecuted non-branch block
are conservatively counted as unexecuted (`geninfo_unexecuted_blocks=1`).
All tool diagnostics remain in the report directory.

GCC can assign a line hit to an enclosing function while a lambda on that line
has zero calls. If lcov rejects this overlap, inspect the reported source and
use `--allow-overlapping-functions` to preserve the raw counters while disabling
its cross-metric consistency check. This exception is opt-in and recorded in
`settings.json`; malformed tracefiles and other data errors still stop the tool.
The baseline report used this option for the lambda in
`Shell/PartialRedundancyHandler.cpp:157`.

## Results and remaining work

Each case has `command.json`, logs, and `result.json`. `results.jsonl` is flushed
as cases complete; `summary.json` records the final totals. A run exits nonzero
for either failures or inconclusive cases. Timeouts, memory limits, and an
unexpected `GaveUp` do not count as passes. A test explicitly expecting `GaveUp`
can pass when that is the observed result. Rejection tests require their expected
diagnostic, not merely a nonzero exit.

Use uncovered branches and reproducible failures to choose the next tests.
The following work is still needed for broader assurance:

- Expand the generated domain beyond bounded propositional logic and ground
  arithmetic: quantifier alternation, polymorphism, higher-order terms, arrays,
  datatypes, induction, and finite models need independent semantic oracles.
- Generate malformed parser inputs with validated expectations and minimize
  crashes. Arbitrary mutations can remain valid inputs, so rejection alone is
  not a suitable oracle.
- Add metamorphic tests for alpha-renaming, equality symmetry, clause ordering,
  and redundant premises across preprocessing and inference configurations.
- Check proof output independently, with unsupported proof steps counted
  separately, and validate models against the original input.
- Run the remaining build profiles, compiler/platform combinations, resource
  boundaries, portfolio workers, and larger external TPTP/SMT-LIB corpora.
- Add deterministic reproductions for confirmed failures and track coverage
  changes between revisions. Keep timing-sensitive performance measurements
  separate from correctness checks.

No percentage threshold currently turns this campaign into a claim of complete
testing. Uncovered or untested areas remain visible in the inventory and reports.

To cross-check the generated SMT expectations and group memory reports:

```sh
python3 checks/testing/check_oracles.py build/testing/results/coverage-all/summary.json \
  --z3 /path/to/z3 --output build/testing/results/oracles.json
python3 checks/testing/triage.py build/testing/results/memcheck-all \
  --output build/testing/results/memcheck-groups.json
```

Stack groups identify recurring diagnostics, not confirmed root causes. Leak
reports from unit-test setup and shutdown need to be distinguished from leaks
during normal proving. No automatic suppression or issue publication is done.

## Terminal output

Each run prints its build, worker count, and per-section pass/fail/inconclusive
totals. Add `--verbose` to print every passing test too. Non-passing cases show the test name, input or stack location, reason,
expected/observed answer, memory-error counts, and log directory. A Valgrind
stack location is a navigation aid, not a confirmed root cause.

`terminal.log` preserves progress output. At completion, `failures.txt` groups
every non-passing test by section and includes the saved reproduction command.
For an existing or running campaign:

```sh
python3 checks/testing/report.py build/testing/results/memcheck-all --watch
python3 checks/testing/report.py build/testing/results/memcheck-all --failures
python3 checks/testing/report.py build/testing/results/memcheck-all --failures --tail 5 --commands
```

The watch view refreshes the table and recent failures; it does not stop the
tests when closed. Terminal clearing is used only on an interactive terminal.

Use `--resume` with the original run command to continue unfinished cases.
The runner checks the build hashes, seed, selection, and saved commands before
reusing results. Worker count and wall timeout may change. Completed failures
and inconclusive cases remain recorded; they are not silently retried or erased.
Interrupted case directories are archived before rerunning those cases.
