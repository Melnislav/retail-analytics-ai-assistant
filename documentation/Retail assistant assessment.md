# Retail Assistant Assessment

## Goal

Design a production-grade High-Level Design (HLD) and build a functional CLI prototype of a retail data-analysis chat assistant for non-technical executives. The assistant must answer retail-performance questions in natural language, generate actionable reports on request, and remain extensible for capabilities such as chart generation, report distribution, and external-trend research.

The assignment requires a gap analysis from requirements to solution. The design must follow DDD, hexagonal architecture, SRP, KISS, YAGNI, and DRY.

## Scope

### Prototype scope

The working prototype must implement:

1. Safety and PII masking.
2. High-stakes oversight for destructive saved-report operations.
3. Resilience and graceful error handling.
4. Observability.
5. A CLI for chat interactions.

### Design-only scope

The HLD and technical explanation must describe production-grade approaches for:

1. Hybrid intelligence through a Golden Knowledge Bucket.
2. Continuous improvement at user and system levels.
3. Quality assurance and evaluation.
4. Persona and tone management without redeployment.

## Functional Requirements

1. **Hybrid intelligence.** Use historical, human-created trios of question, SQL, and analyst report to inform new queries. Define ingestion, curation, update, and query-time retrieval of the Golden Knowledge Bucket.
2. **Safety and PII masking.** Answer analytical requests only; resist malicious input; never expose PII in prompts to the LLM or in output. Enforce brand-only access: managers see their assigned brands, while CEOs see all brands. Customer and order analysis must derive only from purchases of products in scope.
3. **High-stakes oversight.** Save reports only on explicit user request. Reports contain the question, SQL, and answer, and only their author may access them. Before deleting any reports, show the exact target set and require explicit confirmation.
4. **Continuous improvement.** Persist user presentation preferences, such as tables versus bullets and desired analysis depth. Improve the system from feedback and failures without allowing runtime prompt-poisoning or unreviewed online learning.
5. **Resilience.** Detect SQL syntax errors and empty result sets, attempt bounded corrective retries, handle provider failures gracefully, and do not crash the interface or inflate cost.
6. **Quality assurance.** Define evaluation before deployment for policy adherence, intent satisfaction, report faithfulness, data accuracy, user experience, and latency.
7. **Observability.** Define agent-level metrics, trace correlation, and privacy-safe deep-dive debugging that explains messages, failures, and costs.
8. **Persona management.** Let non-developers update report tone and instructions without redeploying the service, while preserving immutable safety and disclosure rules.

## Data, Privacy, and Access Constraints

- Use the read-only `bigquery-public-data.thelook_ecommerce` dataset and its `orders`, `order_items`, `products`, and `users` tables. Generate and execute dynamic SQL; do not rely only on pre-governed metrics.
- Do not expose first or last name, email, street address, postal code, or exact coordinates. PII must not reach the LLM.
- City, state, country, age, gender, and traffic source may appear only as aggregates with at least five distinct users by default. A row must not associate a person or pseudonymous identifier with demographic attributes.
- Stable pseudonymous customer identifiers or internal `user_id` values are permitted when they are not linked to demographic attributes.
- The manager-to-brand mapping is external to the dataset. Specify an appropriate local configuration or access-table approach for the prototype and JWT claims for production.
- For multi-brand orders, calculate a manager's revenue, order counts, customer spend, churn, and rankings from authorized-brand purchases only.
- Define a lightweight, configurable glossary for ambiguous metrics. Use sensible defaults, disclose the applied definition in answers, and permit session-level overrides.

## Operating Constraints

- Prefer AWS for production infrastructure; use a current Google Gemini model for testing, with compatible support for the expected provider configuration.
- Support tens to a few hundred executives, approximately 100 active managers per day, ten questions and one saved report per manager per day, and daily ingestion of about one million rows.
- Target 10–30 seconds for simple questions and 1–2 minutes for long reports.
- Keep the configurable budget ceiling at or below $1 per question and typically use two to five LLM calls per query.
- Local tracing through SQLite or JSON logs is acceptable for the prototype. The HLD must describe the intended production observability tooling and its trade-offs.

## Deliverables and Evaluation

1. An HLD with architecture diagrams, services, compute, storage, communications, technology choices, data flows, failure handling, and a solution for every requirement.
2. A detailed technical explanation covering framework and model rationale, setup, an example run, and production trade-offs.
3. Runnable prototype code with the four in-scope capabilities and a CLI interface.
4. Setup instructions sufficient to run the solution on another machine.
5. An explanation of the selected framework and relevant experience level.

Evaluation prioritizes system design, technical explanation, and an elegant prototype that satisfies the prototype-scope requirements.
