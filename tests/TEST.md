# TEST.md — Testing Standards & Workflow

This document is the single source of truth for testing in this repo. Every test
you write or modify must conform to it. If anything below conflicts with code
you find in the repo, fix the code — not the standard.

If you are Claude Code: read this file in full before writing or modifying any
test. When working on testing tasks, follow the phased workflow at the bottom.

---

## Stack

- Runner: `pytest` (configured via `pyproject.toml`, not `pytest.ini`)
- Coverage: `coverage.py` via `pytest-cov`
- Property-based: `hypothesis` for any function with a non-trivial input space
- Mocking: `pytest-mock` (prefer the `mocker` fixture over `unittest.mock.patch`)
- Time/clock: `freezegun` for any time-dependent code
- Mutation testing: `mutmut` (run quarterly or on critical modules, not every CI)

## Layout

- Tests live in `tests/`, mirroring the `src/` package structure 1:1.
  Example: `src/myapp/billing/invoice.py` → `tests/billing/test_invoice.py`.
- One test file per source module. Do not bundle.
- Shared fixtures go in the nearest `conftest.py`. Put broad fixtures at
  `tests/conftest.py`; scope-specific ones beside the tests that use them.
- Test data files (JSON, CSV, fixtures) go in `tests/data/`, never inline
  if larger than ~10 lines.

## Test anatomy — non-negotiable

1. **AAA structure**: every test has visible Arrange / Act / Assert sections,
   separated by a blank line. Comments only when the section's intent is
   not obvious from the code.
2. **One behavior per test.** If you need more than one logical assertion
   group, split the test.
3. **Descriptive names**: `test_<unit>_<condition>_<expected>`.
   Example: `test_invoice_total_with_zero_items_returns_zero`.
4. **No conditionals or loops** inside test bodies. Use `parametrize` instead.
5. **Tests must be independent.** No ordering, no shared mutable state,
   no reliance on filesystem cwd.
6. **Tests must be deterministic.** Seed random sources, freeze time,
   mock network/clock/filesystem at the boundary.

## What to test — coverage philosophy

We care about *behavior coverage*, not line coverage. For every public
function, class, or module entrypoint, write tests in this priority order:

1. **Happy path** — the documented contract with normal inputs.
2. **Boundary cases** — empty, zero, one, max, off-by-one, unicode edge cases.
3. **Error paths** — every `raise` in the code needs a `pytest.raises` test
   asserting both exception type and (where meaningful) message content.
4. **Equivalence classes** — group inputs that should behave the same;
   parametrize one test per class.
5. **Properties** — for pure functions over structured input (parsers,
   serializers, math, sort/dedupe/transform), add a Hypothesis test
   asserting an invariant (round-trip, idempotence, monotonicity, etc.).
6. **Regression tests** — every closed bug gets a test named after the
   issue/PR (`test_regression_issue_142_...`).

## What NOT to test

- Third-party library internals.
- Trivial getters/setters with no logic.
- Auto-generated code.
- Private helpers (`_foo`) directly — test them through the public surface.
  Exception: if a private helper has non-trivial logic and is hard to reach
  from the public API, that's a design smell — flag it, don't paper over.

## Mocking rules

- Mock at the boundary your code owns, not deeper. If your code calls
  `requests.get`, patch `requests.get` *as imported in your module*
  (`mocker.patch("myapp.client.requests.get")`), not at the library root.
- Never mock the unit under test.
- Prefer fakes/stubs over `MagicMock` when behavior matters. A `MagicMock`
  that returns `MagicMock` for everything is a test that asserts nothing.
- For HTTP, prefer `responses` or `respx` over hand-rolled mocks.
- For databases, use a real in-memory equivalent (SQLite for SQLAlchemy,
  testcontainers for integration) — not a mocked ORM.

## Coverage gates

- `pyproject.toml` requires **97% combined statement and branch coverage** for
  `src/physiclaw`. CI checks each runner's fast suite and the merged reports.
- Prioritize action authorization, provider validation, recovery and I/O errors.
  Assertions must verify behavior; 100% coverage is not required.
- Do not lower the gate or add exclusions to pass CI. Existing exclusions cover
  abstract methods, typing-only code and entry-point scaffolding.
- Python subprocess coverage is enabled. Native code, JavaScript, hardware
  tooling and Python processes started with `-S` are outside this report.

## Performance

- Aim for under 100ms per unit test.
- Mark slow tests with `@pytest.mark.slow` and exclude from the default run.
- Mark integration tests with `@pytest.mark.integration`.

## Workflow when adding tests to existing code

1. Read the source file end to end before writing any test.
2. List every public symbol, every branch, every `raise`.
3. Run existing tests for that module — note what's already covered.
4. Generate a `coverage.py` report scoped to the file: identify uncovered lines.
5. Write tests in the priority order above. Run after each new test.
6. After focused tests pass, run
   `mutmut run --paths-to-mutate src/<file>` and add tests until the
   surviving-mutant rate is <10%.
7. Commit tests in small, reviewable batches (one source file per commit).

## Commands

The Make targets select the locked `dev` and `cad` dependency groups before
collecting tests. Plain `uv run pytest` selects only the default groups, so CAD
tests can be skipped or fail against a stale CAD installation after an upgrade.

