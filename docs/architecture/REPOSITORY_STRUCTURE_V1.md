# Repository Structure Specification - V1

> **Document Type:** Modular Monolith Codebase Architecture<br>
> **Status:** Approved Baseline<br>
> **Version:** 1.0<br>
> **Target Phase:** Phase 1 (Architecture & System Design)<br>
> **Parent Specifications:** [`SYSTEM_ARCHITECTURE_V1.md`](./SYSTEM_ARCHITECTURE_V1.md) | [`TECH_STACK_V1.md`](./TECH_STACK_V1.md) | [`DATABASE_SCHEMA_V1.md`](./DATABASE_SCHEMA_V1.md)<br>
> **Rules & Constraints:** [`../../rules/JOB_FILTERS_V1.md`](../../rules/JOB_FILTERS_V1.md) | [`../../rules/DEDUPLICATION_V1.md`](../../rules/DEDUPLICATION_V1.md) | [`../../rules/VERIFICATION_V1.md`](../../rules/VERIFICATION_V1.md)

---

## 1. Architectural Philosophy: The Modular Monolith

The **AI Job Intelligence** repository is engineered as a **Modular Monolith**. Rather than fragmenting the early codebase into complex, premature distributed microservices with separate message buses and deployment pipelines, the system organizes discrete domain modules within a single cohesive Python repository.

### Core Architectural Guarantees
1. **Clear Module Boundaries:** Each subdirectory under `app/` represents an isolated functional domain with strict inputs, outputs, and single-direction dependencies.
2. **Zero In-Process Coupling:** Modules communicate through strongly typed domain models and direct method invocations rather than arbitrary global states or cross-module database side effects.
3. **Frictionless Testing:** Domain logic (normalization, deduplication, rule filtering) is decoupled from network I/O and external services, allowing ultra-fast in-memory unit tests with mock fixtures.
4. **Implementation Independence:** Swapping an ingestion collector or an AI provider requires zero modification to upstream business rules or downstream persistence layers.

### 1.1 Explicit Architectural Decision: Resolution of `src/` versus `app/`
- **Context & Source of Truth:** The initial repository initialization created a placeholder `src/.gitkeep`. Subsequent Phase 1 architectural specifications ([`SYSTEM_ARCHITECTURE_V1.md`](./SYSTEM_ARCHITECTURE_V1.md), [`DATABASE_SCHEMA_V1.md`](./DATABASE_SCHEMA_V1.md)) specified top-level modules organized under `app/`.
- **Decision:** Explicitly adopt the **`app/`** directory structure as the canonical top-level package root.
- **Rationale for `app/`:**
  1. *Clean Module Imports:* Python modules cleanly import via `from app.orchestration import ...` and `from app.persistence import ...` without redundant nesting (`from src.app...` or `from src...`).
  2. *Standard ASGI Server Conventions:* FastAPI / Uvicorn natively maps to `uvicorn app.api.main:app --reload` at repository root.
  3. *Zero Redundant Nesting:* An extra `src/` wrapper adds zero namespace or dependency isolation benefit for an in-process modular monolith and only introduces path friction.
  4. *Retiring `src/`:* The legacy placeholder `src/` directory (containing only `src/.gitkeep`) is formally deprecated. When scaffolding Phase 2 application code, the `src/` placeholder will be safely removed in favor of `app/`.

---

## 2. Approved Directory Tree

