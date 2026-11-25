# Retail Data Analysis Chat Assistant

Retail Data Analysis Chat Assistant is a Python prototype for retail executives: it turns natural-language analysis requests into guarded, read-only BigQuery queries, produces concise reports, and supports a controlled saved-report workflow. The production architecture, security model, and design decisions live in the HLD; this file is the practical entry point for running the project.

## Stack

Python 3.12, FastAPI, LangGraph/LangChain, Google BigQuery, PostgreSQL, Redis, SQLAlchemy, SQLGlot, Pydantic v2, Rich, Docker Compose, Ruff, and Mypy.

## Quick Start

1. Create a local environment file and install all dependencies, including development tools:

   ```bash
   cp .env.example .env
   uv sync --all-extras
   ```

2. Use the default `BIGQUERY_MOCK_MODE=true` in `.env` for a credential-free evaluation. To query BigQuery, set `BIGQUERY_MOCK_MODE=false`, configure `GCP_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS`, and `GOOGLE_API_KEY`.

3. Start the complete local stack:

   ```bash
   docker compose up --build -d
   ```

4. Open the API documentation at <http://localhost:8000/docs>, or run the CLI client:

   ```bash
   docker compose --profile cli run --rm cli
   ```

5. Run the automated checks before contributing:

   ```bash
   uv run pytest -v
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy .
   ```

## Documentation

The documents below have distinct ownership to keep the project documentation free of duplicated requirements and architecture.

- [Project goals, scope, and requirements](documentation/Retail%20assistant%20assessment.md)
- [High-Level Design: architecture, Mermaid diagrams, data flows, and production decisions](documentation/high_level_design_HLD.md)
- [AI-agent contribution contract](AGENTS.md)
