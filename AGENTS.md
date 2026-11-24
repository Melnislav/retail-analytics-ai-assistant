# AI Coding Agent Guidelines

This repository enforces strict static analysis, linting, formatting, and type-checking using **Ruff** and **Mypy**.
All AI coding agents (Junie, Cursor, Claude Code, Cline, Windsurf, Aider, GitHub Copilot) must follow the early feedback workflow described below.

---

## Fast Feedback Loop (Run Early & Often)

Whenever you edit or add any Python file, you **must** run the following verification checks immediately:

### 1. Fast Linting & Auto-fix
```bash
ruff check .
# Or apply safe automatic fixes:
ruff check --fix .
```

### 2. Code Formatting Check
```bash
ruff format --check .
# Or reformat files:
ruff format .
```

### 3. Static Type Verification
```bash
mypy .
```

> **Rule:** Never mark a task as completed or commit changes if `ruff check`, `ruff format --check`, or `mypy` report any errors or warnings.

---

## 🛠 Tooling Configuration Reference

All configurations are centralized in `pyproject.toml`:
- **Python version:** `>=3.12`
- **Line length:** `100` characters
- **Ruff rules enabled:**
  - `E`, `W` (pycodestyle errors & warnings)
  - `F` (Pyflakes syntax & import checks)
  - `I` (isort import ordering)
  - `B` (flake8-bugbear common bugs & design pitfalls)
  - `C4` (flake8-comprehensions)
  - `UP` (pyupgrade Python 3.12+ modernization)
  - `SIM` (flake8-simplify code simplification)
  - `RUF` (Ruff-specific rules)
- **FastAPI Dependency Injection:**
  - `B008` is configured to allow `Depends()`, `Header()`, `Query()`, etc. in default arguments.
- **Mypy:**
  - Configured with `check_untyped_defs = true`, `disallow_incomplete_defs = true`, `no_implicit_optional = true`.
  - Pydantic v2 support enabled.

---

## Git Pre-Commit Hooks

A `.pre-commit-config.yaml` is configured in the root directory. To run all hooks across the repository:
```bash
pre-commit run --all-files
```
This intercepts any issues before git commits are created.