```text
AI-Job-Intelligence/
├── app/
│   ├── api/                    # REST API endpoints & schemas (FastAPI)
│   ├── orchestration/          # Lightweight pipeline coordinators (API & scheduler entrypoint)
│   ├── collectors/             # External ingestion harvesters (ATS & Web)
│   │   ├── base/               # Abstract base collector & HTTP client
│   │   ├── greenhouse/         # Greenhouse public API collector
│   │   ├── lever/              # Lever public API collector
│   │   ├── ashby/              # Ashby public API collector
│   │   └── ...                 # Future collectors (Workable, custom DOM)
│   ├── discovery/              # Company registry & career page discovery
│   ├── normalization/          # Technology-agnostic payload sanitization
│   ├── deduplication/          # 4-stage identity & duplicate resolution
│   ├── verification/           # 8-dimension authenticity & confidence engine
│   ├── rules/                  # Deterministic business rules & eligibility
│   ├── ai/                     # Provider-agnostic LLM subsystem
│   │   ├── gateway/            # Router, circuit breaker, failover logic
│   │   ├── providers/          # Modular adapters (Gemini, Groq, Ollama)
│   │   └── cache/              # SHA256 composite content cache
│   ├── persistence/            # PostgreSQL ORM models & data access
│   ├── scheduling/             # In-process APScheduler cron triggers
│   └── config/                 # Pydantic environment & app settings
├── tests/
│   ├── unit/                   # Fast, zero-network unit tests
│   ├── integration/            # Database transactions & pipeline tests
│   ├── fixtures/               # Recorded JSON payloads & HTML mock responses
│   └── conftest.py             # Global pytest fixtures & database setup
├── migrations/                 # Alembic database migration revisions
├── scripts/                    # Development utilities, seeds & CLI helpers
├── docs/                       # Specifications, architecture & contract docs
├── rules/                      # Authoritative frozen business & filter rules
├── pyproject.toml              # Unified dependency & build specification
├── uv.lock                     # Deterministic, reproducible lockfile
├── .env.example                # Sample environment configuration template
├── .gitignore                  # Git tracking exclusion definitions
├── docker-compose.yml          # Multi-container orchestration (App + Postgres)
└── README.md                   # Project overview & quickstart documentation
```

---

## 3. Subsystem Responsibilities & Architectural Boundaries

### 3.1 `app/api/` (API Presentation Layer)
- **Responsibility:** Exposes RESTful HTTP endpoints for querying the curated job feed, inspecting audit trails, managing the company registry, and triggering manual ingestion runs.
- **What Belongs Here:**
  - FastAPI routers (`jobs.py`, `companies.py`, `runs.py`, `health.py`).
  - Request parameter validation and query filter parsers.
  - Outgoing JSON response DTOs (Pydantic schemas).
  - Dependency injection fixtures for authentication and database sessions.
- **What Must NOT Belong Here:**
  - Raw SQL queries or direct database connection manipulation.
  - Scraping, crawling, or network harvesting operations.
  - Business rule filtering or deduplication algorithms.
- **Key Boundary:** The API layer delegates pipeline coordination to `app/orchestration/` and reads feed data via `app/persistence/`; core business logic modules never import `app/api/`.

---

### 3.2 `app/orchestration/` (Lightweight Application Orchestration Layer)
- **Responsibility:** In-process application pipeline coordinator. Serves as the single orchestration layer coordinating end-to-end data workflows for both manual API triggers and scheduled cron jobs (`API / Scheduler -> Orchestration -> Pipeline`), preventing duplicated coordination logic across entry points.
- **Design Rationale (`app/orchestration/` vs. `app/services/`):**
  We explicitly select `app/orchestration/` rather than `app/services/`. In modular monolithic architectures, `services/` frequently becomes a vague catch-all dumping ground where domain logic, database queries, web helpers, and external scraping code bleed together. The explicit label `orchestration` enforces a clear, disciplined boundary: this module contains **zero business rules**, **zero scraping logic**, and **zero direct SQL queries**. It exists solely to sequence and direct data flow between domain modules.
- **What Belongs Here:**
  - `pipeline.py`: Orchestrates standard ingestion workflow:
    `collectors / discovery` $\to$ `normalization` $\to$ `deduplication` $\to$ `verification + rules` $\to$ `AI when needed` $\to$ `persistence`.
  - `discovery.py`: Orchestrates discovery sweeps and registry updates.
  - `sweeps.py`: Orchestrates scheduled maintenance sweeps (e.g., evaluating `posted_at` > 5 days for expiration, link re-verifications).
