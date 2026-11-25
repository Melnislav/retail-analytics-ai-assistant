# AI Agent Contribution Contract

## Project Context

This repository contains a Python 3.12 retail analytics assistant. It accepts natural-language analysis requests, applies deterministic privacy and SQL policy checks, executes read-only analytics, and manages user-owned saved reports. Keep domain policy independent of frameworks and adapters.

Before making **any architectural change**, read the [High-Level Design](documentation/high_level_design_HLD.md). The assessment file is the source of project goals and requirements; do not duplicate or reinterpret its scope in implementation documentation.

## Directory Map

```text
app/
├── domain/          # Models, semantic glossary, PII masking, SQL policy, domain exceptions
├── application/     # Use cases, ports, chat orchestration, report lifecycle
├── adapters/        # HTTP and CLI inbound adapters; BigQuery, LLM, persistence, telemetry outbound adapters
├── infrastructure/  # Settings and dependency-composition root
└── main.py          # FastAPI application and lifecycle
configs/             # Local RBAC configuration
documentation/       # Requirements and the authoritative HLD
tests/               # Unit, integration, CLI/RBAC, resilience, and glossary tests
```

## Environment, Run, and Verification

```bash
# Install all runtime and development dependencies
uv sync --all-extras

# Start the full local stack and CLI
docker compose up --build -d
docker compose --profile cli run --rm cli

# Test suite
uv run pytest -v

# Required checks after every Python change
uv run ruff check .
uv run ruff format --check .
uv run mypy .

# Optional local commit gate
uv run pre-commit run --all-files
```

## Engineering Rules

- Preserve the hexagonal boundary: domain and application code depend on ports, never concrete adapters or web frameworks.
- Keep one responsibility per module and centralize security, privacy, and metric rules rather than copying them between layers.
- Treat user identity and allowed brands as trusted context supplied by the appropriate adapter; enforce least privilege and fail closed.
- Do not expose PII to an LLM, logs, or responses. Do not weaken the read-only SQL and k-anonymity controls.
- Saved-report deletion must remain owner-scoped and require an explicit confirmation step.
- Update the HLD when an approved architectural decision, component boundary, data flow, or production assumption changes. Keep setup instructions in `README.md` and agent-only rules here.
- When editing Python, run all three required verification commands above. Do not commit or mark work complete while any of them reports an error or warning.
