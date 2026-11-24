# Retail Data Analysis Chat Assistant

Production-ready prototype built with Python.

## Project Structure

```text
.
├── app/
│   ├── domain/                  # Pure business rules & Pydantic models (zero framework imports)
│   │   ├── models.py            # UserContext, SQLPlan, GuardrailDecision, SavedReport, PendingConfirmation
│   │   ├── pii_masker.py        # Column-level + regex PII redaction & customer pseudonymization
│   │   ├── sql_policy.py        # Read-only SQL firewall, table allowlist & product-level RBAC filter injection
│   │   └── exceptions.py        # Typed domain exception hierarchy
│   ├── application/             # Inbound/Outbound Ports (Protocols) & Orchestration Services
│   │   ├── ports.py             # ChatUseCasePort, AnalyticsWarehousePort, LLMAgentPort, GoldenKnowledgePort, etc.
│   │   ├── chat_orchestrator.py # Guardrails -> Golden Trios -> SQL Self-Correction -> PII Masking -> Synthesis
│   │   └── report_service.py    # Saved Reports CRUD + 2-step Human-in-the-Loop destructive deletion flow
│   ├── adapters/
│   │   ├── inbound/
│   │   │   ├── http_api.py      # FastAPI REST endpoints (/chat, /chat/confirm, /reports, /persona, /metrics)
│   │   │   └── cli_client.py    # Interactive Rich CLI chat client
│   │   └── outbound/
│   │       ├── bigquery_adapter.py     # Async BigQueryRunner + mock fallback for credential-free evaluation
│   │       ├── langchain_adapter.py    # LangChain async Gemini (primary) + OpenAI (fallback) structured outputs
│   │       ├── persistence_adapter.py  # Async SQLAlchemy (PostgreSQL / SQLite) repositories
│   │       └── redis_and_telemetry.py  # Async Redis cache/TTL confirmation store + Langfuse/metrics observability
│   ├── infrastructure/
│   │   ├── config.py            # Pydantic BaseSettings (.env)
│   │   └── container.py         # Composition root wiring Ports to Adapters
│   └── main.py                  # FastAPI entrypoint & lifespan
├── tests/
│   └── test_prototype.py        # Async pytest suite
├── docker-compose.yml           # Full stack: api + postgres + redis + interactive cli profile
├── Dockerfile
├── .env / .env.example
├── pyproject.toml / uv.lock
└── ARCHITECTURE.md              # Full production High-Level Design (HLD) & Mermaid diagrams
```

## Quick Start with Docker Compose

1. Review `.env` (already pre-configured with defaults; add `GOOGLE_API_KEY` and set `BIGQUERY_MOCK_MODE=false` with `GOOGLE_APPLICATION_CREDENTIALS` when connecting to live Gemini & GCP BigQuery):
   ```bash
   cp .env.example .env
   ```

2. Start the entire stack (`api`, `postgres`, `redis`):
   ```bash
   docker compose up --build -d
   ```

3. Launch the interactive CLI chat client against the running stack:
   ```bash
   docker compose --profile cli run --rm cli
   ```

4. Or test via HTTP (`http://localhost:8000/docs`):
   ```bash
   curl -X POST http://localhost:8000/api/v1/chat \
     -H "Content-Type: application/json" \
     -H "X-User-Id: exec_alice" \
     -H "X-User-Role: executive" \
     -d '{"message": "Why did our churn rate spike last month? Create a report with Q2 action items.", "save_report": true}'
   ```

## Authentication & BigQuery Setup

The dataset `bigquery-public-data.thelook_ecommerce` is publicly readable, while query execution quota is billed against the caller's own GCP project (`GCP_PROJECT_ID`). Choose one of three modes:

### Option A: Zero-Config Evaluation (Default)
Keep `BIGQUERY_MOCK_MODE=true` in `.env`. No GCP account or credentials are required; the adapter serves deterministic `thelook_ecommerce` fixtures so the full agent pipeline (guardrails, RBAC, PII redaction, HITL confirmation) can be tested immediately.

### Option B: Live BigQuery via Docker / Local Dev
1. Create a GCP project (free BigQuery Sandbox works without billing) and enable **BigQuery API**.
2. Grant your identity the **`BigQuery Job User`** (`roles/bigquery.jobUser`) role on that project.
3. Generate local credentials using **either** method:
   * **Service Account Key** (personal GCP accounts): Create a Service Account key (JSON) and save it to `./credentials/gcp-sa.json`.
   * **Application Default Credentials (ADC)** (if your org enforces `iam.disableServiceAccountKeyCreation`):
     ```bash
     gcloud auth application-default login
     gcloud auth application-default set-quota-project <YOUR_GCP_PROJECT_ID>
     cp ~/.config/gcloud/application_default_credentials.json ./credentials/gcp-sa.json
     ```
4. Update `.env` and restart the API:
   ```env
   BIGQUERY_MOCK_MODE=false
   GCP_PROJECT_ID=<YOUR_GCP_PROJECT_ID>
   GOOGLE_APPLICATION_CREDENTIALS=/app/credentials/gcp-sa.json
   GOOGLE_API_KEY=<YOUR_GEMINI_API_KEY>

## Running Automated Tests Locally

```bash
uv sync
uv run pytest -v
```

## Code Quality & Static Analysis (Ruff & Mypy)

To ensure immediate feedback for developers and AI coding agents at the earliest stage, the project is configured with **Ruff** (fast linting and formatting) and **Mypy** (static type checking).

```bash
# 1. Fast lint check (and safe auto-fix)
ruff check .
ruff check --fix .

# 2. Formatting verification (and auto-format)
ruff format --check .
ruff format .

# 3. Static type analysis
mypy .

# 4. Optional: run all git pre-commit hooks
pre-commit install
pre-commit run --all-files
```

Configuration is centralized in `pyproject.toml`. Guidelines for AI coding agents are documented in `AGENTS.md`.
