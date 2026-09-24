# Scope and completion criteria

This checkout targets upstream master at
`0257b80f8ab86e7e9e89b7358c0d69105a433561` plus PR #973 at
`4aa2d37488574324383a57d39dcdca680ce92370`. It includes merged PR #971 and the
revised polynomial-normalization fix, with the superseded LRS patch reverted.
Master-only measurements in MASTER_TEST_PROGRESS.md belong to a separate checkout.

## What passing means

A full campaign must finish every configured build and test stage, with no
failed or inconclusive cases. Its coverage gate requires exactly 100% of
instrumented lines, functions, and branches. Missing coverage data fails the
stage. Uncovered exception branches count as uncovered; they are not filtered
to improve the percentage. The report retains locations for every gap.

This target has not been reached. A passing coverage gate would show execution,
not correctness for every input or combination of settings.

## Tests prepared

| Area | Checks and expected results |
| --- | --- |
| Existing regressions | Every registered CTest suite, literal sanity assertions, and the full release sanity script |
| Propositional logic | All 512 subsets of the nine clauses over two variables, including the empty clause and empty clause set; truth-table expectations under LRS, DISCOUNT, and Otter |
| Boolean SMT and FOOL | Seeded expressions with independent truth tables; let shadowing, simultaneous bindings, and nesting through depth 128 |
| Quantifiers | All binary relations on domains of sizes 1, 2, and 3 for 28 formulas; quantifier order, shadowing, equality, seriality, symmetry, transitivity, and contradictions |
| Finite functions | All total unary functions on those domains; fixed points, involutions, injectivity, surjectivity, and pigeonhole contradictions |
| Input transformations | Symbol and bound-variable renaming, clause/literal reversal, duplicates, tautologies, and a fresh satisfiable clause |
| Equality | Congruence and equality chains through length 32, with three selection settings and three saturation algorithms |
| Option interactions | All 72 combinations of saturation algorithm, clausifier, AVATAR, selection, and term ordering on four constructed problems |
| Theories | Integer sign and machine-word boundaries, 100-digit integers, division/modulo, exact rational reals, nonlinear arithmetic, arrays, and datatypes; SMT expectations cross-checked with Z3 |
| Parsing | Empty inputs, comments, CRLF, missing final newline, quoted identifiers, identifiers through 4096 characters, nesting through depth 512, include selection, and transformed malformed regressions |
| Higher-order logic | Shipped HOL regressions under five clausification settings |
| CLI | Catalogue generated from each binary, including experimental options; documented values, aliases, invalid typed values, numeric overflow boundaries, time suffixes, and ratios |
| Processes and output | Existing timeout and LRS replay regressions, one/two-worker portfolios, all built-in schedules, schedule-file parsing, stdin, and 36 TPTP transformation round trips |
| Proof obligations | 36 SMT proof-output cases under LRS, DISCOUNT, and Otter with AVATAR on/off; Z3 checks emitted obligations, and unsupported steps remain inconclusive |
| Runtime options | 69 closed-domain FMB cases across nine options, plus explicit time, memory, and random-seed boundaries |
| Memory and UB | Debug assertions, cleanup enabled, Valgrind including child processes, ASan, and UBSan; no project suppressions |
| Build configurations | Release, debug cleanup, ASan, UBSan, no-Z3, and GCC coverage on Linux/WSL |

CLI parsing tests exit through help after parsing. They do not establish that
the option's solver feature ran. The option audit lists actual solver commands
separately and leaves missing behavioral tests visible. Numeric boundaries may
be accepted or explicitly rejected; that check validates the parser contract,
not the later use of the number. Cutoff lists and other string options are not
misclassified as integers merely because their defaults look numeric.

`print_theory_axioms=on` currently says it is unimplemented. That is recorded as
inconclusive. Timeouts and unsupported configurations also cannot count as
successful semantic tests. The no-Z3 build explicitly checks expected user-error rejection for 13 corpus
configurations requiring Z3. Those checks do not establish semantic correctness
for the unavailable features.

## Work still required

The coverage report and option audit are the work lists for the next round.
Every compiled option needs suitable behavioral fixtures, including options
that require files, interactive commands, particular problem properties, or
other options. The current five-option interaction matrix is bounded; it does
not cover every interaction among all 281 options found in the initial build.

Higher-order logic, polymorphism, induction, synthesis, unification, indexing,
and simplification have shipped regressions and unit tests. They still need
broader independent semantic generators and coverage-driven cases. Emitted SMT inference obligations are checked, but arbitrary whole proofs and
models are not yet independently certified by this campaign. TPTP round trips
reuse Vampire, and their secondary executions are not wrapped in Valgrind. Mutation testing and parser fuzzing
with minimization are also still needed.

Other compilers, operating systems, architectures, static linkage, build-time
switches, allocation failures, instruction-counter permissions, interactive
sessions, and resource exhaustion require further configurations or fixtures.
Native Windows and macOS execution cannot be inferred from a WSL run. ASan
does not instrument a separately built Z3 library.

Coverage includes the compiled Vampire implementation, debug support, bundled
Minisat, and SATSubsumption. It excludes test code and the separately maintained
CaDiCaL, VIRAS, Z3, and mini-GMP dependencies. Compiled-out source has no gcov
counters. This denominator is recorded rather than presented as coverage of
every possible build.

There is no finite enumeration of all Vampire inputs, numeric option values,
resource limits, or concurrent executions. The reports must keep those bounds
visible even after measured coverage reaches 100%.