- **What Must NOT Belong Here:**
  - Network scraping or parsing code (delegated to `app/collectors/`).
  - Filtering criteria, salary evaluations, or business rules (delegated to `app/rules/`).
  - Raw SQL queries or ORM models (delegated to `app/persistence/`).
  - HTTP request parsing or presentation formatting (delegated to `app/api/`).
- **Key Boundary & Centralized Workflow:** Both `app/api/` (manual user/admin triggers) and `app/scheduling/` (APScheduler cron jobs) **must** invoke the exact same orchestration workflows in `app/orchestration/` (`API / Scheduler -> Orchestration -> Pipeline`). Neither API endpoints nor schedulers may bypass orchestration to interact with domain engines or persistence directly, preventing duplicated workflow coordination logic.

---

### 3.3 `app/collectors/` (Data Ingestion Layer)
- **Responsibility:** Interfacing with external platforms, direct ATS endpoints, and company career pages to capture un-mutated external job listings.
- **What Belongs Here:**
  - `base/collector.py`: Abstract `BaseCollector` defining standard `collect_company()` lifecycle.
  - `base/http.py`: Politeness-gated `httpx.AsyncClient` wrapper with rate-limiting, jitter, and user-agent rotation.
  - Source-specific collectors (`greenhouse/`, `lever/`, `ashby/`, etc.) implementing API parsing or Playwright browser rendering.
  - Conversion of HTTP responses into raw ingestion records for `raw_jobs` (assigning native platform IDs or deterministic `"gen_<sha256>"` identifiers and explicit `source_job_id_type` classification without modifying immutable `raw_payload`).
- **What Must NOT Belong Here:**
  - **Zero Filtering:** Collectors must **NEVER** discard an opening because of title, experience, or salary. All discovered listings are captured.
  - Deduplication matching across external companies.
  - AI prompt invocations or semantic evaluation.
- **Key Boundary:** **Collectors must not contain filtering logic.** Their sole mandate is high-fidelity capture of raw data into `raw_jobs`.

---

### 3.4 `app/discovery/` (Company & Endpoint Discovery)
- **Responsibility:** Proactively identifying target companies, detecting their career pages, and discovering underlying ATS endpoints (Greenhouse tokens, Lever slugs, Ashby boards).
- **What Belongs Here:**
  - Discovery workers querying public indices, Common Crawl CDX APIs, and corporate DNS records.
  - ATS signature detection heuristics (analyzing HTML redirect targets and script tags for ATS vendor signatures).
  - Company registry populators and health validators updating `company_sources`.
- **What Must NOT Belong Here:**
  - Individual job listing harvesting (delegated to `app/collectors/`).
  - Job qualification filtering or scoring.
- **Key Boundary:** Discovery produces company and endpoint metadata (`companies`, `company_sources`); it does not process job vacancies.

---

### 3.5 `app/normalization/` (Data Standardization Layer)
- **Responsibility:** Transforming disparate, unstructured source data into strongly typed, standardized canonical attributes.
- **What Belongs Here:**
  - Title normalization (lowercasing, stripping seniority prefixes, mapping variants like "Backend Dev" -> "Backend Developer").
  - Location normalization (extracting canonical Indian tech hubs: Mumbai, Pune, Bengaluru, Hyderabad; detecting remote clauses).
  - Experience extraction heuristics (parsing strings like "0-2 years", "Fresher", "1 to 3 yrs" into numeric min/max bounds).
  - Compensation parsing (standardizing currency to INR, converting monthly/hourly compensation to annual CTC).
  - Text sanitation (stripping HTML markup, formatting bullet points).
- **What Must NOT Belong Here:**
  - Source-specific HTTP handling or network requests.
  - Pass/fail eligibility decisions.
- **Key Boundary:** **Normalization must not depend on a particular ATS.** It takes generic raw payload dictionaries and outputs normalized Pydantic models.

---

