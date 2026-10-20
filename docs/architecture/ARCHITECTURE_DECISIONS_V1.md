# Architecture Decision Records (ADRs) - V1

> **Document Type:** Architectural Decision Records (ADRs)  
> **Status:** Approved Baseline  
> **Version:** 1.0  
> **Target Phase:** Phase 1 (Architecture & System Design)  
> **Parent Architecture:** [`SYSTEM_ARCHITECTURE_V1.md`](./SYSTEM_ARCHITECTURE_V1.md)  
> **Tech Stack Specification:** [`TECH_STACK_V1.md`](./TECH_STACK_V1.md)

---

## Index of Decisions

- [ADR-001: Modular Monolith Architecture over Microservices](#adr-001-modular-monolith-architecture-over-microservices)
- [ADR-002: PostgreSQL as Unified Relational and Raw Payload Store](#adr-002-postgresql-as-unified-relational-and-raw-payload-store)
- [ADR-003: Public-Source-First and API-First Ingestion Hierarchy](#adr-003-public-source-first-and-api-first-ingestion-hierarchy)
- [ADR-004: ATS as Primary Ingestion Backbone with Multi-Source Expansion](#adr-004-ats-as-primary-ingestion-backbone-with-multi-source-expansion)
- [ADR-005: Company-Centric Discovery via Structured Company Registry](#adr-005-company-centric-discovery-via-structured-company-registry)
- [ADR-006: Deterministic-First Filtering Preceding LLM Invocations](#adr-006-deterministic-first-filtering-preceding-llm-invocations)
- [ADR-007: Provider-Agnostic LLM Gateway over Single-Vendor Dependency](#adr-007-provider-agnostic-llm-gateway-over-single-vendor-dependency)
- [ADR-008: Local Model Runtime Fallback for Offline AI Autonomy (Candidate: Ollama)](#adr-008-local-model-runtime-fallback-for-offline-ai-autonomy-candidate-ollama)
- [ADR-009: Deterministic AI Result Caching via Composite Content Hashing](#adr-009-deterministic-ai-result-caching-via-composite-content-hashing)
- [ADR-010: In-Process Job Scheduling via APScheduler (Zero Distributed Brokers)](#adr-010-in-process-job-scheduling-via-apscheduler-zero-distributed-brokers)
- [ADR-011: Strict Exclusion of Firecrawl from Core V1 Dependencies](#adr-011-strict-exclusion-of-firecrawl-from-core-v1-dependencies)
- [ADR-012: Absolute Prohibition of Authenticated LinkedIn Scraping](#adr-012-absolute-prohibition-of-authenticated-linkedin-scraping)
- [ADR-013: Rejection of Anti-Bot / CAPTCHA Bypass Mechanisms](#adr-013-rejection-of-anti-bot--captcha-bypass-mechanisms)
- [ADR-014: Normalized Relational Core with Immutable Raw Evidence](#adr-014-normalized-relational-core-with-immutable-raw-evidence)

---

### ADR-001: Modular Monolith Architecture over Microservices
- **Status:** Approved
- **Context:** Building an intelligent job feed system requires ingestion, normalization, deduplication, verification, and filtering. Systems of this type are frequently over-engineered into distributed microservices with message buses (Kafka, RabbitMQ) and separate databases.
- **Decision:** Build the V1 system as a single **Modular Monolith** in Python. Modules (Collectors, Ingestion, Normalization, Deduplication, Verification, Filter Rules, AI Gateway) interact via direct in-process method calls and strongly typed Pydantic models. The application codebase is organized under a root-level `app/` directory (resolving the placeholder `src/` directory to ensure direct Python module imports and native ASGI/FastAPI conventions).
- **Consequences:**
  - *Positive:* Zero network serialization latency; single deployment artifact; frictionless local debugging; trivial transaction management; single-developer operational feasibility.
  - *Trade-off:* All components share the same application process; CPU-intensive tasks must not block the async event loop.
- **Alternatives Considered:** Distributed microservices (rejected as extreme premature complexity for V1 volume).

---

### ADR-002: PostgreSQL as Unified Relational and Raw Payload Store
- **Status:** Approved
- **Context:** The system requires structured relational storage for canonical jobs, companies, and audit trails, while also mandating permanent preservation of unstructured raw source responses.
- **Decision:** Use a single **PostgreSQL** instance as the unified datastore. Relational tables enforce data integrity across canonical entities, while `JSONB` columns store raw external payloads and execution telemetry.
- **Consequences:**
  - *Positive:* ACID compliance; zero orphaned records; GIN indexing for fast JSONB querying; full-text search capability; ₹0 open-source licensing.
  - *Trade-off:* Database size grows steadily with raw payloads (mitigated by periodic archival of expired raw records).
- **Alternatives Considered:** MongoDB (rejected due to weak cross-table relational guarantees), SQLite (rejected due to write concurrency locks), separating Postgres + MongoDB (rejected as unnecessary dual-database operational overhead).

---

### ADR-003: Public-Source-First and API-First Ingestion Hierarchy
- **Status:** Approved
- **Context:** Collecting jobs across the internet can be fragile if collectors rely primarily on DOM scraping of complex, constantly changing HTML.
- **Decision:** Ingestion strictly prioritizes public, structured endpoints before falling back to browser-rendered pages:
  $$\text{Public Structured APIs} \succ \text{Direct Public HTTP Endpoints} \succ \text{Static HTML Pages} \succ \text{Headless Browser Rendering}$$
- **Consequences:**
  - *Positive:* High-speed ingestion (sub-second per company); resilient against UI layout redesigns; dramatic reduction in CPU/RAM usage.
  - *Trade-off:* Requires mapping and maintaining endpoint signatures across different ATS platforms.
- **Alternatives Considered:** Universal web scraping with headless browsers (rejected due to excessive resource overhead and fragility).

---

### ADR-004: ATS as Primary Ingestion Backbone with Multi-Source Expansion
- **Status:** Approved
- **Context:** Job boards often contain outdated ghost jobs or unverified aggregate data. Direct Applicant Tracking Systems (ATS) hosted by employers reflect true current vacancies.
- **Decision:** Establish modern public ATS endpoints (**Greenhouse, Lever, Ashby**) as the primary ingestion backbone. Complement this backbone with direct company career portals and public job portals where legally and technically viable.
- **Consequences:**
  - *Positive:* Near 100% job authenticity (`jd_verified` = true); direct official ATS application links; zero scraping blocks on public API endpoints.
  - *Trade-off:* Discovery is limited to companies whose ATS is known (solved via ADR-005).
- **Alternatives Considered:** Relying exclusively on third-party aggregators (rejected due to ghost jobs and stale listings).

---

### ADR-005: Company-Centric Discovery via Structured Company Registry
- **Status:** Approved
- **Context:** Querying global job boards with broad keywords produces massive noise, irrelevant locations, and duplicates.
- **Decision:** Implement a **Company Registry** model. The system curates and discovers target tech companies operating in target hubs (Mumbai, Pune, Bengaluru, Hyderabad), automatically identifies their career endpoints and ATS engines, and schedules targeted harvests.
- **Consequences:**
  - *Positive:* Highly targeted recall; accurate company classification (`IT`/`NON_IT`, `PRODUCT`/`SERVICES`); reliable geographic mapping.
  - *Trade-off:* Requires initial seeding and continuous discovery of new target companies.
- **Alternatives Considered:** Pure keyword-based web scraping (rejected due to low signal-to-noise ratio).

---

### ADR-006: Deterministic-First Filtering Preceding LLM Invocations
- **Status:** Approved
- **Context:** Utilizing LLMs for every incoming job is cost-prohibitive, introduces latency, and risks non-deterministic hallucinations.
- **Decision:** Enforce all business rules (role keywords, 0–2yr experience ladder, location tiers, salary minimums, freshness windows, hard exclusions) through **deterministic Python code** before any AI invocation. LLMs are restricted strictly to resolving genuine ambiguity.
- **Consequences:**
  - *Positive:* Deterministic-first evaluation: maximizes deterministic evaluation and invokes AI only when ambiguity or semantic interpretation requires it, minimizing token consumption and ensuring 100% reproducible filter audit trails with sub-millisecond latency.
  - *Trade-off:* Deterministic string rules must be meticulously maintained and tested against edge cases.
- **Alternatives Considered:** LLM-first agentic evaluation (rejected due to rate-limit exhaustion and unpredictable decisions).

---

### ADR-007: Provider-Agnostic LLM Gateway over Single-Vendor Dependency
- **Status:** Approved
- **Context:** Relying exclusively on any single AI provider creates a single point of failure and risks service disruption when free-tier quotas or commercial policies change.
- **Decision:** Build an abstracted **LLM Gateway** structured around modular provider adapters:
  $$\text{LLM Gateway} \longrightarrow \text{Provider Adapters} \longrightarrow \text{Active Free Cloud Providers} \longrightarrow \text{Local Model Fallback}$$
  Specific cloud providers (such as Google Gemini, Groq, OpenRouter) are implementation-time candidate options and examples, not immutable architectural dependencies. Any provider can be swapped, reordered, or removed via configuration without modifying pipeline business logic. The gateway handles failover, circuit breaking, and rate-limit tracking across all registered adapters.
- **Consequences:**
  - *Positive:* Complete provider independence; ₹0 operating cost; zero vendor lock-in; seamless addition, reordering, or removal of providers via configuration without touching business logic.
  - *Trade-off:* Must maintain provider adapter interfaces and standardize prompts across diverse model architectures.
- **Alternatives Considered:** Hard-coding a single provider or API aggregator as the sole provider (rejected to prevent vendor dependency).

---

### ADR-008: Local Model Runtime Fallback for Offline AI Autonomy (Candidate: Ollama)
- **Status:** Approved
- **Context:** Cloud-based free tiers impose request-per-minute (RPM) and request-per-day (RPD) ceilings. If external quotas are exhausted or internet connectivity is disrupted, pipeline ambiguity evaluation must not halt.
- **Decision:** Integrate a local model fallback adapter within the LLM Gateway. When all active free cloud adapters return 429 (quota exceeded) or are unreachable, the LLM Gateway transparently routes inference requests to a local model runtime. The local runtime is accessed via a provider adapter, with **Ollama** (or alternatives such as llama.cpp) acting as an implementation-time candidate option rather than an immutable architectural dependency.
- **Consequences:**
  - *Positive:* 100% offline autonomy; zero cloud token costs; zero downtime when cloud quotas expire or network access is unavailable.
  - *Trade-off:* Local inference latency is higher on consumer CPU/GPU hardware compared to cloud accelerators.
- **Alternatives Considered:** Blocking pipeline execution until cloud quotas reset (rejected as contrary to operational reliability).

---

### ADR-009: Deterministic AI Result Caching via Composite Content Hashing
- **Status:** Approved
- **Context:** Identical jobs frequently re-appear during daily collection runs or across multi-source postings. Calling LLMs repeatedly on identical JD text wastes tokens and quota.
- **Decision:** Implement deterministic caching using a composite hash key:
  $$\text{Cache Key} = \text{SHA256}(\text{JD Content} + \text{Prompt Version} + \text{Model Version})$$
  Cached extractions are stored in PostgreSQL (`ai_analysis_results`). If the same job is evaluated under the same prompt and model, the result is served directly from the database.
- **Consequences:**
  - *Positive:* Zero redundant LLM calls; near-zero token consumption during regular re-crawls; instant evaluation for known JDs.
  - *Trade-off:* Prompt updates change the hash key, intentionally invalidating historical cache entries for re-evaluation.
- **Alternatives Considered:** In-memory LRU cache (rejected because cache must persist across application restarts).

---

### ADR-010: In-Process Job Scheduling via APScheduler (Zero Distributed Brokers)
- **Status:** Approved
- **Context:** Periodic tasks (polling ATS endpoints, daily freshness sweeps, maintenance jobs) require an orchestration engine.
- **Decision:** Use **APScheduler** running in-process within the FastAPI application. Job execution state is persisted in PostgreSQL to ensure schedule continuity across process restarts.
- **Consequences:**
  - *Positive:* Zero external broker infrastructure (no Redis, RabbitMQ, or Celery workers); ₹0 cost; minimal resource footprint.
  - *Trade-off:* Cannot distribute tasks across multiple worker nodes without adding distributed coordination (unnecessary for V1).
- **Alternatives Considered:** Celery + Redis (rejected as excessive operational overhead for a single-node application).

---

### ADR-011: Strict Exclusion of Firecrawl from Core V1 Dependencies
- **Status:** Approved
- **Context:** Firecrawl is a popular web scraping service, but high-volume reliable usage requires paid plans, conflicting with our strict ₹0 constraint.
- **Decision:** Exclude Firecrawl as a required core dependency. The V1 architecture relies on native `httpx` and local `Playwright`. Firecrawl is cataloged purely as a potential optional third-party adapter for future research.
- **Consequences:**
  - *Positive:* Guarantees 100% free operation; zero proprietary vendor dependency.
  - *Trade-off:* Complex JavaScript pages must be parsed using our internal Playwright collector.
- **Alternatives Considered:** Incorporating Firecrawl as the default scraping engine (rejected due to cost and vendor lock-in).

---

### ADR-012: Absolute Prohibition of Authenticated LinkedIn Scraping
- **Status:** Approved
- **Context:** LinkedIn is a major job portal, but scraping behind user authentication violates terms of service, triggers account bans, and requires continuous anti-bot arms races.
- **Decision:** Strictly prohibit automated authenticated scraping, cookie harvesting, or session spoofing on LinkedIn. Publicly accessible, unauthenticated job links may be cataloged, but automated scraping behind login walls is banned from the V1 architecture.
- **Consequences:**
  - *Positive:* Eliminates legal and operational risk; zero maintenance burden battling LinkedIn anti-bot protections.
  - *Trade-off:* Direct automated ingestion from LinkedIn is restricted to public links or future official partner APIs.
- **Alternatives Considered:** Headless browser scraping using burner accounts (rejected as high-risk, fragile, and non-production-quality).

---

### ADR-013: Rejection of Anti-Bot / CAPTCHA Bypass Mechanisms
- **Status:** Approved
- **Context:** Some aggregators and websites employ Cloudflare Turnstile, reCAPTCHA, and browser fingerprinting to block automated access.
- **Decision:** The system will **not** integrate third-party CAPTCHA solving services (e.g., 2Captcha, Anti-Captcha), residential proxy rotators, or fingerprint masking tools. If an endpoint requires CAPTCHA circumvention, it is treated as an unsuitable automated source and flagged for alternative or manual ingestion.
- **Consequences:**
  - *Positive:* Strictly respects our ₹0 cost limit (CAPTCHA services are paid); prevents IP blacklisting; ensures clean ethical operation.
  - *Trade-off:* Aggressively protected proprietary job portals cannot be automatically crawled without public APIs.
- **Alternatives Considered:** Paid residential proxies and CAPTCHA solvers (rejected due to cost, ethical boundaries, and high failure rates).

---

### ADR-014: Normalized Relational Core with Immutable Raw Evidence
- **Status:** Approved
- **Context:** Ingestion of job postings from diverse external platforms introduces varying schemas, missing fields, and recurring duplicates. Systems commonly choose between two extremes: a purely schemaless document store (e.g., MongoDB), which forfeits relational data integrity, or an aggressive relational normalizer that immediately overwrites or discards original source payloads.
- **Decision:** Use **PostgreSQL** as the unified canonical persistence layer structured around a three-tier data hierarchy:
  $$\text{Raw Evidence} \longrightarrow \text{Canonical Data} \longrightarrow \text{Derived Decisions}$$
  1. **Raw Evidence (`raw_jobs`, `job_source_references`):** Unaltered external payloads stored in native PostgreSQL `JSONB` columns with SHA256 content hashes, establishing permanent provenance and auditability. `raw_jobs` is explicitly anchored to `company_sources` via `company_source_id` to establish unambiguous company endpoint provenance, with metadata (`source_job_id_type`, `collected_at`) maintained exclusively in dedicated columns to ensure `raw_payload` remains strictly unmutated.
  2. **Canonical Data (`jobs`, `companies`, `sources`):** Strongly typed, normalized relational tables enforcing integrity, relationships, and deduplication across unique physical entities.
  3. **Derived Decisions (`verification_records`, `filter_decisions`, `ai_analysis_results`):** Append-only evaluation records preserving rule execution history, multi-dimensional verification passes, and cached AI extractions. Foreign keys enforce `ON DELETE RESTRICT` on audit tables and `ON DELETE SET NULL` on `ai_analysis_results`, preventing accidental cascade deletions.
- **Why:**
  - Allows parser updates, rule changes, and feature enhancements to re-process historical discoveries without re-scraping external endpoints.
  - Preserves exact source attribution across multi-source cross-postings (`job_source_references`).
  - Guarantees referential integrity across companies, jobs, and audits via foreign key constraints.
  - Supports rapid feed queries via B-Tree indexing on canonical status and priority fields.
- **Consequences:**
  - *Positive:* Zero information loss; full historical reproducibility; ACID guarantees prevent orphaned records; enables deterministic backtesting of rule engine changes; ₹0 operational cost.
  - *Trade-off:* Database size grows steadily with raw payloads (managed via periodic archival of historical payloads older than 180 days).
- **Alternatives Considered:**
  - *Pure Document Store (e.g., MongoDB):* Rejected because lack of strict relational constraints risks orphaned job references, and document duplicates cannot easily maintain multi-source provenance.
  - *Pure Relational Normalization without Raw JSONB:* Rejected because discarding raw external payloads makes it impossible to re-extract newly targeted fields or audit parsing bugs without re-scraping.
  - *Separate Databases (e.g., Postgres for relational + MongoDB for raw payloads):* Rejected as unnecessary operational overhead violating the single-node simplicity mandate.
