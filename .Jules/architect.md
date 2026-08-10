# Architect's Journal

## 2026-08-10 - Verify syntax/import claims against the pinned interpreter, not system python

**Proposal:** N/A — this is a correction to an existing proposal (PR #74,
"Add a CI workflow that runs lint, typecheck, and tests on every PR"), not a
new one.

**Why now?** While independently re-scanning for structural debt, I nearly
repeated the same mistake PR #74's write-up made: `src/drove/proxy.py` and
`src/drove/server_manager.py` contain `except A, B:` clauses (no
parentheses). Under Python <3.14 that's a hard `SyntaxError`. Running
`python3 -c "import ast; ast.parse(...)"` with whatever `python3` happens to
be on `PATH` reproduces that error — which is what PR #74 did, concluding
`drove serve` was unable to import on `master` for ~3.5 months undetected.

**Tradeoffs:** That conclusion was wrong. This repo requires
`python>=3.14` (`pyproject.toml`, `.python-version`), and Python 3.14 added
[PEP 758](https://peps.python.org/pep-0758/), which makes parenthesis-free
multi-exception `except` clauses valid syntax. Re-running the same check
with `uv run python3` (the project's actual pinned interpreter) parses
clean, and `uv run ruff check .` / `uv run mypy src` both pass with zero
issues on `master` right now. The code was never broken — only the
diagnostic tool was pointed at the wrong Python.

**Migration path:** Left a correcting comment on PR #74 rather than opening
a duplicate proposal, since the core idea (add `.github/workflows/ci.yml` —
nothing currently runs `pytest`/`ruff`/`mypy` in CI) is still sound; only
the "why now" framing and one migration step (a phantom follow-up bug-fix
PR) needed to be dropped.

**Lesson:** When a finding hinges on "this doesn't parse / doesn't import,"
always reproduce it with the project's own toolchain (`uv run python3`,
`uv run pytest`, `uv run ruff`, `uv run mypy` here — check the repo's actual
invocation, e.g. `Makefile` / CI config, for whatever project this is) before
treating it as fact, not whatever `python3` happens to resolve to on
`PATH`. A syntax feature can be valid on the project's required Python
version and invalid on an older one sitting in the sandbox by default.
