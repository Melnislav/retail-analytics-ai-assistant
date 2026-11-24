# High-Level Design (HLD) — Retail Data Analysis Chat Assistant

## 1. Executive Summary & Architectural Principles

The **Retail Data Analysis Chat Assistant** is an enterprise-grade AI system designed for non-technical executive teams (e.g., OpsFleet executive managers, CEOs) to query complex retail transactional data in natural language. The system executes dynamic SQL queries over Google BigQuery's public dataset (`bigquery-public-data.thelook_ecommerce`), synthesizes business insights, manages saved report libraries, and provides structured executive summaries.

### Core Architectural Principles
* **Domain-Driven Design (DDD):** Domain logic, security governance, and analytical policies are decoupled from infrastructure adapters.
* **Hexagonal Architecture (Ports & Adapters):** Core business logic relies purely on abstract interfaces. Infrastructure components (LLM providers, relational databases, analytical data warehouses, CLI/Web adapters) can be swapped seamlessly without touching core domain services.
* **Single Responsibility Principle (SRP):** Each agent, service, and policy module owns exactly one operational domain (e.g., AST security, PII scrubbing, report lifecycle).
* **KISS & YAGNI:** The system architecture defines a dual-profile deployment model. The prototype runs natively as a zero-dependency CLI application using local SQLite and in-memory storage, while the production design defines a scalable containerized architecture.
* **DRY (Don't Repeat Yourself):** Shared policies (such as $k$-anonymity enforcement and Zero-PII scrubbing) are centralized in reusable policy engines.

---

## 2. System Architecture & Topology

The system uses a **Dual-Profile Deployment** approach:
1. **Prototype Profile (KISS / Zero-Docker):** Driven by an interactive `Rich CLI` client, utilizing `SQLite` for report storage, state check-pointing, and structured JSON tracing, alongside an in-memory TTL store for confirmation tokens.
2. **Production Profile (Cloud-Agnostic / Containerized):** Driven by a `FastAPI` REST/SSE router behind an API Gateway, backed by `PostgreSQL 16` (`pgvector` enabled), `Redis 7` for session cache/HITL tokens, and `OpenTelemetry` + `LangSmith`/`Langfuse` for distributed tracing.

```mermaid
flowchart TB
    subgraph Clients["Inbound Driving Channels"]
        CLI["Rich CLI Client (Interactive Terminal)"]
        WEB["Executive Web / Slack UI Client"]
    end

    subgraph InboundAdapters["Inbound Adapters"]
        API["FastAPI REST / SSE Router + OIDC/JWT RBAC Auth"]
    end

    subgraph Core["Application & Domain Core (Hexagonal / Pure Python)"]
        ORCH["ChatOrchestratorService<br/>(Request Budget Guard: <=5 LLM calls)"]
        GUARD["Intent & Injection Guardrail"]
        GLOSS["MetricsGlossaryService<br/>(Default Formulas + Session Overrides)"]
        POLICY["SQLPolicyEngine<br/>(SQLGlot AST + Brand RBAC + k-Anonymity)"]
        PII["PIIMasker<br/>(Column Scrubbing + Regex + HMAC Pseudonyms)"]
        REP["ReportService<br/>(2-Step SHA-256 Snapshot HITL)"]
    end

    subgraph Ports["Outbound Driven Adapters (Ports)"]
        LLM["LLMGatewayAdapter<br/>(Gemini 2.5 Flash -> OpenAI/Anthropic Fallback)"]
        BQ["AsyncBigQueryAdapter<br/>(Dry-Run Byte Guard + Read-Only Runner)"]
        STORE["PersistenceAdapter<br/>(Reports, Checkpoints, HITL Tokens, Golden RAG)"]
        OBS["ObservabilityAdapter<br/>(PII-Redacted Traces & Agent Metrics)"]
    end

    subgraph Infra["Infrastructure & External Services"]
        MODELS["Google Gemini API / Secondary LLM Provider"]
        BIGQUERY[("Google BigQuery<br/>thelook_ecommerce Raw Dataset")]
        DB_PROD[("Production: PostgreSQL 16 + pgvector / Redis 7")]
        DB_LOCAL[("Prototype: Local SQLite + InMemory TTL Store")]
        TRACE["Observability: Local JSONL / Langfuse / LangSmith"]
    end

    CLI --> Core
    WEB --> API --> Core

    ORCH --> GUARD
    ORCH --> GLOSS
    ORCH --> POLICY
    ORCH --> PII
    ORCH --> REP

    ORCH --> LLM --> MODELS
    ORCH --> BQ --> BIGQUERY
    ORCH --> STORE
    REP --> STORE
    ORCH --> OBS

    STORE -.-> DB_LOCAL
    STORE -.-> DB_PROD
    OBS -.-> TRACE
```

---

## 3. End-to-End Execution & Security Flow

The system processes incoming queries through a strict multi-tier execution pipeline where security rules, brand isolation, and demographic privacy policies are enforced deterministically in Python code before queries execute against BigQuery or data reaches the LLM.

```mermaid
flowchart TD
    REQ["User Query + Trusted SecurityContext"] --> GUARD{"Input Policy Guard<br/>(Prompt Injection / Off-topic / DDL)"}
    GUARD -->|Blocked| REFUSE["Safe Refusal Response + Audit Log"]
    GUARD -->|Allowed| ROUTE{"Intent Router"}

    ROUTE -->|Delete Reports| PREP["ReportService.prepare_deletion()<br/>(Owner Filter + SHA-256 Snapshot + TTL)"]
    PREP --> HITL["Show Target Report List & Request Confirmation"]
    HITL -->|Confirmed & Hash Matches| EXEC_DEL["Transactional Delete + Audit Record"]
    HITL -->|Rejected / Expired / Mismatched| ABORT_DEL["Abort Operation"]

    ROUTE -->|List / View Reports| READ_REP["ReportService.get_user_reports()<br/>(Strict owner_id Isolation)"]

    ROUTE -->|Analytical Query / Report| CTX["Inject Active Metrics Glossary<br/>+ Retrieve Golden Trios (Untrusted Data Tagged)"]
    CTX --> GEN_SQL["SQL Generator (LLM)"]
    GEN_SQL --> AST["SQLPolicyEngine (SQLGlot AST)<br/>• Single Read-Only SELECT<br/>• Allowlisted Tables Only<br/>• Block PII & orders.gender<br/>• Rewrite Brand CTE Isolation<br/>• Enforce HAVING COUNT(DISTINCT user) >= 5"]

    AST -->|Policy Violation| BLOCK_SQL["0 Retries: Immediate Block + Audit Event"]
    AST -->|Valid AST| DRY["BigQuery Dry-Run<br/>(Validate max_bytes_billed limit)"]
    DRY -->|Exceeds Budget| EXPENSIVE["Halt & Suggest Narrower Aggregation"]
    DRY -->|Within Budget| RUN_BQ["Execute Read-Only Query (with LIMIT)"]

    RUN_BQ -->|Syntax / Schema Error| RETRY_SYN{"Retry <= 2?"}
    RETRY_SYN -->|Yes| GEN_SQL
    RETRY_SYN -->|No| GRACEFUL_ERR["Graceful Error Message"]

    RUN_BQ -->|0 Rows Returned| RETRY_EMPTY{"Empty Check <= 1?"}
    RETRY_EMPTY -->|Yes: Widen Filter/Casing| GEN_SQL
    RETRY_EMPTY -->|No: Still Empty| CLARIFY["Ask Clarifying Question (No Hallucination)"]

    RUN_BQ -->|Rows Returned| MASK_IN["PIIMasker (Pre-LLM Boundary)<br/>Scrub Columns + Regex + HMAC-SHA256 user_id"]
    MASK_IN --> SYNTH["Analyst Synthesis + Mandatory Glossary Disclosure"]
    SYNTH --> MASK_OUT["PIIMasker (Post-LLM Output Guard)"]
    MASK_OUT -->|Explicit Save Requested| SAVE_REP["Save Report (Question + SQL + Answer)"]
    MASK_OUT --> RESP["Final Response + PII-Redacted Trace Log"]
```

---

## 4. Detailed Specification Across All 8 Requirements

### 4.1. Safety, PII Masking & Brand RBAC (Prototype + Production)

* **Security Principle:** LLMs generate analytical intents, but security, access control, and privacy boundaries are enforced deterministically in code.
* **Authentication & Identity (`SecurityContext`):**
  * In the CLI prototype, identity is loaded from a trusted YAML profile (`configs/users_rbac.yaml`) or CLI startup flag.
  * In Production, the API Gateway parses a signed OIDC/JWT token and constructs an immutable `SecurityContext(user_id, role, allowed_brands)`.
  * **Brand RBAC:** Access control is scoped strictly by `brand`. Brand managers query data associated with their assigned brands. Users with the `CEO` role have unrestricted access across all brands. If a non-CEO user has no assigned brands, access fails closed.
* **Dynamic SQL AST Policy Engine (`SQLPolicyEngine` via `SQLGlot`):**
  * Parses generated BigQuery SQL into an AST.
  * Permits exactly **one read-only SELECT statement** over four allowlisted tables: `orders`, `order_items`, `products`, `users`.
  * Rejects DML (`INSERT`, `UPDATE`, `DELETE`), DDL (`DROP`, `ALTER`), wildcard projections (`SELECT *`), system schema queries, and subqueries accessing external datasets.
  * **Multi-Brand Row/CTE Isolation:** Automatically injects non-recursive CTEs scoping products, order items, orders, and users to the user's authorized brands:
    ```sql
    WITH _scoped_products AS (
      SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`
      WHERE brand IN ('BrandA', 'BrandB')
    ),
    _scoped_order_items AS (
      SELECT oi.* FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
      INNER JOIN _scoped_products p ON oi.product_id = p.id
    ),
    _scoped_orders AS (
      SELECT o.* FROM `bigquery-public-data.thelook_ecommerce.orders` o
      WHERE EXISTS (SELECT 1 FROM _scoped_order_items soi WHERE soi.order_id = o.order_id)
    ),
    _scoped_users AS (
      SELECT u.* FROM `bigquery-public-data.thelook_ecommerce.users` u
      WHERE EXISTS (SELECT 1 FROM _scoped_order_items soi WHERE soi.user_id = u.id)
    )
    -- All table references in the original query are rewritten to point to these scoped CTEs
    ```
* **Demographic Privacy & $k$-Anonymity Protection:**
  * Demographics (`city`, `state`, `country`, `age`, `gender`, `traffic_source`) are prohibited as raw unaggregated fields.
  * Demographic attributes are permitted only as grouping dimensions when paired with a compulsory `HAVING COUNT(DISTINCT user_id) >= k` clause (default $k=5$, configurable).
  * **AST Leakage Prevention:** Rejects queries referencing `orders.gender` directly without $k$-anonymity validation to prevent schema bypasses around `users.gender`.
* **Zero-PII Perimeter (`PIIMasker`):**
  * Explicit PII columns (`first_name`, `last_name`, `email`, `street_address`, `postal_code`, `latitude`, `longitude`, `user_geom`) are blocked at the SQL AST level.
  * Before query results reach the LLM or cache, raw numeric `user_id` values are converted to stable deterministic pseudonyms:
    $$\text{Pseudonym} = \text{HMAC-SHA256}(\text{user\_id}, \text{SecretSalt})[:12]$$
  * High-entropy patterns (emails, phone numbers, addresses) are scrubbed via Regex before LLM prompt assembly and final output rendering.

---

### 4.2. High-Stakes Oversight: Destructive Operations (Prototype + Production)

* **Scope & Ownership:** Saved reports (containing `question`, `sql`, `answer`, `author_id`) can only be read or modified by their creator (`author_id == user_id`).
* **Two-Step Confirmation Workflow (Anti-TOCTOU):**
  1. **Preparation Phase:** Upon receiving a destructive command (e.g., *"Delete all reports mentioning Client X"*), `ReportService.prepare_deletion` resolves matching report IDs owned by `user_id`, takes an immutable snapshot, computes a SHA-256 payload hash, and generates a single-use confirmation token stored with a 600-second TTL:
     $$\text{PayloadHash} = \text{SHA256}(\text{SortedReportIDs} \mathbin{\Vert} \text{user\_id} \mathbin{\Vert} \text{nonce})$$
  2. **User Presentation:** The CLI/UI displays the exact list of target reports (ID, title, creation date, summary snippet) and prompts for explicit confirmation (`yes/no` or button click).
  3. **Execution Phase:** Upon confirmation, the service verifies that the token is active, matches `user_id`, and that the SHA-256 hash of current target IDs matches the snapshot hash. If valid, deletion executes within a single ACID transaction, generating a security audit log.

```mermaid
sequenceDiagram
    autonumber
    actor Exec as Executive (User)
    participant Agent as ChatOrchestrator
    participant RepSvc as ReportService
    participant Store as StateStore (SQLite/Redis)
    participant DB as Report Repository

    Exec->>Agent: "Delete all reports made in this conversation"
    Agent->>RepSvc: prepare_deletion(user_id, scope)
    RepSvc->>DB: Query reports WHERE author_id = user_id AND thread_id = X
    DB-->>RepSvc: Return [Report #12, Report #15]
    RepSvc->>RepSvc: Generate SHA-256 Hash of Snapshot IDs
    RepSvc->>Store: Save PendingConfirmation Token (TTL=600s, Hash, ReportIDs)
    RepSvc-->>Agent: Return Confirmation Payload
    Agent-->>Exec: Render Table of Reports [12, 15] + Request Confirmation (Y/N)

    alt User Approves (Y)
        Exec->>Agent: "Yes, confirm deletion"
        Agent->>RepSvc: execute_deletion(token_id, user_id)
        RepSvc->>Store: Fetch & Validate Token
        RepSvc->>DB: Re-verify ownership & SHA-256 Hash equality
        RepSvc->>DB: ACID DELETE FROM saved_reports WHERE id IN (12, 15)
        RepSvc->>Store: Invalidate Token
        RepSvc-->>Agent: Success Confirmation
        Agent-->>Exec: "Successfully deleted 2 reports [12, 15]."
    else User Rejects or Token Expired
        Exec->>Agent: "No / Cancel"
        Agent->>RepSvc: cancel_deletion(token_id)
        RepSvc->>Store: Delete Token
        Agent-->>Exec: "Deletion cancelled. No data was modified."
    end
```

---

### 4.3. Resilience & Graceful Error Handling (Prototype + Production)

* **SQL Self-Correction Loop:**
  * **Syntax & Schema Errors:** If BigQuery returns a syntax error or missing column fault, the error message is fed back to the LLM for targeted repair, capped at `MAX_SQL_RETRIES = 2`.
  * **Security Policy Rejections:** If `SQLPolicyEngine` rejects a generated query due to AST/RBAC violations, retries are capped at `0`. The query fails fast to prevent the model from probing security boundaries.
  * **Empty Result Handling ($0$ Rows):** If a valid query returns $0$ rows, the system attempts **1 bounded retry** to check case sensitivity (e.g., `'California'` vs `'CALIFORNIA'`) or widen restrictive date filters. If results remain empty, the agent returns a graceful explanation and asks a clarifying question without inventing data.
* **LLM Provider Fallback Strategy:**
  * Primary Provider: `Google Gemini 2.5 Flash` (via Google AI Studio / GCP Vertex AI).
  * Secondary Fallback: Managed `LLMGateway` switches to `OpenAI (GPT-4o)` or `Anthropic (Claude 3.5 Sonnet)` on HTTP 429 (Rate Limit), 5xx Server Errors, or API timeouts using `Runnable.with_fallbacks()`.
* **Cost & Byte Control Guards:**
  * Every SQL statement undergoes a BigQuery `dry_run` pre-execution check. If `total_bytes_processed` exceeds the configured cap (`BQ_MAX_BYTES_BILLED = 100 MB` for prototype, `1 GB` for production), execution halts and the user is prompted to request a narrower query window.
  * **Request Budget Guard:** Enforces a strict upper bound of **$\le 5$ LLM calls** and **$\le \$1.00$ compute cost** per user query turn.

---

### 4.4. Observability & Debugging (Prototype + Production)

* **Correlation Tracing:** Every request generates a unique `trace_id` propagated across all internal calls (Guardrails, Schema Resolution, AST Policy Validation, BigQuery execution, PII Redaction, Synthesis).
* **Tracked Metrics:**
  * **Agent Performance:** Query latency (p50, p95, p99), LLM time-to-first-token (TTFT), token usage (prompt/completion), cost per query.
  * **Data & Policy Metrics:** BigQuery bytes billed, AST policy violation counts, PII redaction counters, SQL self-correction attempt rates.
  * **System Reliability:** LLM provider fallback counts, $0$-row return frequencies, HITL deletion confirmation/cancellation rates.
* **PII-Scrubbed Logging:**
  * All trace events, prompts, and tool payloads pass through `PIIMasker` **prior to storage or export**. Raw customer names, emails, and exact coordinates never reach trace logs.
  * **Prototype:** Structured JSONL files written locally to `logs/agent_traces.jsonl` alongside SQLite event records.
  * **Production:** `OpenTelemetry` SDK exporting traces to `LangSmith` or `Langfuse` with structured JSON logs sent to centralized logging (e.g., CloudWatch / GCP Cloud Logging).

---

### 4.5. Hybrid Intelligence: "Golden Knowledge" Bucket (Design-Only HLD)

* **Concept:** Human analysts create curated "Trios" consisting of $(\text{Natural Language Question}, \text{Optimized SQL Query}, \text{Analyst Report})$.
* **Storage & Indexing Pipeline:**
  * Expert-approved Trios are stored in a versioned Knowledge Service backed by PostgreSQL (`pgvector`).
  * Hybrid Search: Combines Dense Vector Embeddings (semantic intent match) with Sparse Keyword Search (BM25 for exact business terms and brand names).
* **Query-Time Retrieval & Injection:**
  1. User query triggers semantic search over Golden Trios filtered by `allowed_brands`.
  2. Top-$K$ ($K=3$) relevant examples are retrieved and tagged as **`untrusted historical data`** inside the prompt context to prevent prompt injection.
  3. The LLM uses these examples to mirror domain logic, but the newly generated SQL must still pass the live user's `SQLPolicyEngine` AST firewall.
* **Continuous Bucket Lifecycle:**
  * Production queries enter the Golden Bucket candidate queue only after: (a) Explicit user approval, (b) Human analyst review, (c) Automated regression testing, and (d) Complete PII scrubbing.

```mermaid
flowchart LR
    Q["User Query"] --> EMB["Hybrid Search<br/>(Dense Vectors + BM25)"]
    EMB --> VECTOR[("pgvector Repository<br/>Golden Trios")]
    VECTOR --> FILTER["Filter by User's Allowed Brands"]
    FILTER --> TAG["Tag Content as 'Untrusted Data'"]
    TAG --> PROMPT["Inject as Top-3 Few-Shot Examples into LLM Prompt"]
    PROMPT --> AST["Pass Generated SQL to Live SQLPolicyEngine"]
```

---

### 4.6. Continuous Improvement: The Learning Loop (Design-Only HLD)

* **User-Level Adaptation (Preferences Profile):**
  * Tracks user presentation preferences (e.g., preference for tables vs bullet points, analytical depth, default formatting).
  * Preferences are stored in a explicit user profile store: `UserProfile(user_id, output_format, depth_level, auto_chart_enabled)`.
  * **Consent & Transparency:** Preferences update only through explicit user commands (e.g., *"Always show results in tables"*) or confirmed prompt prompts, avoiding silent drift. Users can inspect and reset their preference profiles.
* **System-Level Evaluation & Optimization Pipeline:**
  * Offline pipeline ingests negative feedback ($\text{thumbs down}$, query edits, failed SQL traces).
  * Failed queries are clustered to identify missing schema glossary terms, ambiguous column mappings, or LLM syntax traps.
  * Periodic prompt updates and fine-tuning datasets are evaluated offline before deployment. Real-time online model updating is explicitly excluded to prevent prompt injection poisoning.

---

### 4.7. Quality Assurance & Evaluation Framework (Design-Only HLD)

Pre-deployment validation combines deterministic security test suites, execution benchmarks, and automated judge metrics:

```mermaid
flowchart TD
    CI["CI/CD Deployment Gate"] --> SEC["1. Security & Adversarial Suite (pytest)"]
    CI --> BENCH["2. Execution Accuracy Benchmark (Golden Set)"]
    CI --> JUDGE["3. LLM-as-a-Judge Evaluation"]
    CI --> UX["4. UX & Reliability Performance Telemetry"]

    SEC --> SEC_CHK{"Passes 100% of PII, AST & RBAC Injection Tests?"}
    BENCH --> BENCH_CHK{"Exact Data Match >= 95% on Benchmark?"}
    JUDGE --> JUDGE_CHK{"Faithfulness & Glossary Score >= 4.5 / 5.0?"}
    UX --> UX_CHK{"Latency p95 <= 30s (Query) / 120s (Report)?"}

    SEC_CHK -->|Yes| APPROVE["Deploy to Production"]
    BENCH_CHK -->|Yes| APPROVE
    JUDGE_CHK -->|Yes| APPROVE
    UX_CHK -->|Yes| APPROVE

    SEC_CHK -->|No| REJECT["Block Release"]
    BENCH_CHK -->|No| REJECT
    JUDGE_CHK -->|No| REJECT
    UX_CHK -->|No| REJECT
```

* **Test Suites & Evaluation Criteria:**
  1. **Deterministic Security Suite (`pytest`):** Verifies PII redaction, brand CTE isolation, $k$-anonymity enforcement, `orders.gender` restriction, and HITL deletion snapshot token validation.
  2. **Execution Accuracy Benchmark:** Compares BigQuery DataFrame outputs produced by agent-generated SQL against ground-truth DataFrames over a reference evaluation dataset (evaluating data equivalence rather than exact SQL string equality).
  3. **LLM-as-a-Judge Evaluation:** Evaluates final synthesized reports for **Faithfulness** (no hallucinations beyond executed SQL results), **Completeness** (addresses user question), and **Glossary Compliance** (correctly applies metric definitions).
  4. **UX & SLA Performance Telemetry:**
     * Simple Query Latency Target: $p50 < 10\text{s}$, $p95 < 30\text{s}$.
     * Complex Report Latency Target: $p50 < 45\text{s}$, $p95 < 120\text{s}$.
     * Clarification Rate Target: $< 8\%$ of incoming user queries.

---

### 4.8. Agility: Persona & Tone Management (Design-Only HLD)

* **Architecture Isolation:** System Guardrails (security, AST rules, PII scrubbing, metrics formulas) are immutably baked into application code and system prompt headers. Persona instructions are injected into an isolated, configurable XML prompt block:
  ```xml
  <system_instructions>
    <!-- Immutable Security & Policy Rules -->
  </system_instructions>
  <persona_configuration version="2.1" updated_by="CEO_Office">
    <!-- Non-developer configurable tone, executive style, narrative preferences -->
    Tone: Concise, direct, executive-ready. Highlight key risks first.
  </persona_configuration>
  ```
* **Management & Governance:**
  * Non-developer administrators update tone configuration via a lightweight Admin UI or configuration API backed by a version-controlled store (`PersonaConfigStore`).
  * Every update creates a new version record with audit logs (`editor_id`, `timestamp`, `diff`).
  * Features a one-click rollback mechanism to revert to previous tone definitions instantly if analytical clarity is impacted.

---

## 5. Domain Glossary & Business Metrics Framework

To resolve metric ambiguities across disparate business units, the system introduces a **`MetricsGlossaryService`**. Default definitions are stored in a central configuration (`configs/business_glossary.yaml`) and injected into the LLM system prompt.

### Standard Default Metric Rules
* **Churned Customer:** Any customer with zero completed purchases within the last 90 consecutive days:
  $$\text{Churned}(u) \iff \neg \exists \text{ order } o \text{ where } o.user\_id = u.id \text{ AND } o.created\_at \ge (\text{CURRENT\_TIMESTAMP} - 90\text{ days}) \text{ AND } o.status = \text{'Complete'}$$
* **Net Revenue:** Total sales price of order items excluding cancelled or returned items:
  $$\text{Net Revenue} = \sum (\text{order\_items.sale\_price}) \quad \forall \text{ items WHERE } \text{status NOT IN ('Cancelled', 'Returned')}$$
* **Active User Grouping:** Demographic aggregations require a distinct count of users meeting the threshold:
  $$\text{Demographic Filter} \implies \text{HAVING COUNT(DISTINCT user\_id)} \ge k \quad (k=5)$$

### Dynamic Disclosure & User Overrides
1. **Mandatory Disclosure:** Every generated analyst response includes an **"Applied Metric Definitions"** footer detailing the formulas used during computation.
2. **Session Overrides:** Executives can override metric parameters in conversation (e.g., *"Calculate churn using 60 days instead of 90"*). The `MetricsGlossaryService` overrides the parameter for the duration of the current session thread without modifying system defaults.

---

## 6. Schema Architecture & Data Mapping

The assistant operates against four primary tables within `bigquery-public-data.thelook_ecommerce`:

```mermaid
erDiagram
    users ||--o{ orders : "places (user_id)"
    users ||--o{ order_items : "owns (user_id)"
    orders ||--|{ order_items : "contains (order_id)"
    products ||--o{ order_items : "referenced in (product_id)"

    users {
        int64 id PK
        string first_name "PII - Scrubbed"
        string last_name "PII - Scrubbed"
        string email "PII - Scrubbed"
        int64 age "Demographic - k-Anonymity Guard"
        string gender "Demographic - k-Anonymity Guard"
        string state "Demographic - k-Anonymity Guard"
        string street_address "PII - Scrubbed"
        string postal_code "PII - Scrubbed"
        string city "Demographic - k-Anonymity Guard"
        string country "Demographic - k-Anonymity Guard"
        float64 latitude "PII - Scrubbed"
        float64 longitude "PII - Scrubbed"
        string traffic_source "Demographic"
        timestamp created_at
    }

    orders {
        int64 order_id PK
        int64 user_id FK
        string status "Processing/Completed/Cancelled/Returned"
        string gender "Protected - Governed by Policy Engine"
        timestamp created_at
        timestamp returned_at
        timestamp shipped_at
        timestamp delivered_at
        int64 num_of_item
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
        string brand "RBAC Access Scope Dimension"
        float64 retail_price
        string department
        string sku
        int64 distribution_center_id
    }
```

---

## 7. Operational Trade-Offs & Production Strategy

| Component / Layer | Prototype Profile (KISS / CLI) | Production Profile (Cloud-Agnostic) | Architectural Rationale & Trade-Offs |
| :--- | :--- | :--- | :--- |
| **Interface / Inbound** | `Rich CLI` (Terminal UI) | `FastAPI` (REST / SSE) + Web UI | CLI provides instant zero-dependency testing; Web API supports enterprise streaming. |
| **Authentication & RBAC** | Local YAML config (`users_rbac.yaml`) | OIDC / OAuth2 JWT (Cognito, Keycloak, Auth0) | Production delegates token validation to standard Identity Providers. |
| **Data Warehouse Access** | BigQuery Client (Service Account) | BigQuery Client with Bounded Quotas | Direct execution over raw public BigQuery dataset using AST policy rewriting. |
| **Persistence (Reports & State)** | `SQLite` (`reports.db`, `checkpoints.db`) | `PostgreSQL 16` (`RDS` / `Cloud SQL`) | SQLite requires no container orchestration; PostgreSQL supports concurrent scaling. |
| **Cache & HITL Storage** | In-Memory TTL Store | `Redis 7` Cluster | Memory store simplifies local CLI runs; Redis supports multi-instance session caching. |
| **Vector Search (Golden Bucket)**| Disabled (Described in HLD) | `pgvector` / OpenSearch / Pinecone | Avoids vector index setup in prototype while defining semantic search in HLD. |
| **Observability & Tracing** | Local JSONL + SQLite Trace Log | OpenTelemetry + `LangSmith` / `Langfuse` | Local file logging avoids third-party dependencies; OpenTelemetry enables full distributed tracing. |

---

## 8. Prototype Project Structure

```text
retail-analytics-agent/
├── src/
│   ├── core/                        # Application & Domain Core (Framework Agnostic)
│   │   ├── domain/                  # Entities, Value Objects, Domain Policies
│   │   │   ├── security_context.py  # User Identity, Role & Allowed Brands
│   │   │   ├── metrics_glossary.py  # Default & Session Metric Definitions
│   │   │   └── report_entity.py     # Saved Report Domain Models & HITL Snapshot
│   │   ├── services/                # Application Services & Business Logic
│   │   │   ├── orchestrator.py      # Main Agent Loop & Request Budget Guard
│   │   │   ├── sql_policy_engine.py # AST Parser, Brand CTE Rewriter, k-Anonymity
│   │   │   ├── pii_masker.py        # Column Scrubbing, Regex, HMAC Pseudonymization
│   │   │   └── report_service.py    # Report CRUD & 2-Step Confirmation Engine
│   │   └── ports/                   # Abstract Interfaces (Hexagonal Ports)
│   │       ├── llm_port.py          # Abstract LLM Gateway
│   │       ├── database_port.py     # Abstract Query Execution Engine
│   │       ├── storage_port.py      # Abstract Persistence Gateway
│   │       └── tracer_port.py       # Abstract Observability Interface
│   │
│   ├── adapters/                    # Outbound Driven Adapters
│   │   ├── llm/
│   │   │   └── langchain_adapter.py # Gemini 2.5 Flash -> OpenAI Fallback Adapter
│   │   ├── database/
│   │   │   └── bigquery_adapter.py  # Async BigQuery Runner + Dry-Run Byte Guard
│   │   ├── storage/
│   │   │   ├── sqlite_adapter.py    # Local SQLite Storage (Reports & Checkpoints)
│   │   │   └── memory_ttl_store.py  # In-Memory HITL Confirmation Token Store
│   │   └── observability/
│   │       └── json_tracer.py       # PII-Redacted Structured JSON Tracing
│   │
│   └── inbound/                     # Inbound Driving Adapters
│       └── cli/
│           ├── app.py               # Rich CLI Application Loop
│           └── formatting.py        # Terminal Tables, Markdown & Color Renderers
│
├── configs/                         # System Configuration Files
│   ├── users_rbac.yaml              # Local User Identity & Brand Access Mappings
│   ├── business_glossary.yaml       # Default Churn, Revenue & Metric Definitions
│   └── persona_config.yaml          # CEO Tone & Response Style Rules
│
├── tests/                           # Security, Integration & Unit Test Suites
│   ├── test_sql_policy_engine.py    # AST Security, Brand Isolation & k-Anonymity Tests
│   ├── test_pii_masker.py           # Zero-PII Scrubbing & HMAC Pseudonym Tests
│   ├── test_report_service.py       # Deletion Confirmation Snapshot Tests
│   └── test_resilience.py           # Error Self-Correction & Fallback Tests
│
├── docs/                            # Architectural Documentation
│   └── hld_document.md              # Production High-Level Design (This Document)
│
├── requirements.txt                 # Python Dependencies
├── .env.example                     # Environment Configuration Template
└── README.md                        # Setup & Execution Instructions
```