### 3.6 `app/deduplication/` (Identity & Duplicate Resolution Layer)
- **Responsibility:** Executing the 4-stage deduplication hierarchy defined in [`DEDUPLICATION_V1.md`](../../rules/DEDUPLICATION_V1.md) to merge cross-source postings into a single canonical `Job`.
- **What Belongs Here:**
  - Stage 1: Exact source platform and external job ID matching (evaluating native platform IDs or deterministic `gen_<sha256>` IDs).
  - Stage 2: Direct canonical application URL / ATS endpoint matching.
  - Stage 3: Deterministic composite matching (`company_id` + `normalized_title` + `normalized_location`), honoring company stub isolation for generic employer identities.
  - Stage 4: Text signature overlap evaluation using token-based similarity (MinHash / Jaccard similarity) executed in memory.
  - Creating and linking `job_source_references` to canonical jobs (persisting `source_job_id` and explicit `source_job_id_type`).
- **What Must NOT Belong Here:**
  - Deciding job relevance, priority, or operational eligibility.
  - Network scraping or data fetching.
- **Key Boundary:** Deduplication resolves identity and provenance; it does not evaluate whether a job is desirable.

---

### 3.7 `app/verification/` (Authenticity & Auditability Layer)
- **Responsibility:** Evaluating the eight independent verification dimensions defined in [`VERIFICATION_V1.md`](../../rules/VERIFICATION_V1.md) and computing composite confidence scores.
- **What Belongs Here:**
  - Dimension evaluators: source validity, JD existence (>100 chars), application link reachability, company legitimacy, location consistency, experience consistency, salary statedness, posting date plausibility (evaluating publication date plausibility; missing/invalid `posted_at` falls back provisionally to `collected_at` per frozen verification rule and routes to `review_reason = POSTED_DATE_SUSPICIOUS`, while `posted_at` remains NULL in the schema; `collected_at` alone serves only as ingestion metadata and provisional fallback, and never establishes active freshness).
  - Composite confidence calculation formula:
    $$\text{Confidence Score} = \sum (\text{Dimension Score} \times \text{Weight})$$
  - Construction of detailed machine-readable diagnostic payloads for `verification_records.audit_trail`.
- **What Must NOT Belong Here:**
  - Automatic rejection of postings due to unstated or ambiguous secondary fields (salary, experience).
  - Database persistence transactions (delegated to persistence repositories).
- **Key Boundary:** Verification evaluates authenticity and completeness; it strictly honors the maximum recall principle by routing ambiguity to `REVIEW`.

---

### 3.8 `app/rules/` (Deterministic Business Rules Engine)
- **Responsibility:** Enforcing the frozen V1 filtering criteria defined in [`JOB_FILTERS_V1.md`](../../rules/JOB_FILTERS_V1.md).
- **What Belongs Here:**
  - Role classification and priority tier assignment (`P1`, `P2`, `P3`, `NOT_RELEVANT`).
  - Experience ladder validation (Keep: Fresher, 0–1, 0–2, 1–2; Review: 1–3; Reject: $\ge 2$ min, 2–3, 2–4, 3+, Senior/Lead).
  - Location tier enforcement (P1: Mumbai/Pune; P2: Bengaluru/Hyderabad; Review: Remote with HQ outside hubs; Missing/Ambiguous location: retained without dummy values, marked unverified, and routed to REVIEW with `review_reason = LOCATION_AMBIGUOUS`).
  - Salary threshold checks (Mumbai / Navi Mumbai / Thane: no strict minimum; Pune: minimum ₹3.6 LPA; Bengaluru: minimum ₹5 LPA; Hyderabad: minimum ₹5 LPA; unstated/unknown salary: do not automatically reject; route according to the frozen review policy with `salary_verified = false` and `review_reason = SALARY_UNKNOWN`).
  - Freshness window sweeps (Original publication date `posted_at` governs 0–24h, 1–3d, 3–5d active feed; >5d `EXPIRED` with `rejection_reason = JOB_TOO_OLD`. Late-discovered postings with verified `posted_at` > 5 days remain `EXPIRED` rather than active; jobs with missing or invalid `posted_at` retain `posted_at = NULL` and remain in `REVIEW` with `review_reason = POSTED_DATE_SUSPICIOUS`, with `collected_at` serving only as ingestion metadata and provisional verification fallback that cannot alone qualify a job as fresh).
  - Assigning `eligibility_status`, `rejection_reason`, and `review_reason`.