- Run all tests, including slow and integration: `make test-all` (alias: `make testall`)
- Run fast tests with coverage: `make test-cov`
- Run all suites with coverage: `make test-all-cov`
- Run one file: `uv run --locked --group dev --group cad pytest tests/test_package_init.py -v`
- Run one test: `uv run --locked --group dev --group cad pytest tests/test_package_init.py::test_package_version_matches_installed_distribution -v`
- Run only fast: `make test`
- Prepare the locked test environment without running tests: `make test-bootstrap`
- Run only slow or integration tests: `make test-slow` or `make test-integration`
- Check both UTF-8 modes: `make test-encoding` (individual modes: `make test-encoding-0` and `make test-encoding-1`)
- Mutation pass on a module: `make mutate MOD=src/physiclaw/common/text.py`

`.github/workflows/ci.yml` runs the fast, slow, integration and encoding suites
on Linux, macOS and Windows with Python 3.12, 3.13 and 3.14. Native-only tests
declare their platform requirements through pytest skips. Independent checks
continue after a test failure, while setup failures stop dependent checks.

CI merges fast, slow and integration coverage per runner, then across the matrix.
The aggregation job uses the `coverage` dependency group. Keep `source` in
`pyproject.toml` and use bare `--cov` so data can merge across checkouts. Slow and
integration coverage is checked after merging. Per-runner reports retain
platform-specific gaps.

JUnit reports, subprocess logs, raw coverage data, HTML reports and combined XML
are retained for seven days. Test job limits are 45 minutes on Windows and 30 on
Linux/macOS. Step limits are 30 minutes for fast tests and five each for slow
and integration tests.

The reusable `package.yml` workflow checks the same OS/Python matrix, including
fresh installs and upgrades. Both run `doctor`, HTTP readiness and graceful
server interrupt checks. Upgrades also verify that configuration, calibration,
macros and session data survive. See [RELEASING.md](../RELEASING.md) for baseline
selection.

## When you are uncertain

Stop and ask. Do not invent business rules to make a test pass. Do not
skip a test with `pytest.skip` to avoid thinking about it. If a function's
contract is ambiguous, the right output is a question, not a green test.

---

## Phased workflow for testing tasks

When asked to add tests to this repo (or any subset of it), work in five
phases. **Stop after each phase and wait for confirmation before continuing.**

### Phase 1 — Recon (read-only)

- Map the source tree. List every module under `src/` and its public surface.
- Identify the existing test runner, config, and any existing tests.
- Run the existing suite if any: report pass/fail/skip counts and current
  coverage (line and branch) per file.
- Output: a markdown table with columns
  `module | LOC | public_symbols | existing_tests | line_cov | branch_cov | risk`
  where `risk` is high/med/low based on logic density, external I/O, and
  current coverage. Save to `tests/_AUDIT.md`.

### Phase 2 — Plan

- Based on the audit, propose a prioritized list of modules to test, ordered
  by risk × business value. For each, list the test categories from "What to
  test" above that apply, and estimate test count.
- Identify any code that is hard to test and explain why (tight coupling,
  hidden state, time/network dependencies). Propose minimal refactors
  **only** where testing is otherwise impossible — do not rewrite the codebase.
- Save the plan to `tests/_PLAN.md`. Wait for approval before writing any tests.

### Phase 3 — Scaffold

- Set up `pyproject.toml` with pytest, coverage, hypothesis, pytest-mock,
  and freezegun configured per this document.
- Create the `tests/` directory mirror of `src/`.
- Create a top-level `tests/conftest.py` with shared fixtures only if needed.
- Add a `Makefile` or `justfile` with recipes for the commands listed above.
- **Do not write any actual tests in this phase.**

### Phase 4 — Execute (one module at a time)

For each module in the approved plan, in order:

1. Re-read the source file in full.
2. Write tests in the priority order from "What to test".
3. Run `uv run --locked --group dev --group cad pytest <test_file> --cov --cov-report=term-missing --cov-fail-under=0`.
   Inspect the target module's missing branches. Check the coverage gate with
   `make test-cov` or `make test-all-cov`.
4. Run `mutmut run --paths-to-mutate <source_file>` and report surviving
   mutants. Add tests to kill the meaningful ones. Document any survivors
   you intentionally accept (e.g., logging-only branches) inline in the
   test file.
5. Commit with message:
   `test(<module>): add suite — <N> tests, X% cov, Y% mutation score`.
6. Stop and report. Wait for "next" before moving on.

### Phase 5 — Integrate

- Keep `.github/workflows/ci.yml` running the suite on push and PR, with the
  full OS/Python matrix and coverage gate enforcement.
- Add a `tests/README.md` summarizing the conventions (point to this file
  as source of truth).
- Final report: total tests, coverage, mutation score, slow/integration
  test counts, and remaining coverage gaps with reasons.

### Hard rules for the whole job

- Do not modify source code except for the minimal refactors approved in Phase 2.
- Do not skip, `xfail`, or comment out tests to make CI green.
- Do not invent behavior. If the spec is unclear, ask.
- After every file, run the full fast suite
  (`pytest -m "not slow and not integration"`) to catch cross-module regressions.
