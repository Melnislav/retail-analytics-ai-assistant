# Retail Data Analysis Chat Assistant

Retail Data Analysis Chat Assistant is a Python prototype for retail executives: it turns natural-language analysis requests into guarded, read-only BigQuery queries (`bigquery-public-data.thelook_ecommerce`), applies deterministic PII masking and $k$-anonymity checks, persists user-owned saved reports with SHA-256 snapshot confirmation, and logs PII-redacted execution traces locally.

The production architecture, security model, and design-only capabilities live in [documentation/high_level_design_HLD.md](documentation/high_level_design_HLD.md). This file covers reproducible setup and running the prototype on another machine.

## Stack

Python 3.12+, FastAPI, LangGraph/LangChain, Google BigQuery, SQLAlchemy (SQLite locally / PostgreSQL in production), Redis (optional, with automatic TTL in-memory fallback), SQLGlot, Pydantic v2, Rich, Docker Compose, Ruff, and Mypy.

## Quick Start (Zero-Docker Local CLI Prototype)

1. **Install dependencies** using `uv`:

   ```bash
   cp .env.example .env
   uv sync --all-extras
   ```

2. **Run immediately in mock/offline evaluation mode** (no GCP or API keys required; uses local SQLite `retail_local.db`, simulated `thelook_ecommerce` warehouse, deterministic fallback planner, and `logs/agent_traces.jsonl`):

   ```bash
   uv run python -m app.adapters.inbound.cli_client --standalone --user brand_manager_alice
   ```

   Or launch as unrestricted CEO to compare brand-scoped vs global analytics:

   ```bash
   uv run python -m app.adapters.inbound.cli_client --standalone --user ceo
   ```

## Enabling Live Gemini & Google BigQuery

Edit `.env` with your credentials and set:

```dotenv
LLM_PROVIDER=gemini
GOOGLE_API_KEY=<your-gemini-api-key>
GEMINI_MODEL=gemini-2.5-flash

BIGQUERY_MOCK_MODE=false
GCP_PROJECT_ID=<your-gcp-billing-project>
GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-service-account.json
```

If an LLM provider fails or hits rate limits, `LangChainRetailAgentAdapter` fails over to configured secondary providers (`OPENROUTER_API_KEY` / `OPENAI_API_KEY`) or deterministic rule-based heuristics while keeping turn limits at `MAX_LLM_CALLS_PER_TURN=5` and `MAX_TURN_COST_USD=1.0`.

## Running with Docker Compose

1. Start the PostgreSQL, Redis, and FastAPI stack:

   ```bash
   docker compose up --build -d
   ```

2. Open Swagger UI at <http://localhost:8000/docs>, or start the containerized CLI client:

   ```bash
   docker compose --profile cli run --rm cli
   ```

   Note: In HTTP mode, the API enforces the server-side `PROTOTYPE_USER_PROFILE` from `.env` (default `brand_manager_alice`). Use `--standalone` to switch personas interactively with `/user <profile>`.

## Interactive CLI Walkthrough (Example Run)

Available commands inside the CLI:

- `/users` — list RBAC profiles from [configs/users_rbac.yaml](configs/users_rbac.yaml)
- `/user <profile>` — switch active user in `--standalone` mode (e.g., `/user ceo`, `/user brand_manager_bob`)
- `/save <question>` — ask an analytical question and explicitly save the report (question, SQL, and answer)
- `/reports` — list saved reports owned by the current user
- `/report <id>` — inspect a full saved report (supports 8-char ID prefix)
- `/metrics` — view counters, latency percentiles (`p50`/`p95`/`p99`), and trace log path
- `/trace [trace_id]` — inspect recent PII-scrubbed traces or a specific trace record
- `/clear` — start a new session thread
- `/exit` — exit the CLI

Example session:

```text
mgr_alice> Show net revenue by category
mgr_alice> Calculate churn defining churn as no purchase in 60 days
mgr_alice> What is our churn rate now?          # Remembers 60-day override in this session
mgr_alice> Show customer email addresses        # Blocked deterministically before LLM/SQL
mgr_alice> /save Top categories by revenue
mgr_alice> /reports
mgr_alice> /report <8-char-id>
mgr_alice> Delete all reports made in this conversation
           # Displays target report IDs, timestamps, SHA-256 snapshot hash, and Y/N prompt
mgr_alice> /metrics
mgr_alice> /trace
```

## Verification Checks

Run all required checks before committing:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest -v
```

## Documentation

- [Project goals, scope, and requirements](documentation/Retail%20assistant%20assessment.md)
- [High-Level Design: architecture, Mermaid diagrams, data flows, and production decisions](documentation/high_level_design_HLD.md)
- [AI-agent contribution contract](AGENTS.md)