- **What Must NOT Belong Here:**
  - Network I/O, web scraping, or ATS communication.
  - Database queries or ORM session management.
- **Key Boundary:** **Rules must not contain source-specific collection logic.** The rules engine is 100% deterministic, operating purely on normalized in-memory data models.

---

### 3.9 `app/ai/` (Provider-Agnostic LLM Subsystem)
- **Responsibility:** Resolving semantic ambiguity in job descriptions (e.g., classifying unstated roles or parsing complex responsibilities) while strictly controlling token consumption and costs.
- **Sub-Packages:**
  - `gateway/`: Abstract router managing provider failover, rate-limit tracking (RPM/RPD), circuit breaking, and timeout cascades.
  - `providers/`: Modular provider adapters (`gemini_adapter.py`, `groq_adapter.py`, `openrouter_adapter.py`, `ollama_adapter.py`) implementing the unified `BaseLLMAdapter`.
  - `cache/`: Composite content-hash caching (`SHA256(JD + prompt_v + model)`) to ensure zero redundant external API invocations.
- **What Belongs Here:**
  - Prompt templates with version identifiers (`prompts.py`).
  - Pydantic response models enforcing strict structured JSON extractions.
  - Client retry policies and local model fallback triggering.
- **What Must NOT Belong Here:**
  - Direct SQL queries or PostgreSQL table manipulation.
  - High-level business rule definitions (e.g., determining whether 2 years is junior).
- **Key Boundary:** **AI providers must not access PostgreSQL directly.** The AI subsystem receives sanitized text and returns validated schemas; persistence is handled by calling orchestration workflows.

---

### 3.10 `app/persistence/` (Data Access & Relational Storage)
- **Responsibility:** Managing relational mappings, database transactions, connection pooling, and CRUD queries against PostgreSQL.
- **What Belongs Here:**
  - `models/`: SQLAlchemy 2.0 declarative database models corresponding to the entities defined in [`DATABASE_SCHEMA_V1.md`](./DATABASE_SCHEMA_V1.md).
  - `repositories/`: Repository classes encapsulating SQL queries (`job_repository.py`, `company_repository.py`, `raw_job_repository.py`).
  - `database.py`: Async engine configuration, sessionmaker factories (`asyncpg`), and connection pooling settings.
- **What Must NOT Belong Here:**
  - Ingestion, scraping, or web crawling code.
  - Business rule filtering decisions or eligibility logic.
  - Presentation formatting or HTTP response building.
- **Key Boundary:** **Persistence must not contain business-rule decisions.** It stores and retrieves state without altering domain evaluation outcomes.

---

### 3.11 `app/scheduling/` (Job Trigger & Schedule Dispatcher)
- **Responsibility:** Coordinating periodic background trigger dispatches and cron schedules using in-process APScheduler. Schedulers trigger workflows by invoking `app/orchestration/` (`Scheduler -> Orchestration -> Pipeline`), preventing duplicated coordination logic across entry points.
- **What Belongs Here:**
  - APScheduler scheduler lifecycle configuration (start/shutdown within FastAPI app lifecycle).
  - Scheduled job definitions:
    - High-frequency ATS polling (every 3 hours) $\to$ dispatches to `orchestration.pipeline.run_ats_harvest()`.
    - Daily career page crawl (daily at 02:00 IST) $\to$ dispatches to `orchestration.pipeline.run_career_crawl()`.
    - Daily freshness & expiration sweep (daily at 04:00 IST) $\to$ dispatches to `orchestration.sweeps.run_freshness_sweep()` (evaluating original publication date `posted_at` > 5 days).
  - Run telemetry logging into `discovery_runs` and `collection_runs`.
