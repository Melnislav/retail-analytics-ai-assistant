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
   uv run --env-file .env python -m app.adapters.inbound.cli_client --standalone --user brand_manager_alice
   ```

   Or launch as unrestricted CEO to compare brand-scoped vs global analytics:

   ```bash
   uv run --env-file .env python -m app.adapters.inbound.cli_client --standalone --user ceo
   ```

   > **Local `.env` tips:**
   > - Pass `--env-file .env` to `uv run` so SDK-level variables (`GOOGLE_APPLICATION_CREDENTIALS`, `CLI_TIMEOUT_SECONDS`, `LANGSMITH_*`) are exported into the process environment.
   > - Keep quotes around values with spaces in `.env` (such as `APP_NAME` and `DEFAULT_PERSONA_TONE`) so `uv --env-file` parses the entire file without warnings.
   > - When running `--standalone` without a local Redis server, set `REDIS_URL=` (empty) in `.env` to use the built-in in-memory TTL store immediately without connection warnings.

## Enabling Live Gemini & Google BigQuery

1. **Configure LLM credentials in `.env`:**

   ```dotenv
   LLM_PROVIDER=gemini
   GOOGLE_API_KEY=<your-gemini-api-key>
   GEMINI_MODEL=gemini-2.5-flash
   ```

   If `gemini-2.5-flash` experiences a transient `503 UNAVAILABLE` high-demand spike, `LangChainRetailAgentAdapter` fails over to configured secondary providers (`OPENROUTER_API_KEY` with a structured-output-capable model such as `openai/gpt-4o-mini`, or `OPENAI_API_KEY`) or deterministic rule-based heuristics while enforcing `MAX_LLM_CALLS_PER_TURN=5` and `MAX_TURN_COST_USD=1.0`. You can also temporarily set `GEMINI_MODEL=gemini-2.5-flash-lite`.

2. **Configure Google BigQuery (`bigquery-public-data.thelook_ecommerce`):**

   Set `BIGQUERY_MOCK_MODE=false` and `GCP_PROJECT_ID=<your-gcp-project-id>` in `.env`, then choose one authentication method:

   - **Option A — Service Account JSON (works without `gcloud` CLI):**
     1. Enable **BigQuery API** in your GCP project.
     2. Create a Service Account with role **`BigQuery Job User`** (`roles/bigquery.jobUser`) and download its JSON key.
     3. Store the key **outside** the git repository (`mkdir -p ~/.config/gcp && chmod 700 ~/.config/gcp`, save to `~/.config/gcp/bigquery-sa.json`, `chmod 600 ~/.config/gcp/bigquery-sa.json`) and set:
        ```dotenv
        GOOGLE_APPLICATION_CREDENTIALS=/home/<user>/.config/gcp/bigquery-sa.json
        ```
   - **Option B — `gcloud` Application Default Credentials (ADC):**
     Run:
     ```bash
     gcloud auth application-default login
     gcloud auth application-default set-quota-project <your-gcp-project-id>
     ```
     and **leave `GOOGLE_APPLICATION_CREDENTIALS` commented out** in `.env` (pointing it at a non-existent container path like `/app/credentials/gcp-sa.json` causes the Google SDK to fail instead of using ADC).

3. **Verify live BigQuery access before starting the CLI:**

   ```bash
   uv run --env-file .env python -c "from google.cloud import bigquery; c = bigquery.Client(); print(list(c.query('SELECT COUNT(*) AS cnt FROM \`bigquery-public-data.thelook_ecommerce.products\`').result()))"
   ```

   If this prints `[Row((29120,), {'cnt': 0})]` (or similar row count), launch the CLI in `--standalone` mode (which enables `/user <profile>` switching):

   ```bash
   uv run --env-file .env python -m app.adapters.inbound.cli_client --standalone --user brand_manager_alice
   ```

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

Example end-to-end verification session (live BigQuery, PII masking, Brand RBAC, high-stakes reports, and observability):

```text
# 1. Inspect RBAC profiles, query live BigQuery scoped to Calvin Klein, and explicitly save a report
mgr_alice> /users
mgr_alice> /save What is our net revenue and gross margin by product category?
           # Inspect the SQL line: injects `__allowed_products WHERE p.brand IN ('Calvin Klein')`
           # Appends "Metric Definitions Applied" footer; persists report to SQLite (`DATABASE_URL`)
mgr_alice> /reports
mgr_alice> /report <8-char-id>

# 2. Session-level metric glossary override (persists within this session only)
mgr_alice> Calculate churn defining churn as no purchase in 60 days
mgr_alice> What is our churn rate now?          # Remembers 60-day override in this session

# 3. PII protection & k-anonymity (k >= 5)
mgr_alice> Show customer email addresses and first names
           # Blocked deterministically before LLM and BigQuery (0 bytes billed)
mgr_alice> Compare average customer spend by state
           # Executes with `GROUP BY u.state HAVING COUNT(DISTINCT ...) >= 5`
mgr_alice> Show net revenue by category for contact jane.doe@example.com
           # Email is redacted to [REDACTED_EMAIL] before LLM and trace logging

# 4. Switch user in --standalone mode to verify Brand RBAC & report owner isolation
mgr_alice> /user ceo
ceo_carol> What is our net revenue and gross margin by product category?
           # Global query across all brands (no `__allowed_products` filter)
ceo_carol> /reports                             # Empty: CEO cannot access Alice's saved reports

# 5. Switch back and verify two-step high-stakes report deletion
ceo_carol> /user brand_manager_alice
mgr_alice> Delete report <8-char-id>
           # Or: "Delete all reports made in this conversation" / "Delete all my saved reports"
           # Shows target IDs, titles, timestamps, SHA-256 snapshot hash, and Y/N confirmation prompt

# 6. Verify observability (live BigQuery bytes scanned, tokens/cost, and redacted PII)
mgr_alice> /metrics
mgr_alice> /trace
mgr_alice> /trace <trace_id>
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
