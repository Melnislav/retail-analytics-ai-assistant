# High-Level Design (HLD) — Retail Data Analysis Chat Assistant

> **Document ownership.** This HLD is the authoritative source for the system architecture, data flows, technology choices, operational design, and Mermaid diagrams. The canonical project goals, scope, and acceptance requirements are in [Retail assistant assessment.md](Retail%20assistant%20assessment.md). Setup belongs in the [README](../README.md); AI-agent implementation rules belong in [AGENTS.md](../AGENTS.md).

## 1. Executive Summary & Architectural Principles

The **Retail Data Analysis Chat Assistant** is an enterprise-grade AI solution designed for non-technical executive teams (e.g., retail managers and CEOs) to query complex retail transactional data using natural language. The system executes dynamic SQL queries against Google BigQuery's public dataset (`bigquery-public-data.thelook_ecommerce`), synthesizes actionable business insights, manages a library of saved executive reports, and exposes extensible hooks for future capabilities (e.g., automated charting, email distribution, web trend analysis).

### Core Architectural Principles
* **Domain-Driven Design (DDD):** Domain logic, security governance, and analytical policies are decoupled from external infrastructure adapters.
* **Hexagonal Architecture (Ports & Adapters):** Business logic interacts solely with abstract interfaces (Ports). Infrastructure components (LLM providers, analytical databases, persistence layers, CLI/Web frameworks) can be swapped seamlessly without altering core domain logic.
* **Single Responsibility Principle (SRP):** Each service, agent, and policy engine owns exactly one operational domain (e.g., AST security parsing, PII masking, report lifecycle management, metrics glossary resolution).
* **KISS & YAGNI:** A dual-profile deployment model guarantees a zero-dependency, local `Rich CLI` application running on `SQLite` for instant prototype evaluation, while providing a scalable containerized cloud architecture for production.
* **DRY (Don't Repeat Yourself):** Security boundaries, $k$-anonymity enforcement, and PII masking rules are centralized in a single domain-level `PrivacyPolicy` Value Object and reusable policy engines.

---

## 2. Framework Selection & Technical Rationale

Per assessment guidelines (Deliverable #6), the technical stack was selected based on production reliability, strict domain isolation capabilities, and architectural fit.

### Stack Specification & Rationale
1. **Core Language & Type System (Python 3.12+ & Pydantic v2):**
   * *Rationale:* Provides strong static typing, ultra-fast validation via Rust-based Pydantic core, and seamless interoperability with major AI/ML SDKs. Immutable value objects represent identity, security contexts, and analytical queries deterministically.
2. **LLM Orchestration & Fallback Framework (LangChain / Core Expression Language - LCEL):**
   * *Rationale:* Offers lightweight abstraction over multi-provider LLM integrations (Google Gemini, OpenAI, Anthropic) via unified interfaces (`BaseChatModel`). Allows declarative fallback chains (`Runnable.with_fallbacks()`), structured output parsing (`with_structured_output()`), and provider neutrality without lock-in.
3. **SQL Parsing & Security AST Engine (`SQLGlot`):**
   * *Rationale:* Pure Python SQL parser and transpiler capable of parsing BigQuery SQL dialects into Abstract Syntax Trees (AST). Enables deterministic inspection, security rule enforcement, CTE injections, and table/column allowlisting prior to query execution.
4. **Data Manipulation & In-Memory Analytics (Pandas & PyArrow):**
   * *Rationale:* Efficient DataFrame serialization of BigQuery execution results. Facilitates post-query demographic masking ($k$-anonymity verification) and PII redaction before data hits the LLM boundary.
5. **Inbound Frameworks (Rich CLI / FastAPI):**
   * *Rationale:* `Rich` powers terminal formatting (tables, syntax highlighting, progress spinners) for zero-dependency CLI execution. `FastAPI` (Production) provides asynchronous REST/SSE streaming endpoints backed by ASGI/Uvicorn.

### Architect Experience Statement
* *Experience Level:* Senior / Principal AI Solutions Architect level. Extensive hands-on experience in building enterprise-grade LLM applications, deterministic agentic workflows, function calling pipelines, and secure RAG systems on AWS and GCP. Proficient in applying DDD patterns to AI architectures, managing prompt security, and operating high-throughput analytical LLM pipelines in production.

---

## 3. Production Workload Sizing & Target AWS Infrastructure Topology

The production design is calibrated for the following operational parameters: **~100 active executive managers per day**, generating **~10 analytical queries per manager per day** (~1,000 queries/day total), saving **~1 report per manager per day**, and requiring simple query responses within **10–30 seconds** and complex report generation within **1–2 minutes**.

### Production Infrastructure Topology (AWS + Google Cloud)

```mermaid
flowchart TB
    subgraph Clients["Executive Clients"]
        WEB["Executive Web Application / Slack Bot"]
    end

    subgraph AWS["AWS Cloud Production Environment (us-east-1)"]
        ALB["AWS Application Load Balancer (ALB)"]
        
        subgraph Compute["ECS Fargate Container Cluster"]
            TASKS["FastAPI App Tasks (1-2 Tasks, 0.5 vCPU / 1 GB RAM)<br/>Async Request Handling & Structured Routing"]
        end

        subgraph Storage["Persistence & Caching"]
            RDS[("Amazon RDS PostgreSQL 16<br/>(pgvector enabled)<br/>• Saved Reports<br/>• User Profiles & Persona Drafts<br/>• Golden Trios Index")]
            S3[("Amazon S3 Data Lake<br/>• Golden Bucket Trios (JSONL/Parquet)<br/>• Exported PDF/CSV Reports")]
        end

        subgraph Governance["Security, Identity & Config"]
            IDP["AWS Cognito / OIDC Enterprise IdP<br/>(JWT issuing brand claims)"]
            SECRETS["AWS Secrets Manager<br/>(API Keys, Service Credentials)"]
            APPCONFIG["AWS AppConfig<br/>(Dynamic Persona Tone & Glossary Config)"]
        end

        subgraph Telemetry["Observability & Tracing"]
            CW["Amazon CloudWatch<br/>(System Logs, Container Metrics, Alarms)"]
            LANGFUSE[("Self-Hosted Langfuse on ECS Fargate<br/>(PII-Redacted Agent Traces & Costs)")]
        end
    end

    subgraph External["External Cloud Services"]
        GCP_IAM["Google Cloud Workload Identity Federation"]
        BQ[("Google BigQuery<br/>bigquery-public-data.thelook_ecommerce")]
        GEMINI["Google Gemini API (2.5 Flash)<br/>/ OpenAI GPT-4o Fallback"]
    end

    WEB --> ALB --> TASKS
    IDP -.->|Validate JWT Scopes| TASKS
    APPCONFIG -.->|Hot Reload Persona| TASKS
    SECRETS -.->|Fetch Credentials| TASKS

    TASKS --> RDS
    TASKS --> S3
    TASKS --> CW
    TASKS --> LANGFUSE

    TASKS -->|OIDC Token Exchange| GCP_IAM
    GCP_IAM -->|Service Account Token| BQ
    TASKS -->|REST Call via RateLimiter| GEMINI
```

### Resource Sizing & Cost Justification
* **Compute (ECS Fargate):** 1–2 tasks running 0.5 vCPU / 1 GB RAM effortlessly handle peak concurrency ($\sim 5$ concurrent requests during business peak hours) with average utilization $<20\%$. Estimated AWS compute cost: **~$$30/month**.
* **Database (RDS PostgreSQL):** `db.t4g.small` multi-AZ instance is sufficient for storing saved reports, preferences, and vector embeddings for 100 managers. Estimated database cost: **~$$40/month**.
* **Analytical Compute (Google BigQuery):** Queries run against `bigquery-public-data.thelook_ecommerce` (~100 MB per unaggregated query, controlled via AST projection and `dry_run` byte guards). At ~1,000 queries/day (~100 GB/day processed), total usage is ~3 TB/month. With 1 TB free tier, net cost is ~2 TB $\times$ $$6.25/TB = **~$$12.50/month**.
* **LLM Compute (Gemini 2.5 Flash):** At ~1,000 questions/day with 2–3 calls per question (~2,500 calls/day, ~2M input tokens/day), cost under Gemini 2.5 Flash ($$0.15 / 1M input tokens) is **< $$0.50/day** (**~$$15.00/month**), well within the **$$1.00 per query ceiling limit**.

---

## 4. Dual-Profile Deployment Model

To satisfy both prototype evaluation (zero Docker, immediate CLI run) and enterprise deployment standards, the system implements a **Dual-Profile Topology**:

1. **Prototype Profile (KISS / Zero-Docker):** Runs as an interactive terminal application (`Rich CLI`), storing saved reports in local `SQLite` (`retail_local.db`), session caches/confirmation tokens in-memory with TTL fallback (or Redis), and PII-redacted execution traces in structured `JSONL` (`logs/agent_traces.jsonl`).
2. **Production Profile (AWS Containerized):** Runs as a containerized `FastAPI` service on `AWS ECS Fargate`, utilizing `PostgreSQL 16` (`pgvector`), `Redis`, `AWS AppConfig`, `AWS S3`, `OpenTelemetry`, and `Langfuse`.

```mermaid
flowchart TB
    subgraph DrivingAdapters["Inbound Driving Adapters"]
        CLI["Rich CLI Client (Interactive Terminal)<br/>[Prototype Profile]"]
        WEB_API["FastAPI REST Router<br/>[Production & Docker Profile]"]
    end

    subgraph CoreDomain["Application Core (Hexagonal / Pure Python)"]
        ORCH["ChatOrchestratorService<br/>(TurnBudget: <= 5 LLM calls, <= $1.00)"]
        PRE["InputSafetyPolicy<br/>(Deterministic Ingress Pre-Guard)"]
        GLOSSARY["SemanticGlossary<br/>(YAML Defaults + Session Overrides)"]
        AST_ENGINE["SQLPolicyEngine (SQLGlot AST)<br/>(Brand Isolation, k-Anonymity, DML Block)"]
        POST_GUARD["PostSQLDataGuard<br/>(Pure-Python k-Anonymity Guard)"]
        MASKER["PIIMasker<br/>(Regex + HMAC Pseudonymization)"]
        REPORT_SVC["ReportService<br/>(2-Step SHA-256 Snapshot HITL Deletion)"]
    end

    subgraph Ports["Ports (app/application/ports.py)"]
        LLM_PORT["LLMAgentPort"]
        DB_PORT["AnalyticsWarehousePort"]
        STORE_PORT["ReportRepositoryPort / CachePort"]
        TRACE_PORT["ObservabilityPort"]
    end

    subgraph DrivenAdapters["Outbound Driven Adapters"]
        LLM_ADAPTER["LangChainRetailAgentAdapter<br/>(Gemini 2.5 Flash -> OpenRouter/OpenAI Fallback)"]
        BQ_ADAPTER["AsyncBigQueryAdapter<br/>(Dry-Run Byte Guard + Read-Only Runner)"]
        DB_LOCAL[("Prototype: SQLite (retail_local.db)<br/>+ In-Memory TTL Cache")]
        DB_PROD[("Production: PostgreSQL 16<br/>(pgvector) + Redis + AWS S3")]
        TRACE_LOCAL["Prototype: StructuredObservabilityAdapter<br/>(logs/agent_traces.jsonl)"]
        TRACE_PROD["Production: CloudWatch + Langfuse"]
    end

    CLI --> ORCH
    WEB_API --> ORCH

    ORCH --> PRE
    ORCH --> GLOSSARY
    ORCH --> AST_ENGINE
    ORCH --> POST_GUARD
    ORCH --> MASKER
    ORCH --> REPORT_SVC

    ORCH --> LLM_PORT --> LLM_ADAPTER
    ORCH --> DB_PORT --> BQ_ADAPTER
    ORCH --> STORE_PORT
    REPORT_SVC --> STORE_PORT
    ORCH --> TRACE_PORT

    STORE_PORT -.-> DB_LOCAL
    STORE_PORT -.-> DB_PROD
    TRACE_PORT -.-> TRACE_LOCAL
    TRACE_PORT -.-> TRACE_PROD
```

---

## 5. End-to-End Execution Pipeline & Security Flow

The system processes incoming queries through a multi-tier pipeline enforcing security rules deterministically in Python before queries execute against BigQuery or raw data reaches the LLM.

```mermaid
flowchart TD
    REQ["User Natural Language Query + SecurityContext"] --> PRE_GUARD{"Deterministic Pre-Guard<br/>(System Commands / PII Injection Check)"}
    PRE_GUARD -->|Blocked| REFUSE["Safe Refusal Response + Trace Event"]
    PRE_GUARD -->|Allowed| LLM_PLAN["Structured LLM Call (Intent + Glossary Override + SQL Generation)<br/>[LLM Call #1]"]

    LLM_PLAN --> ROUTE{"Intent Router"}

    ROUTE -->|Delete Reports| PREP_DEL["ReportService.prepare_deletion()<br/>(Owner Isolation + SHA-256 Snapshot + TTL 600s)"]
    PREP_DEL --> SHOW_DEL["Render Target Report List + Hash to User"]
    SHOW_DEL --> CONFIRM_DEL{"User Confirms Hash & Action?"}
    CONFIRM_DEL -->|Confirmed| EXEC_DEL["Transactional Delete + Security Audit Log"]
    CONFIRM_DEL -->|"Cancelled / Expired"| ABORT_DEL["Abort Operation + Retain Data"]

    ROUTE -->|"List / View Reports"| READ_REP["ReportService.get_user_reports()<br/>(Strict author_id Filter)"]

    ROUTE -->|Analytical Query| AST_VAL{"SQLPolicyEngine (SQLGlot AST)<br/>1. Single SELECT Only<br/>2. Allowlisted Tables Only<br/>3. Block PII & direct orders.gender<br/>4. Rewrite Brand CTE Isolation<br/>5. Inject HAVING COUNT(DISTINCT user_id) >= k"}

    AST_VAL -->|"Hard Violation (PII / Brand / DML)"| FAILS_FAST["0 Retries: Fail Fast + Audit Security Violation"]
    AST_VAL -->|"Soft Format Failure (e.g. SELECT *)"| SOFT_RETRY{"Soft Repair Attempt <= 1?"}
    SOFT_RETRY -->|Yes| LLM_PLAN
    SOFT_RETRY -->|No| REJECT_SOFT["Reject Query with Formatting Error"]

    AST_VAL -->|Valid AST| DRY["AsyncBigQueryAdapter.dry_run()<br/>(Validate total_bytes_billed <= Limit)"]
    DRY -->|Exceeds Budget| EXPENSIVE["Halt & Prompt User to Narrow Range"]
    DRY -->|Within Budget| BQ_EXEC["Execute Read-Only SQL Query"]

    BQ_EXEC -->|Syntax Error| RETRY_SYN{"Syntax Repair Attempt <= 2?"}
    RETRY_SYN -->|Yes| LLM_PLAN
    RETRY_SYN -->|No| GRACEFUL_ERR["Graceful Error Message"]

    BQ_EXEC -->|0 Rows Returned| RETRY_EMPTY{"Empty Repair Attempt <= 1?"}
    RETRY_EMPTY -->|"Yes: Widen Casing / Date Filter"| LLM_PLAN
    RETRY_EMPTY -->|"No: Still 0 Rows"| CLARIFY["Return Explanation + Ask Clarifying Question"]

    BQ_EXEC -->|Data Returned| POST_GUARD{"PostSQLDataGuard (Pandas)<br/>Verify Demographic Aggregates >= k"}
    POST_GUARD -->|Mask Below k| MASK_DF["Mask Rows Below k Threshold"]
    POST_GUARD -->|Valid| PII_STRIP["PIIMasker: Convert user_id to HMAC Pseudonym"]

    MASK_DF --> PII_STRIP
    PII_STRIP --> LLM_SYNTH["Analyst Synthesis LLM Call<br/>[LLM Call #2]"]
    LLM_SYNTH --> FOOTER["Inject Deterministic Metric Glossary Footer"]
    FOOTER --> PII_OUT["PIIMasker: Post-LLM Output Scrubbing"]
    PII_OUT --> RESP["Final Response Rendered + Trace Logged"]
```

---

## 6. Detailed Specification Across All 8 Requirements

### 6.1. Safety, PII Masking & Brand RBAC (Prototype + Production)

#### Immutable Central Security Policy (`PrivacyPolicy` Value Object)
To adhere to DRY, privacy boundaries are defined in a central `PrivacyPolicy` domain object:
* **Explicit PII Columns:** `first_name`, `last_name`, `email`, `street_address`, `postal_code`, `latitude`, `longitude`, `user_geom`.
* **Demographic Columns:** `city`, `state`, `country`, `age`, `gender`, `traffic_source`.
* **Demographic Grouping Rule:** Demographic columns are prohibited as raw unaggregated attributes. They are permitted **only** within aggregated `GROUP BY` queries enforcing `HAVING COUNT(DISTINCT user_id) >= k` (default $k=5$, configurable).
* **Schema Leakage Rules:** Direct references to `orders.gender` without $k$-anonymity checks are rejected at the AST level.

#### Identity & Brand Access Control
* **Identity Loading (`SecurityContext`):**
  * In CLI Prototype: Loaded from `configs/users_rbac.yaml` or startup flags (`--user ceo`, `--user brand_mgr`).
  * In Production: Extracted from verified JWT claims issued by AWS Cognito / OIDC provider: `SecurityContext(user_id, role, allowed_brands)`.
* **Brand Access Scope:**
  * Access is scoped strictly by **`brand`**. Brand managers can query data associated only with their assigned brands. CEOs have unrestricted access across all brands. If a non-CEO user has no assigned brands, queries fail-closed.
  * Customer and order metrics are accessible **only through purchases of in-scope products**.

#### Dynamic AST Policy Engine (`SQLPolicyEngine` via `SQLGlot`)
Parses incoming BigQuery SQL into an AST and applies deterministic transformations:
1. Rejects multi-statement queries, DML (`INSERT`, `UPDATE`, `DELETE`), DDL (`DROP`, `ALTER`), wildcard projections (`SELECT *`), and system catalog tables.
2. **Multi-Brand CTE Row Isolation:** Automatically injects non-recursive CTEs scoping products, order items, orders, users, and multi-brand metrics:
   ```sql
   WITH __allowed_products AS (
     SELECT p.* FROM `bigquery-public-data.thelook_ecommerce.products` AS p
     WHERE p.brand IN ('BrandA', 'BrandB')
   ),
   __scoped_order_items AS (
     SELECT oi.* FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
     INNER JOIN __allowed_products AS p ON oi.product_id = p.id
   ),
   __scoped_orders AS (
     SELECT o.order_id, o.user_id, o.status, o.gender, o.created_at,
            o.returned_at, o.shipped_at, o.delivered_at, COUNT(oi.id) AS num_of_item
     FROM `bigquery-public-data.thelook_ecommerce.orders` AS o
     INNER JOIN __scoped_order_items AS oi ON oi.order_id = o.order_id
     GROUP BY o.order_id, o.user_id, o.status, o.gender, o.created_at,
              o.returned_at, o.shipped_at, o.delivered_at
   ),
   __scoped_users AS (
     SELECT u.* FROM `bigquery-public-data.thelook_ecommerce.users` AS u
     WHERE EXISTS (SELECT 1 FROM __scoped_order_items AS oi WHERE oi.user_id = u.id)
   )
   -- Original query table references rewritten to point to __allowed_products / __scoped_* CTEs
   ```
3. **Multi-Brand Basket Size Protection:** Inside `__scoped_orders`, `orders.num_of_item` for non-CEO users is replaced with `COUNT(oi.id)` over `__scoped_order_items` to prevent revealing total item counts of competitor brands in mixed-brand orders.

#### Post-SQL Data Guard & Zero-PII Perimeter
* **Post-SQL Guard:** As a secondary defense-in-depth layer, `PostSQLDataGuard` inspects returned rows in pure Python before caching or synthesis. If demographic dimensions are present, any aggregation row where the distinct user count is $< k$ is masked (`"[Hidden: group < 5]"`), and demographic columns paired with user identifiers are scrubbed (`"[REDACTED_DEMOGRAPHIC]"`).
* **Deterministic Pseudonymization:** Raw numeric `user_id` values are pseudonymized before reaching LLM prompts, storage, or final UI rendering:
  $$\text{Pseudonym} = \text{HMAC-SHA256}(\text{user\_id}, \text{SecretSalt})[:12]$$
* **Regex Pre/Post Scrubbing:** All text payloads passing into LLM prompts or out to client rendering pass through `PIIMasker` regex scrubbers (redacting emails, phone numbers, street addresses, and coordinates).

---

### 6.2. High-Stakes Oversight: Destructive Operations (Prototype + Production)

#### Saved Report Domain Model
Saved reports represent snapshot analytical assets owned by executives (`SavedReport` in `app/domain/models.py`):
```python
class SavedReport(BaseModel):
    report_id: str
    owner_user_id: str
    session_id: str
    question: str
    title: str
    summary: str
    content_markdown: str
    action_items: list[str]
    sql_used: str | None
    metric_definitions: list[str]
    created_at: datetime
```
* **Ownership Rule:** Reports can only be viewed or deleted by their original author (`owner_user_id == user_id`).

#### Two-Step Confirmation Workflow (TOCTOU-Safe)

```mermaid
sequenceDiagram
    autonumber
    actor Exec as Executive Manager
    participant Agent as ChatOrchestrator
    participant RepSvc as ReportService
    participant Store as StorageAdapter (SQLite/PostgreSQL)

    Exec->>Agent: "Delete all reports made in this conversation"
    Agent->>RepSvc: prepare_deletion(user_id, thread_id)
    RepSvc->>Store: Query reports WHERE author_id = user_id AND thread_id = X
    Store-->>RepSvc: Return [Report #12, Report #15]
    RepSvc->>RepSvc: Create DeletionProposal Snapshot & Compute SHA-256 Payload Hash
    RepSvc->>Store: Save DeletionProposal Token (TTL=600s, Hash, ReportIDs)
    RepSvc-->>Agent: Return DeletionProposal
    Agent-->>Exec: Render Table of Target Reports [12, 15] + Hash + Request Confirmation (Y/N)

    alt User Approves (Y)
        Exec->>Agent: "Yes, confirm deletion"
        Agent->>RepSvc: execute_deletion(token_id, user_id)
        RepSvc->>Store: Fetch Token & Verify active status + author_id match
        RepSvc->>Store: Re-verify SHA-256 Hash equality of target IDs
        RepSvc->>Store: Execute ACID DELETE FROM saved_reports WHERE id IN (12, 15)
        RepSvc->>Store: Invalidate Token
        RepSvc-->>Agent: Success Confirmation
        Agent-->>Exec: "Successfully deleted 2 reports [12, 15]."
    else User Rejects or Token Expired
        Exec->>Agent: "No / Cancel"
        Agent->>RepSvc: cancel_deletion(token_id)
        RepSvc->>Store: Invalidate Token
        Agent-->>Exec: "Deletion cancelled. No data modified."
    end
```

* **Snapshot Hash Formula:**
  $$\text{PayloadHash} = \text{SHA256}(\text{SortedReportIDs} \mathbin{\Vert} \text{user\_id} \mathbin{\Vert} \text{nonce})$$
* **Execution Safety:** Deletion executes inside an ACID database transaction. Target report IDs are re-verified against current database state before deletion to prevent Time-of-Check to Time-of-Use (TOCTOU) race conditions.

---

### 6.3. Resilience & Graceful Error Handling (Prototype + Production)

#### Differentiated Error Policy Engine

| Fault Category | Max Retries | Strategy & Action Taken |
| :--- | :--- | :--- |
| **Hard Security / Policy Violation** (PII request, DML, unauthorized brand access) | **0 Retries** | **Fail Fast:** Immediately halt query generation, log security audit event, return friendly policy explanation to user. |
| **Soft Formatting Violation** (e.g. Missing `GROUP BY` for aggregates, `SELECT *`) | **1 Repair** | **Targeted Correction:** Pass policy error message back to LLM with directive to format explicitly. |
| **BigQuery Syntax / Column Error** | **2 Repairs** | **Targeted SQL Repair:** Feed BigQuery compiler error back into promptcontext for corrective fix. |
| **Empty Result Set ($0$ Rows)** | **1 Repair** | **Filter Relaxation:** Attempt 1 widen retry (adjusting case sensitivity or widening date ranges). If still 0 rows, return clear explanation without hallucinating. |
| **LLM Provider API Error / Rate Limit (HTTP 429/5xx)** | **Automatic Fallback** | **Gateway Switch:** Switch from `Gemini 2.5 Flash` to configured secondary provider (`OpenRouter` / `OpenAI`) via `Runnable.with_fallbacks()`, then deterministic rule-based heuristics if all providers fail. |

#### Provider Multi-Tenancy & Testing Support
* **Primary & Fallback Routing (`LangChainRetailAgentAdapter`):** Allows configuring the primary provider via environment variable (`LLM_PROVIDER=gemini|openrouter|openai`) with automatic fallback chains so reviewers can run prototype evaluations with their own keys or completely offline.
* **Request Budget Guard (`TurnBudget`):** Enforces strict limits of **$\le 5$ LLM calls** (`MAX_LLM_CALLS_PER_TURN`) and **$\le \$1.00$ total compute cost** (`MAX_TURN_COST_USD`) per query turn.

---

### 6.4. Observability & Deep-Dive Debugging (Prototype + Production)

#### Correlation Tracing Architecture
Every incoming executive query generates a unique `trace_id` propagated across all internal domain services (`TraceRecord` in `app/domain/models.py`).

#### Dual-Profile Observability Adapters
* **Prototype Profile:** `StructuredObservabilityAdapter` persists PII-scrubbed `TraceRecord` entries to `logs/agent_traces.jsonl`, computes latency percentiles (`p50`, `p95`, `p99`), and exposes `/metrics` and `/trace [id]` in the CLI plus `/api/v1/metrics` and `/api/v1/traces` in HTTP mode.
* **Production Profile:** `OpenTelemetry` SDK exports spans to `AWS CloudWatch` (system metrics/alarms) and self-hosted `Langfuse` on Fargate (PII-scrubbed agent execution traces).

#### Tracked Agent Metrics
1. **Performance Metrics:** Query Latency ($p50, p95, p99$), LLM Time-to-First-Token (TTFT), Prompt/Completion Token counts.
2. **Data & Policy Metrics:** BigQuery Bytes Billed per query, AST Policy Violation Rate, $k$-Anonymity Masking Counter, PII Scrubbing Counter.
3. **Reliability & Cost Metrics:** LLM Provider Fallback Count, $0$-Row Return Frequency, Cost Per Question ($< \$1.00$), HITL Confirmation Rate.

---

### 6.5. Hybrid Intelligence: "Golden Knowledge" Bucket (Design-Only HLD)

#### Concept & Architecture
Human analysts curate high-value "Trios": $(\text{Natural Language Question}, \text{Optimized SQL Query}, \text{Analyst Report})$.

```mermaid
flowchart LR
    RAW_TRIO["Human Analyst Input Trio"] --> S3_LAKE[("Amazon S3 Data Lake<br/>Raw Golden Trios Bucket")]
    S3_LAKE --> PII_PROC["PII Scrubbing & Skeleton Extraction"]
    PII_PROC --> EMBED["Generate Text Embeddings"]
    EMBED --> PG_VECTOR[("Amazon RDS PostgreSQL<br/>(pgvector Repository)")]

    USER_Q["User Query"] --> HYBRID_SEARCH["Hybrid Search<br/>(Vector Similarity + BM25)"]
    PG_VECTOR --> HYBRID_SEARCH
    HYBRID_SEARCH --> SCOPE_FILTER{"Matches User's Allowed Brand?"}
    SCOPE_FILTER -->|Yes| FULL_TRIO["Inject Top-3 Full Trios as Untrusted Context"]
    SCOPE_FILTER -->|No| SKELETON_ONLY["Inject Top-3 SQL Skeletons Only (No Business Metric Leak)"]

    FULL_TRIO --> PROMPT["LLM System Prompt Context"]
    SKELETON_ONLY --> PROMPT
    PROMPT --> AST_FIREWALL["Pass Generated SQL to SQLPolicyEngine AST"]
```

#### Multi-Brand Isolation in RAG Context
To prevent business data leakage between brands:
* If a retrieved Golden Trio belongs to an allowed brand, the full example is provided as few-shot context tagged as `<untrusted_historical_example>`.
* If a retrieved Golden Trio belongs to an unauthorized brand, the system strips analytical commentary and business values, injecting only the anonymized `sql_skeleton` (e.g., query structure without specific brand names or metric figures).

#### Continuous Knowledge Bucket Lifecycle
New candidate Trios are added to the Golden Bucket only after passing a 4-stage pipeline: (1) Executive thumbs-up / explicit submission, (2) Human analyst review, (3) Automated regression testing against `SQLPolicyEngine`, and (4) Complete PII scrubbing.

---

### 6.6. Continuous Improvement: The Learning Loop (Design-Only HLD)

#### User-Level Preference Adaptation
Tracks individual presentation preferences using a dedicated domain aggregate:
```python
@dataclass
class UserPreferences:
    user_id: str
    preferred_format: str  # "table" | "bullet_points" | "narrative"
    analysis_depth: str  # "executive_summary" | "deep_dive"
    tone_preference: str  # "concise" | "detailed"
    auto_chart_enabled: bool
```
* **Proactive Preference Confirmation ("2-Repeat Rule"):** If a user explicitly requests a format change twice in a single conversation (e.g., *"Show this as a table"*), the agent presents a single-click confirmation asking: *"Would you like me to set 'Table format' as your default preference for future sessions?"*.

#### System-Level Improvement Pipeline
* **Offline Negative Feedback Clustering:** Queries receiving negative feedback (`thumbs_down`, prompt edits, SQL repair failures) are captured in an offline queue.
* **Glossary & Schema Gap Identification:** Clustered failures are reviewed by Data Engineering teams to identify missing metric definitions, missing table joins, or ambiguous vocabulary.
* **No Runtime Prompt Poisoning:** Real-time online model updating is explicitly prohibited to prevent prompt injection poisoning attacks.

---

### 6.7. Quality Assurance & Evaluation Framework (Design-Only HLD)

Pre-deployment verification combines deterministic security suites, data execution benchmarks, LLM-as-a-Judge evaluation, and UX telemetry:

```mermaid
flowchart TD
    CI_GATE["CI/CD Deployment Gate"] --> SUITE_1["1. Deterministic Security Suite (pytest)"]
    CI_GATE --> SUITE_2["2. Execution Accuracy Benchmark (Golden Set)"]
    CI_GATE --> SUITE_3["3. LLM-as-a-Judge Evaluation"]
    CI_GATE --> SUITE_4["4. UX Telemetry & SLA Benchmarks"]

    SUITE_1 --> CHK_1{"100% Security & AST Mutation Tests Pass?"}
    SUITE_2 --> CHK_2{"DataFrame Result Match >= 95%?"}
    SUITE_3 --> CHK_3{"Faithfulness & Glossary Score >= 4.5/5.0?"}
    SUITE_4 --> CHK_4{"Latency p95 <= 30s (Simple) / 120s (Report)?"}

    CHK_1 -->|Yes| PASS_1[Pass Gate]
    CHK_2 -->|Yes| PASS_2[Pass Gate]
    CHK_3 -->|Yes| PASS_3[Pass Gate]
    CHK_4 -->|Yes| PASS_4[Pass Gate]

    PASS_1 & PASS_2 & PASS_3 & PASS_4 --> DEPLOY["Deploy to Production (AWS ECS)"]

    CHK_1 -->|No| REJECT["Block Release"]
    CHK_2 -->|No| REJECT
    CHK_3 -->|No| REJECT
    CHK_4 -->|No| REJECT
```

#### Test Suite Specifications
1. **Deterministic Security Suite (`pytest`):** Executes mutation tests including AST bypass attempts (`UNION ALL` injections, nested CTE aliases, direct `orders.gender` queries, and `orders.num_of_item` probes in multi-brand queries).
2. **Execution Accuracy Benchmark:** Evaluates data equivalence by comparing output DataFrames generated by agent SQL against ground-truth DataFrames over an evaluation dataset of 30 test cases across 5 functional categories.
3. **LLM-as-a-Judge Evaluation:** Evaluates synthesized responses for **Faithfulness** (no hallucinations beyond executed SQL DataFrames), **Completeness** (answering all parts of executive queries), and **Glossary Compliance**.

#### Reference Evaluation Set (5 Core Capability Categories)

```markdown:Eval Set Example:docs/eval_set_example.md
| ID | Capability Category | Test Query | Expected SQL Pattern / Behavior | Success Criterion |
| :--- | :--- | :--- | :--- | :--- |
| **EV-01** | **Customer Behavior** | "Who are our top spending customers in California?" | AST injects `__allowed_products` / `__scoped_*` CTEs. Selects `user_id`, `SUM(sale_price)`. Aggregates state with `HAVING COUNT(DISTINCT user_id) >= 5`. | Raw PII columns blocked. Returns HMAC pseudonyms. Data matches BQ ground truth. |
| **EV-02** | **Product Performance** | "Compare performance of product X and product Y this quarter." | Joins `products` and `order_items` scoped to user's authorized brands. Aggregates revenue and volume per product. | Non-authorized brand products excluded. Returns concise comparison report. |
| **EV-03** | **Time-based Metrics** | "What was our monthly churn rate for the last 6 months?" | Applies `SemanticGlossary` default (no purchase in 90 days, status='Complete'). Calculates monthly churn ratio. | Response includes mandatory "Applied Metric Definitions" footer detailing 90-day formula. |
| **EV-04** | **Database Structure** | "What data do we have available about customer logistics?" | Routing identifies intent as `db_schema`. Returns allowlisted schema descriptions without executing BQ query. | 0 BQ bytes billed. Explains available order tracking timestamps gracefully. |
| **EV-05** | **Multi-step Query / Report** | "Create a Q1 performance report with action items for Q2." | Multi-turn analysis executing sales, top products, and churn queries. Synthesizes executive summary + action items. | Latency $p95 \le 120\text{s}$. Completes within budget ($\le 5$ LLM calls). |
```

---

### 6.8. Agility: Persona & Tone Management (Design-Only HLD)

#### Architecture & Prompt Isolation
To allow non-developer administrators to alter agent tone without code redeployments:

```xml
<system_prompt>
  <system_instructions_immutable version="1.0">
    <!-- Immutably baked into application core -->
    You are an enterprise retail analytics assistant.
    SECURITY RULES: You MUST NOT leak PII. You MUST NOT execute DML/DDL.
    METRICS GOVERNANCE: Always state applied metric definitions in the footer.
  </system_instructions_immutable>

  <persona_configuration version="3.2" updated_by="CEO_Office" updated_at="2026-11-20">
    <!-- Dynamically loaded from AWS AppConfig / Database -->
    Tone: Executive, highly direct, action-oriented.
    Formatting: Emphasize core financial risks first. Use bullet points for takeaways.
  </persona_configuration>
</system_prompt>
```

#### Administration & Publish Workflow
1. **Non-Developer Management:** CEO office administrators update tone configurations through an Admin Web UI backed by `AWS AppConfig` or database store (`PersonaConfigStore`).
2. **Sandbox Testing:** Before publishing, administrators test draft persona configurations against a suite of 5 reference queries in a sandbox environment.
3. **Post-Render Invariant Check:** Automated validation verifies that the new tone does not suppress required metric disclosures or safety notices.
4. **Publishing & Versioning:** Every update generates an audited version record (`persona_version`). The current `persona_version` is logged in all trace records, enabling instant one-click rollbacks if analytical clarity drops.

---

## 7. Domain Glossary & Business Metrics Framework

The **`SemanticGlossary`** domain service loads default metric definitions from configuration (`configs/business_glossary.yaml`).

### Standard Default Metric Formulas
* **Churned Customer:** Any customer with zero completed purchases within the last 90 consecutive days:
  $$\text{Churned}(u) \iff \neg \exists \text{ order } o \text{ WHERE } o.user\_id = u.id \text{ AND } o.created\_at \ge (\text{CURRENT\_TIMESTAMP} - 90\text{ days}) \text{ AND } o.status = \text{'Complete'}$$
* **Net Revenue:** Total sale price of order items excluding cancelled or returned items:
  $$\text{Net Revenue} = \sum (\text{order\_items.sale\_price}) \quad \forall \text{ items WHERE } \text{status NOT IN ('Cancelled', 'Returned')}$$
* **Active Demographic Aggregation:**
  $$\text{Demographic Aggregation} \implies \text{HAVING COUNT(DISTINCT user\_id)} \ge k \quad (k=5)$$

### Deterministic Footer & Session Overrides
1. **Deterministic Response Footer:** Every generated analyst response automatically appends an **"Applied Metric Definitions"** footer in Python code before output rendering.
2. **Session Parameter Overrides:** Executives can override parameters in conversation (e.g., *"Calculate churn using 60 days instead of 90"*). The `SemanticGlossary` service parses the override, validates its range ($1 \le \text{days} \le 365$), and applies it for the current thread session without altering global defaults.

---

## 8. Data Mapping & BigQuery Schema Architecture

The assistant operates over four core tables within `bigquery-public-data.thelook_ecommerce`:

```mermaid
erDiagram
    users ||--o{ orders : "places (user_id)"
    users ||--o{ order_items : "owns (user_id)"
    orders ||--|{ order_items : "contains (order_id)"
    products ||--o{ order_items : "referenced in (product_id)"

    users {
        int64 id PK
        string first_name "PII - Blocked in AST"
        string last_name "PII - Blocked in AST"
        string email "PII - Blocked in AST"
        int64 age "Demographic - k-Anonymity Guard (k>=5)"
        string gender "Demographic - k-Anonymity Guard (k>=5)"
        string state "Demographic - k-Anonymity Guard (k>=5)"
        string street_address "PII - Blocked in AST"
        string postal_code "PII - Blocked in AST"
        string city "Demographic - k-Anonymity Guard (k>=5)"
        string country "Demographic - k-Anonymity Guard (k>=5)"
        float64 latitude "PII - Blocked in AST"
        float64 longitude "PII - Blocked in AST"
        string traffic_source "Demographic"
        timestamp created_at
    }

    orders {
        int64 order_id PK
        int64 user_id FK
        string status "Processing/Completed/Cancelled/Returned"
        string gender "Protected - Blocked without k-Anonymity Check"
        timestamp created_at
        timestamp returned_at
        timestamp shipped_at
        timestamp delivered_at
        int64 num_of_item "Multi-brand Rewritten to COUNT(*)"
    }

    order_items {
        int64 id PK
        int64 order_id FK
        int64 user_id FK
        int64 product_id FK
        int64 inventory_item_id
        string status
        timestamp created_at
        timestamp shipped_at
        timestamp delivered_at
        timestamp returned_at
        float64 sale_price
    }

    products {
        int64 id PK
        float64 cost
        string category
        string name
        string brand "RBAC Scope Dimension"
        float64 retail_price
        string department
        string sku
        int64 distribution_center_id
    }
```

---

## 9. Operational Trade-Offs & AWS Production Strategy

| Component / Layer | Prototype Profile (KISS / CLI) | Production Profile (AWS Cloud) | Architectural Rationale & Trade-Offs |
| :--- | :--- | :--- | :--- |
| **Inbound Gateway** | `Rich CLI` (Terminal Application) | `FastAPI` on `AWS ECS Fargate` + ALB | CLI provides zero-dependency local testing; ALB+FastAPI provides high-concurrency SSE streaming. |
| **Authentication & RBAC** | Local YAML (`users_rbac.yaml`) | `AWS Cognito` / OIDC Provider (JWT) | Production delegates token verification to corporate identity providers. |
| **Data Warehouse Access** | BigQuery Client (Service Account) | BigQuery Client via GCP Workload Identity | Direct access over public dataset utilizing `dry_run` byte guards and AST CTE rewriting. |
| **Persistence (Reports & State)** | `SQLite` (`retail_local.db`) + In-Memory TTL | `Amazon RDS PostgreSQL 16` (`pgvector`) + `Redis` | SQLite requires no container infrastructure; PostgreSQL + Redis support concurrent production scaling. |
| **Storage (Data Lake)** | Local Filesystem (`logs/`) | `Amazon S3 Data Lake` | S3 provides durable, versioned storage for Golden Trios and exported analytical reports. |
| **Observability & Tracing** | Local `JSONL` (`logs/agent_traces.jsonl`) | `AWS CloudWatch` + Self-Hosted `Langfuse` | Local file logging avoids third-party dependencies; CloudWatch/Langfuse provides enterprise distributed tracing. |
| **Configuration Engine** | Local YAML Files | `AWS AppConfig` + Admin UI | AppConfig enables dynamic tone and glossary updates without container redeployments. |

---

## 10. Implemented Project Structure and Extension Ports

The following is the repository layout implemented by the prototype. The named modules are the current extension seams; future charting, notification, and web-search capabilities should be introduced as application ports with outbound adapters rather than coupled to the domain.

```text
app/
├── domain/                           # Models, privacy policy, post-SQL guard, budget, glossary, SQL policy, PII masking
├── application/                      # Ports, ChatOrchestratorService, ReportService
├── adapters/
│   ├── inbound/                      # FastAPI HTTP API and Rich CLI client
│   └── outbound/                     # BigQuery, LLM, persistence, Redis/JSONL telemetry adapters
├── infrastructure/                   # Pydantic settings and composition root
└── main.py                           # FastAPI entrypoint
configs/
├── users_rbac.yaml                   # Local user-to-brand mapping
└── business_glossary.yaml            # Default retail metric definitions
logs/agent_traces.jsonl               # Local PII-redacted execution trace log (runtime-generated)
tests/                                # Prototype, privacy, high-stakes reports, observability, resilience, glossary tests
documentation/                        # Requirements and this HLD
docker-compose.yml                    # API, PostgreSQL, Redis, and optional CLI profile
pyproject.toml / uv.lock              # Python project and resolved dependencies
README.md                             # Human setup, example run, and quick start
AGENTS.md                             # AI-agent contribution contract
```