- **What Must NOT Belong Here:**
  - Implementation of scraping or parsing logic.
  - Pipeline sequencing logic (delegated entirely to `app/orchestration/`).
  - Distributed message brokers (Celery, Redis, RabbitMQ are explicitly rejected).
- **Key Boundary:** **Scheduling must trigger orchestration, not implement pipeline logic.** It simply dispatches trigger events to `app/orchestration/` at configured intervals.

---

### 3.12 `app/config/` (Configuration & Environment)
- **Responsibility:** Centralizing application settings, secrets, and environment variable loading.
- **What Belongs Here:**
  - `settings.py`: Pydantic `BaseSettings` defining database connection strings, active AI provider priority orders, log levels, and rate limits.
- **What Must NOT Belong Here:**
  - Hardcoded API keys, passwords, or credentials.
  - Business logic or dynamic state.

---

### 3.13 `tests/` (Testing Suite)
- **Responsibility:** Comprehensive verification of all system components through unit, integration, and fixture-based tests.
- **Subdirectories:**
  - `unit/`: Fast, isolated tests for normalization functions, rule engine decisions across experience bands, deduplication matchers, and Pydantic schemas. Zero network access.
  - `integration/`: End-to-end database transactions, repository queries, Alembic migration verification, and pipeline cascades.
  - `fixtures/`: Recorded, static JSON payloads from Greenhouse, Lever, and Ashby, and mock HTML responses for deterministic collector testing.
- **Key Boundary:** **Tests should mirror the application boundaries.** Unit tests verify domain modules in isolation without spawning databases or making network requests.

---

## 4. Architectural Dependency Flow

To prevent circular dependencies, spaghetti architecture, and duplicated coordination logic between the REST API and background schedulers, all workflows flow through the lightweight orchestration layer:

```text
┌───────────────────────────┐         ┌───────────────────────────┐
│       API (app/api)       │         │ Scheduler (app/scheduling)│
└─────────────┬─────────────┘         └─────────────┬─────────────┘
              │                                     │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │          app/orchestration          │
              │  (Lightweight Pipeline Coordinator) │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │       collectors / discovery        │
              │  (app/collectors / app/discovery)   │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │          normalization              │
              │       (app/normalization)           │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │          deduplication              │
              │       (app/deduplication)           │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │       verification + rules          │
              │    (app/verification / app/rules)   │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼ (When ambiguous)
              ┌─────────────────────────────────────┐
              │          AI when needed             │
              │             (app/ai)                │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │           persistence               │
              │        (app/persistence)            │
              └─────────────────────────────────────┘
```

### Strict Module Boundaries & Dependency Rules
- **Collectors do not filter:** `app/collectors` $\longrightarrow$ `app/rules` (**FORBIDDEN**). Collectors have a zero-filtering mandate; their sole job is high-fidelity capture of raw data into `raw_jobs`.
- **Rules do not scrape:** `app/rules` $\longrightarrow$ `app/collectors` (**FORBIDDEN**). The rules engine is 100% deterministic and operates purely on normalized in-memory models.
- **AI providers do not access persistence directly:** `app/ai/providers` $\longrightarrow$ `app/persistence` (**FORBIDDEN**). The AI subsystem receives sanitized text inputs and returns validated Pydantic schemas; database persistence is managed by orchestration workflows.
- **Persistence does not make business decisions:** `app/persistence` $\longrightarrow$ `app/rules` (**FORBIDDEN**). Repositories store and retrieve state without altering domain evaluation outcomes.
- **Normalization is ATS-agnostic:** `app/normalization` $\longrightarrow$ `app/collectors` (**FORBIDDEN**). Normalization parses generic raw payload dictionaries without source-specific branching.
- **Orchestration does not contain domain rules:** `app/orchestration` coordinates pipeline execution order; it does not implement filtering logic, scraping regexes, or ORM queries.
- **Scheduler and API do not duplicate pipeline logic:** Both `app/api` and `app/scheduling` dispatch execution through `app/orchestration` (`Scheduler -> Orchestration -> Pipeline` and `API -> Orchestration -> Pipeline`), preventing duplicated coordination logic.
