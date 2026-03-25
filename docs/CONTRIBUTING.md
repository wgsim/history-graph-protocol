# Contributing to History Graph Protocol

Thank you for your interest in contributing to History Graph Protocol (HGP). This guide provides instructions for setting up your development environment and preparing contributions.

## Prerequisites

Before you begin, ensure you have:

- **Python** ≥ 3.12
- **uv**: Install from [https://docs.astral.sh/uv/](https://docs.astral.sh/uv/)
- **git**: Version control

Verify your Python version:

```bash
python --version  # Should be 3.12 or newer
```

## Development Setup

Clone the repository and initialize your environment:

```bash
git clone https://github.com/wgsim/history-graph-protocol
cd history-graph-protocol
uv sync --all-extras    # Creates .venv and installs dev dependencies
```

Verify the setup by running the test suite:

```bash
uv run pytest           # All 159+ tests should pass
```

If the tests pass, your environment is ready.

## Running Tests

### Full Test Suite

```bash
uv run pytest
```

### Single Test File

```bash
uv run pytest tests/test_db.py -v
```

### Single Test Case

```bash
uv run pytest tests/test_server_tools.py::test_create_operation_basic -v
```

### Alternative (without uv)

If uv is unavailable, use:

```bash
.venv/bin/python -m pytest
```

**Important:** Never use bare `python -m pytest`. The system `python` may be a different version and lack the required packages. Always use `uv run pytest` or `.venv/bin/python -m pytest`.

## Linting and Type Checking

All code must pass linting and type checks before submission.

### Lint with Ruff

Check for violations:

```bash
uv run ruff check src/ tests/
```

Auto-format code:

```bash
uv run ruff format src/ tests/
```

### Type Checking with Pyright

```bash
uv run pyright
```

Pyright runs in strict mode. All code must be type-safe.

### Pre-Submission Checklist

Before pushing, run all checks in order:

```bash
uv run ruff check src/ tests/       # Ensure no violations
uv run ruff format src/ tests/      # Auto-format if needed
uv run pyright                      # Type check
uv run pytest                       # Run tests
```

All four commands must succeed.

## Working with Git Worktrees

Use Git worktrees for isolated feature development without switching branches in the main checkout.

### Create a Worktree

```bash
git worktree add .worktrees/feat-my-feature -b feat/my-feature
cd .worktrees/feat-my-feature
uv sync --all-extras    # Create separate venv for this worktree
```

### Work in the Worktree

All your development and testing happens in the worktree. Each worktree has its own virtual environment.

### Remove When Done

```bash
cd ../..                # Return to repo root
git worktree remove .worktrees/feat-my-feature
```

**Note:** `.worktrees/` is in `.gitignore`. Worktrees are local only and never committed to git.

## Commit Conventions

Follow Conventional Commits format for all commits. This keeps history readable and enables automation.

### Commit Types

- **feat:** New feature
- **fix:** Bug fix
- **docs:** Documentation changes only
- **test:** Test additions or changes
- **chore:** Maintenance, dependencies, configuration
- **refactor:** Code refactor without behavior change

### Examples

```
feat: add chain hash verification

fix: prevent path traversal in object_hash validation

docs: update API reference for new endpoints

test: add tests for edge cases in compute_chain_hash

chore: update ruff to 0.9.0
```

### Atomic Commits

Each commit should represent one logical change. Avoid combining unrelated work in a single commit. This makes history easier to understand and enables efficient bisection for debugging.

## Pull Request Checklist

Before submitting a pull request, verify:

- [ ] All 159+ tests pass: `uv run pytest`
- [ ] No ruff violations: `uv run ruff check src/ tests/`
- [ ] Pyright passes: `uv run pyright`
- [ ] New features include tests
- [ ] Public-facing changes update relevant docs in `docs/`
- [ ] Commits follow Conventional Commits format
- [ ] Branch is up to date with `main`

Submit your PR against the `main` branch with a clear description of your changes.

## Questions?

If you have questions about the development process, check the existing documentation in `docs/` or open an issue on GitHub.
