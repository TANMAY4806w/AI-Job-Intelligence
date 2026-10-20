# System Architecture Specification (V1)

> **Document Type:** Production Architecture Specification  
> **Status:** Approved Baseline  
> **Version:** 1.0  
> **Target Phase:** Phase 1 (Architecture & System Design)  
> **Parent Specifications:** [`../requirements/V1_SPECIFICATION.md`](../requirements/V1_SPECIFICATION.md) | [`../requirements/JOB_DATA_CONTRACT.md`](../requirements/JOB_DATA_CONTRACT.md)  
> **Related Rules:** [`../../rules/JOB_FILTERS_V1.md`](../../rules/JOB_FILTERS_V1.md) | [`../../rules/DEDUPLICATION_V1.md`](../../rules/DEDUPLICATION_V1.md) | [`../../rules/VERIFICATION_V1.md`](../../rules/VERIFICATION_V1.md)

---

## 1. Executive Summary & Architectural Goals

The **AI Job Intelligence** system is engineered to solve the fragmentation, unreliability, and noise of modern technical job discovery. Its mission is to deliver a curated, high-trust job feed focused on **AI / GenAI Engineering** and foundational **Backend / Python** roles in target Indian tech hubs.

### 1.1 Key Architectural Goals
1. **Maximum Trustworthy Recall:** Maximize the discovery of eligible opportunities without discarding records due to incomplete or ambiguous metadata.
2. **Strict ₹0 Operating Cost:** Operate entirely on open-source software, free/public endpoints, public company registries, local runtimes, and sustainable free-tier API quotas.
3. **Public-Source & Compliance-First Safety:** Eliminate brittle anti-bot circumvention, CAPTCHA bypasses, and unauthorized authenticated scraping (specifically avoiding authenticated LinkedIn scraping).
4. **Deterministic Precedence:** Enforce business logic, verification, and filtering via deterministic algorithms before invoking LLMs.
5. **Full Provenance & Auditability:** Preserve raw payloads immutably and maintain machine-readable audit trails for every status change, conflict, and review flag.
6. **Modular Simplicity (Single Developer Operable):** Deploy as a cohesive modular monolith without distributed messaging buses, microservices, or complex operational overhead.

---

## 2. Core Architectural Principles

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                             CORE PRINCIPLES                                 │
├──────────────────────┬──────────────────────┬───────────────────────────────┤
│    ₹0 Hard Limit     │  Public-Source First │     Maximum Recall            │
│  Free/OSS & Local    │  Public APIs > HTML  │  Incomplete data -> REVIEW    │
├──────────────────────┼──────────────────────┼───────────────────────────────┤
│ Deterministic First  │ AI Provider Agnostic │   Immutable Provenance        │
│ Rule checks before AI│ Multi-provider/Local │   Never delete source payloads│
└──────────────────────┴──────────────────────┴───────────────────────────────┘
```

1. **The ₹0 Hard Constraint:** Every core architectural component must run on standard developer hardware or free-tier cloud infrastructure. No component may hard-depend on paid commercial APIs (e.g., paid Firecrawl, paid proxy networks, paid LLM endpoints).
2. **Hierarchy of Data Sources:** Ingestion adheres strictly to:
   $$\text{Public Structured APIs} \succ \text{Direct Public HTTP Endpoints} \succ \text{Static HTML Career Pages} \succ \text{Headless Browser Rendering (Fallback)}$$
3. **Company-Centric Discovery Backbone:** Rather than randomly querying job portals, discovery anchors on a structured **Company Registry** that links employers directly to their respective Applicant Tracking Systems (ATS) and career portals.
4. **Source Safety & Non-Circumvention:** The system strictly interacts with public endpoints. It does not perform authenticated scraping on hostile networks, bypass Cloudflare/CAPTCHA, or violate platform access terms.
5. **Multi-Source Provenance:** A single physical job vacancy may be announced across multiple portals. Deduplication clusters these sources under one canonical entity while keeping all raw source payloads intact.
6. **Conflict Conservatism:** When sources disagree on critical attributes (experience, compensation, location, dates), the system **never** assumes the "more permissive" value; it preserves all assertions and flags the record as `SOURCE_CONFLICT_REVIEW`.
7. **LLM as an Ambiguity Specialist:** LLMs are strictly quarantined to late-stage nuance extraction (semantic role normalization, fuzzy experience clause parsing, tech-stack disambiguation) and are never used for routine discovery or deterministic filtering.

---

## 3. High-Level System Architecture (Modular Monolith)

The system is organized as a single cohesive modular monolith. Communication between subsystems occurs via direct Python method calls and strongly typed Pydantic models, eliminating network serialization overhead and message bus operational complexity.

```text
                                 EXTERNAL SOURCES
   ┌───────────────────────┬──────────────────────────┬───────────────────────┐
   │ Direct ATS Endpoints  │ Public Company Webpages  │ Public Job Boards/APIs│
   │ (Greenhouse, Lever,   │ (Static HTML / Rendered) │ (Naukri, Indeed,      │
   │  Ashby)               │                          │  Cutshort, etc.)      │
   └───────────┬───────────┴────────────┬─────────────┴───────────┬───────────┘
               │                        │                         │
═══════════════╪════════════════════════╪═════════════════════════╪════════════
               │                        │                         │
   ┌───────────▼────────────────────────▼─────────────────────────▼───────────┐
   │                        INGESTION & DISCOVERY LAYER                       │
   │  ┌───────────────────────┐             ┌──────────────────────────────┐  │
   │  │   Company Registry    │             │   Collector Subsystem        │  │
   │  │   (Curated Datasets,  │────────────>│   (BaseCollector, Greenhouse,│  │
   │  │    ATS Detector)      │             │    Lever, Ashby, Portals)    │  │
   │  └───────────────────────┘             └──────────────┬───────────────┘  │
   └───────────────────────────────────────────────────────┼──────────────────┘
                                                           │ Raw JSON / HTML
                                            ┌──────────────▼──────────────────┐
                                            │ Raw Source Preservation Table   │
                                            │ (raw_jobs / immutable audit)    │
                                            └──────────────┬──────────────────┘
                                                           │
   ┌───────────────────────────────────────────────────────┼──────────────────┐
   │                        CORE PROCESSING PIPELINE       │                  │
   │                                                       │                  │
   │  ┌─────────────────────────────────────────────────┐  │                  │
   │  │ 1. Normalization Engine (Schema Standardisation)│<─┘                  │
   │  └────────────────────────┬────────────────────────┘                     │
   │                           ▼                                              │
   │  ┌─────────────────────────────────────────────────┐                     │
   │  │ 2. Deduplication Engine (Staged Evidence)       │                     │
   │  └────────────────────────┬────────────────────────┘                     │
   │                           ▼                                              │
   │  ┌─────────────────────────────────────────────────┐                     │
   │  │ 3. Verification Engine (8 Independent Checks)   │                     │
   │  └────────────────────────┬────────────────────────┘                     │
   │                           ▼                                              │
   │  ┌─────────────────────────────────────────────────┐                     │
   │  │ 4. Deterministic Filter Engine (V1 Rules)       │                     │
   │  └────────────────────────┬────────────────────────┘                     │
   │                           │                                              │
   │            Ambiguous Data │ (Needs AI)       Eligible / Definite Discard │
   │                           ▼                               │              │
   │  ┌─────────────────────────────────────────────────┐      │              │
   │  │ 5. LLM Gateway & Result Cache (Fallback & Hash) │      │              │
   │  └────────────────────────┬────────────────────────┘      │              │
   │                           │                               │              │
   │                           └───────────────┬───────────────┘              │
   │                                           ▼                              │
   │  ┌─────────────────────────────────────────────────┐                     │
   │  │ 6. Ranking & Feed Presentation (P1, P2, P3)     │                     │
   │  └─────────────────────────────────────────────────┘                     │
   └───────────────────────────────────────────┬──────────────────────────────┘
                                               │
                                    ┌──────────▼──────────┐
                                    │ PostgreSQL Database │
                                    │ (Canonical Jobs,    │
                                    │  Company Registry,  │
                                    │  Audit Trail)       │
                                    └──────────▲──────────┘
                                               │
   ┌───────────────────────────────────────────┴──────────────────────────────┐
   │                        ORCHESTRATION & CONTROL                           │
   │  ┌─────────────────────────┐             ┌────────────────────────────┐  │
   │  │   APScheduler Engine    │             │   FastAPI Administrative   │  │
   │  │   (Cron / Intervals)    │             │   & Presentation API       │  │
   │  └─────────────────────────┘             └────────────────────────────┘  │
   └──────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Component Responsibilities & Boundaries

### 4.1 Company Discovery & Company Registry
To ensure comprehensive job discovery without relying on fragile global keyword scraping, the system uses a **Company-Centric Architecture**.

- **Company Registry:** A structured database storing hiring organizations, known operating domains, physical office locations (P1/P2 city validation), industrial sector (`IT` / `NON_IT` / `UNKNOWN`), and business model (`PRODUCT` / `SERVICES` / `BOTH` / `UNKNOWN`).
- **Discovery Sources:**
  - Curated seed lists of tech firms, startups, and product companies operating in Mumbai, Navi Mumbai, Thane, Pune, Bengaluru, and Hyderabad.
  - Open datasets (e.g., GitHub startup repositories, public tech directories).
  - Common Crawl domain indices to identify career subdomain patterns (`careers.*`, `jobs.*`).
- **ATS Detector:**
  - Probes company career portals to identify the underlying ATS provider.
  - Inspects DNS CNAME records, HTTP redirects, and DOM scripts to detect standard ATS footprints:
    - Greenhouse (`boards.greenhouse.io/<company>` or `api.greenhouse.io`)
    - Lever (`jobs.lever.co/<company>` or `api.lever.co`)
    - Ashby (`jobs.ashbyhq.com/<company>` or `api.ashbyhq.com`)
    - Workday / SmartRecruiters / Taleo (tagged for future expansion)
  - Links discovered companies to their direct ATS identifier, enabling structured API harvesting.

### 4.2 Ingestion & Collector Subsystem
The ingestion subsystem defines a decoupled, extensible collector architecture. Every source adapter implements a standard contract.

#### Architectural Collector Interface (`BaseCollector`)
```python
# Conceptual architectural interface - implementation deferred to Phase 2
class BaseCollector(ABC):
    source_name: str
    rate_limit_per_minute: int
    requires_browser: bool

    @abstractmethod
    async def discover_jobs(self, company_context: CompanyContext) -> AsyncIterator[RawJobPayload]:
        """Harvests raw job records from target endpoint without schema transformation."""
        pass

    @abstractmethod
    async def health_check(self) -> SourceHealthStatus:
        """Verifies endpoint availability, latency, and structure validity."""
        pass
```

#### Collector Specializations
1. **`GreenhouseCollector`:** Queries public JSON endpoints (`https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`). Requires zero browser rendering; highly stable, fast, and structured.
2. **`LeverCollector`:** Queries public REST endpoints (`https://api.lever.co/v0/postings/{company}?mode=json`). High-speed, structured JSON responses.
3. **`AshbyCollector`:** Queries public Ashby job-board endpoints (`https://api.ashbyhq.com/posting-api/job-board/{organization}`).
4. **`CompanyCareerCollector`:** Targets custom company career pages. Uses direct `httpx` GET requests for static pages, falling back to headless `Playwright` only when dynamic client-side rendering (CSR) is detected.
5. **`PortalCollectors` (Naukri, Cutshort, Indeed, etc.):** Interfaces designed to harvest publicly accessible listings without authenticated sessions, respecting robots.txt and public HTTP access boundaries.

#### Independence Guarantee
Adding, updating, or disabling an individual collector requires **zero changes** to the normalization, deduplication, verification, filtering, or AI gateway modules.

### 4.3 Raw Data Preservation Strategy
1. **Zero Data Destruction:** When a collector encounters a posting, the raw response (unparsed JSON dictionary, raw HTML string, or API snippet) is immediately inserted into the `raw_jobs` table.
2. **Reprocessing Capability:** If normalization schemas evolve or new extraction rules are introduced, the entire canonical database can be reconstructed or backfilled directly from `raw_jobs` without triggering network requests to external sources.
3. **Audit Trail:** Every canonical `Job` references one or more [`JobSourceReference`](../requirements/JOB_DATA_CONTRACT.md#4-supporting-entity-jobsourcereference) records containing the `raw_payload_id` and raw text.

### 4.4 Normalization Engine
The Normalization Engine accepts raw payloads and standardizes them into the canonical [`Job`](../requirements/JOB_DATA_CONTRACT.md#3-job-entity-contract) schema:
- **Title Normalization:** Trims whitespace, cleans tracking tags, strips recruitment boilerplate (e.g., *"Hiring Urgently!"* $\rightarrow$ extracted to tags).
- **Location Normalization:** Resolves aliases into canonical forms (e.g., "Bangalore" $\rightarrow$ "Bengaluru, Karnataka", "Bombay" $\rightarrow$ "Mumbai, Maharashtra").
- **Salary Standardisation:** Converts diverse representations (e.g., "₹35,000 pm", "₹6 LPA", "$100,000 / year") into normalized `salary_min`, `salary_max`, `salary_period`, and `salary_currency`.
- **Text Extraction:** Cleans raw HTML/markdown into formatted, human-readable plain text descriptions.

### 4.5 Deduplication Engine
Implements the 4-stage evidence hierarchy defined in [`DEDUPLICATION_V1.md`](../../rules/DEDUPLICATION_V1.md):
- **Stage 1 (Exact Platform ID):** Matches identical `source` and `source_job_id` (Confidence: 1.0). Uses native platform IDs for structured ATS endpoints and deterministic `"gen_<hash>"` identifiers for generic career pages lacking native IDs.
- **Stage 2 (Canonical ATS Endpoint):** Matches identical direct application links (Confidence: 0.98).
- **Stage 3 (Composite Deterministic Match):** Evaluates `normalized_company_name` + `normalized_title` + `normalized_location` + posting date proximity $\le 14$ days (Confidence: 0.90).
- **Stage 4 (Fuzzy Overlap & Dispute Resolution):** Triggers `review_reason = DUPLICATE_AMBIGUOUS` for ambiguous matches.
- **Cross-Source Attribute Conflict Policy:** When conflicting attributes occur across sources, the system **never** picks the "more permissive" value. Values from each source are preserved in their respective `JobSourceReference`, canonical fields are marked uncertain, and the job transitions to `eligibility_status = REVIEW` (`review_reason = SOURCE_CONFLICT_REVIEW`).

### 4.6 Verification Engine
Executes the eight independent verification dimensions specified in [`VERIFICATION_V1.md`](../../rules/VERIFICATION_V1.md):
1. **Source Validity:** Verifies approved origin platform (`source_verified`).
2. **Actual JD Existence:** Checks body length (> 100 chars) and HTTP live status (`jd_verified`).
3. **Application URL Validity:** Tests reachability and HTTPS syntax of destination endpoint.
4. **Company Legitimacy:** Cross-references company against the local `CompanyRegistry` (`company_verified`).
5. **Location Consistency:** Confirms P1/P2 cities or explicit remote eligibility in JD body (`location_verified`).
6. **Experience Consistency:** Verifies experience expressions match 0–2 year criteria (`experience_verified`).
7. **Salary Statedness:** Determines if salary is disclosed vs. null (`salary_verified`).
8. **Posting Date Plausibility:** Validates publication timestamp within $T_{collected} \ge T_{posted} \ge (T_{collected} - 90\text{ days})$. If `posted_at` is missing or invalid, per the frozen verification rule the system falls back provisionally to `collected_at` for relative plausibility evaluation and flags `review_reason = POSTED_DATE_SUSPICIOUS`. In the canonical schema, `posted_at` remains NULL and the job must remain in `REVIEW` with `POSTED_DATE_SUSPICIOUS` until publication-date validity is established; `collected_at` alone serves only as ingestion metadata and provisional verification fallback, and never establishes active-feed eligibility or freshness. Late-discovered jobs with verified publication dates older than 5 days remain `EXPIRED` (`rejection_reason = JOB_TOO_OLD`).
- **Confidence Computation:** Generates weighted composite score ($0.0 \le C \le 1.0$). If $C < 0.60$, record is routed to dedicated review.

### 4.7 Rule Engine & Deterministic Filtering
Implements the frozen business rules from [`JOB_FILTERS_V1.md`](../../rules/JOB_FILTERS_V1.md):
- **Role Prioritization:** Classifies into `P1` (14 core AI/GenAI roles), `P2` (7 AI Backend/ML roles), `P3` (9 foundational software roles), or `NOT_RELEVANT`.
- **Hard Exclusions:** Rejects prohibited roles (Salesforce, generic frontend, QA, IT Support, Architect/PM/BA) with `rejection_reason = EXCLUDED_ROLE`.
- **Java / .NET Nuance:** Does not reject modern AI/backend roles that mention Java/.NET alongside modern stacks; rejects only roles fundamentally centered on legacy Java/.NET enterprise development.
- **Experience Ladder:**
  - `0–1`, `0–2`, `1–2 years`, `Fresher`, `Entry-level` $\rightarrow$ **`KEEP` (`ELIGIBLE`)**
  - `1–3 years`, ambiguous/unstated minimum $\rightarrow$ **`REVIEW` (`EXPERIENCE_AMBIGUOUS`)**
  - Stated range starts at $\ge 2$ years (`2–3`, `2–4`, `2–5`, `3+`, `Senior`, `Lead`, `Architect`) $\rightarrow$ **`EXCLUDE` (`REJECTED`, `EXPERIENCE_TOO_HIGH`)**
- **Location & Remote Policy:** P1/P2 accepted. Remote roles with head office outside P1/P2 are **not** rejected; routed to `REVIEW` (`LOCATION_REMOTE_REVIEW`) pending explicit remote policy finalization.
- **Regional Salary Minimums:** Mumbai / Navi Mumbai / Thane (no strict minimum required), Pune ($\ge$ ₹30,000/month or ₹3.6 LPA minimum), Bengaluru ($\ge$ ₹5.0 LPA minimum), Hyderabad ($\ge$ ₹5.0 LPA minimum). Disclosed compensation below regional minimum $\rightarrow$ `REJECTED` (`SALARY_BELOW_MINIMUM`). Unknown salary $\rightarrow$ **never** automatically rejected; preserved in `KEEP`/`REVIEW` with `salary_verified = false` and `review_reason = SALARY_UNKNOWN`.
- **Freshness Window:** Strictly governed by original publication date (`posted_at`), **never** `collected_at`: 0–24h (Highest), 1–3d (High), 3–5d (Normal), >5d (`JOB_TOO_OLD`, excluded from active feed, preserved in historical database). Late-discovered postings whose `posted_at` is older than 5 days are preserved historically and routed directly to `EXPIRED` (`JOB_TOO_OLD`) rather than active; `collected_at` serves solely as ingestion metadata.
- **Skills Advisory:** Skills are advisory ranking dimensions; missing skills **never** cause rejection.

---

## 5. LLM Gateway & Result Cache

To adhere strictly to the ₹0 cost constraint while avoiding single-provider vendor lock-in, all AI interactions pass through an abstracted **LLM Gateway**.

```text
               Deterministic Pipeline Encountered Ambiguity
                                    │
                                    ▼
                     ┌──────────────────────────────┐
                     │   Deterministic Pre-Filter   │
                     │  (Can rules resolve this?)   │
                     └──────────────┬───────────────┘
                                    │ Yes -> Resolve without LLM
                                    │ No
                                    ▼
                     ┌──────────────────────────────┐
                     │   Content-Hash Cache Check   │
                     │  hash(JD + prompt_v + model) │
                     └──────────────┬───────────────┘
                                    │
                      ┌─────────────┴─────────────┐
                      ▼ Cache Hit                 ▼ Cache Miss
             ┌─────────────────┐       ┌──────────────────────────────┐
             │ Return Cached   │       │      LLM Gateway Router      │
             │ JSON Analysis   │       │  (Rate-limit & Quota Tracker)│
             └─────────────────┘       └──────────────┬───────────────┘
                                                      │
                        ┌─────────────────────────────┴─────────────────────────────┐
                        ▼ Primary                                                   ▼ Fallback
             ┌─────────────────────────────┐                             ┌─────────────────────────────┐
             │ Free-Tier Cloud Adapters    │── Timeout / Rate-Limit ────>│ Local Model Fallback Adapter│
             │ (Candidate Pool: Gemini,    │   Exceeded or Unavailable   │ (Candidate: Ollama / Local  │
             │  Groq, OpenRouter, etc.)    │                             │  Open Weights, 100% Offline)│
             └──────────────┬──────────────┘                             └──────────────┬──────────────┘
                            │                                                           │
                            └─────────────────────────────┬─────────────────────────────┘
                                                          │
                                                          ▼
                                           ┌──────────────────────────────┐
                                           │  Pydantic Schema Validation  │
                                           │  & Extraction Sanity Check   │
                                           └──────────────┬───────────────┘
                                                          │
                                                          ▼
                                           ┌──────────────────────────────┐
                                           │ Store in Cache & PostgreSQL  │
                                           │ (ai_analysis_results)        │
                                           └──────────────────────────────┘
```

### 5.1 Gateway Router & Provider Adapter Architecture
- **Provider-Agnostic Abstraction:** The LLM subsystem follows a modular adapter pattern:
  $$\text{LLM Gateway} \longrightarrow \text{Provider Adapters} \longrightarrow \text{Active Free Cloud Providers} \longrightarrow \text{Local Model Fallback}$$
- **Modular Adapter Independence:** The gateway defines a clean adapter interface (`BaseLLMAdapter`). Individual providers can be added, removed, reordered, or disabled entirely through configuration at implementation time without modifying upstream business logic or filtering rules.
- **Candidate Free-Tier Cloud Providers (Implementation Options):**
  Specific providers and models are implementation-time options evaluated for current availability and free quotas, such as:
  - *Google Gemini API (Free Tier):* Candidate for high-context extraction under zero-cost developer quotas.
  - *Groq Cloud (Free Tier):* Candidate for low-latency inference on open models.
  - *OpenRouter / Hugging Face Free Pools:* Supplementary candidates for free-tier open models.
- **Local Model Fallback (Implementation Option):**
  - An open-source local inference runtime (e.g., *Ollama* or *llama.cpp* hosting quantized models such as Llama-3-8B-Instruct or Mistral-7B) serves as the offline failover tier.
  - Ensures 100% offline resilience and uninterrupted operation when external cloud quotas or rate limits are exhausted.
- **Failover & Circuit Breaking:** If the active cloud provider encounters a rate limit (429), timeout, or service error (503), the gateway cascades through remaining active cloud adapters before falling back to the local model runtime.
- **Structured Output Enforcement:** Prompts mandate strict JSON output validated via Pydantic schemas. Unparseable responses trigger automatic re-prompts or route the record to human review.

### 5.2 AI Cost & Token Control Strategy
1. **Deterministic Pre-Filtering:** LLMs are never called for clearly eligible or clearly excluded roles. If a job title is `"Salesforce Developer"`, it is rejected deterministically with zero tokens spent.
2. **Composite Cache Identity:**
   $$\text{Cache Key} = \text{SHA256}(\text{JD Content} + \text{Prompt Version} + \text{Model Version})$$
   If the identical job description is evaluated under the same prompt and model version, the gateway returns the cached analysis without issuing an API call.
3. **Prompt Minimization:** Prompts transmit only extracted candidate-relevant sections (responsibilities, requirements, qualifications), pruning company marketing boilerplate.

---

## 6. Persistence & Relational Schema Design

The persistence layer uses a single **PostgreSQL** database. It combines relational schemas for canonical jobs, companies, and audit events with `JSONB` columns for unstructured raw payloads.

```text
┌─────────────────┐       ┌──────────────────────┐       ┌──────────────────────┐
│    companies    │       │   company_sources    │       │       sources        │
├─────────────────┤       ├──────────────────────┤       ├──────────────────────┤
│ id (PK)         │1     *│ id (PK)              │*     1│ id (PK)              │
│ name            ├───────┤ company_id (FK)      ├───────┤ source_name          │
│ normalized_name │       │ source_id (FK)       │       │ base_url             │
│ domain          │       │ ats_type (Enum)      │       │ is_active            │
│ sector (Enum)   │       │ ats_identifier (NULL)│       └──────────────────────┘
│ business_model  │       │ career_url           │
│ is_verified     │       │ api_endpoint         │
│ confidence      │       └──────────┬───────────┘
└────────┬────────┘                  │ 1 (Provenance)
         │ 1                         │
         │ *                         │ *
┌────────┴────────┐1     *┌──────────┴───────────┐*     1┌──────────────────────┐
│      jobs       ├───────┤ job_source_references├───────┤       raw_jobs       │
├─────────────────┤       ├──────────────────────┤       ├──────────────────────┤
│ id (PK)         │       │ id (PK)              │       │ id (PK)              │
│ company_id (FK) │       │ job_id (RESTRICT)    │       │ company_source_id(FK)│
│ title           │       │ raw_job_id (FK)      ├───────┤ raw_payload (JSONB)  │
│ location        │       │ source_url           │       │ source_job_id        │
│ eligibility_stat│       │ application_url      │       │ source_job_id_type   │
│ role_priority   │       │ source_job_id        │       │ content_hash (SHA256)│
│ verif_confidence│       │ source_job_id_type   │       │ collected_at         │
└────────┬────────┘       │ source_data (JSONB)  │       └──────────────────────┘
                          └──────────────────────┘
         │ 1
         ├───────────────────────────────────────┐
         │ 1                                     │ 1
┌────────┴─────────────┐                ┌────────┴─────────────┐
│ verification_records │                │ ai_analysis_results  │
├──────────────────────┤                ├──────────────────────┤
│ id (PK)              │                │ id (PK)              │
│ job_id (RESTRICT)    │                │ job_id (SET NULL)    │
│ jd_verified (bool)   │                │ cache_key (unique)   │
│ source_verified      │                │ model_name           │
│ experience_verified  │                │ prompt_version       │
│ salary_verified      │                │ structured_output    │
│ audit_trail (JSONB)  │                │ tokens_used          │
└──────────────────────┘                └──────────────────────┘
```

### 6.1 Core Relational Tables
1. **`companies`:** Canonical corporate directory (name, normalized name, website, sector, business model, verified status, confidence).
2. **`sources`:** Registry of external discovery channels (e.g., `GREENHOUSE`, `LEVER`, `ASHBY`, `NAUKRI`).
3. **`company_sources`:** Map connecting companies to their detected ATS tokens, direct career URLs, and scraper configurations. `ats_identifier` is nullable for generic career pages, enforced via PostgreSQL partial unique indexes (`uq_company_sources_ats`, `uq_company_sources_career_url`, `uq_company_sources_api_endpoint`, and `uq_company_sources_bare_mapping`) preventing duplicate configurations across all nullability combinations.
4. **`raw_jobs`:** Immutable append-only store preserving raw external payloads. Anchored to `company_sources` via `company_source_id` to establish unambiguous origin provenance (`raw_payload JSONB`, `content_hash`, `collected_at`, `source_job_id`, `source_job_id_type`). Metadata is maintained strictly in dedicated columns, ensuring `raw_payload` remains byte-for-byte immutable.
5. **`jobs`:** Canonical deduplicated job listings with normalized fields, current lifecycle status (`eligibility_status`), role priority (`role_priority`), and machine-readable rejection/review reasons.
6. **`job_source_references`:** Link table preserving provenance. Every discovery of an opening on any platform links back to the canonical `jobs.id` and retains the platform-specific URL, timestamps, `source_job_id`, `source_job_id_type`, and extracted data. Enforces `ON DELETE RESTRICT` against `jobs(id)` to prevent accidental deletion of corroborated postings.
7. **`verification_records`:** Stores independent dimension verification outcomes and machine-readable audit trails. Enforces `ON DELETE RESTRICT` against `jobs(id)`.
8. **`ai_analysis_results`:** Key-value cache indexed by `cache_key = SHA256(...)` storing structured LLM extractions. Enforces `ON DELETE SET NULL` on `job_id` to permanently preserve cached inferences and token investments.
9. **`collection_runs` / `discovery_runs`:** Observability tables logging execution durations, collector outcomes, HTTP statuses, and yield metrics.

---

## 7. Scheduling & Pipeline Orchestration

To maintain the ₹0 cost model and avoid the operational overhead of Redis, Celery, or RabbitMQ, execution scheduling is managed via **APScheduler** running within the application process.

### 7.1 Pipeline Execution Schedules
| Job Type | Trigger | Cadence | Purpose |
| :--- | :--- | :--- | :--- |
| **High-Frequency Ingestion** | Interval | Every 3 hours | Queries direct ATS endpoints (Greenhouse, Lever, Ashby) for rapid discovery (0–24h fresh tier). |
| **Daily Ingestion & Crawl** | Cron | Daily at 02:00 IST | Executes deeper career page crawls and portal harvests. |
| **Freshness & Expiry Sweep** | Cron | Daily at 04:00 IST | Sweeps canonical jobs older than 5 days based on original publication date (`posted_at`), transitioning them to `EXPIRED` (`JOB_TOO_OLD`) in the active feed. |
| **Company Discovery Sweep** | Cron | Weekly on Sunday | Runs ATS detection against new companies added to the registry. |

### 7.2 Concurrency & Threading Model
- **Async I/O with `httpx`:** Network-bound collector requests run concurrently via `asyncio.gather` with strict semaphore concurrency limits (e.g., maximum 5 concurrent connections per domain to respect server health).
- **Graceful Shutdown & State Persistence:** APScheduler persists job execution state in the PostgreSQL database (`apscheduler_jobs` table), ensuring scheduled runs survive process restarts without duplicate execution.

---

## 8. Source Safety & Ethical Scraping Boundaries

The architecture mandates strict compliance and safety boundaries to ensure sustainable, long-term operation:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                        SOURCE SAFETY BOUNDARIES                             │
├──────────────────────────────────────┬──────────────────────────────────────┤
│              PERMITTED               │          STRICTLY PROHIBITED         │
├──────────────────────────────────────┼──────────────────────────────────────┤
│ • Public ATS APIs (JSON endpoints)   │ • Authenticated LinkedIn scraping    │
│ • Direct public career pages         │ • CAPTCHA solving / bypass services  │
│ • Robots.txt compliant requests      │ • Anti-bot evasion / IP fingerprinting│
│ • Respectful rate limits (1-2 req/s) │ • Credential stuffing / spoofing     │
│ • Clear identification User-Agent    │ • Automated application submission   │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

1. **Zero Authenticated LinkedIn Scraping:** LinkedIn is a valuable discovery reference, but automated scraping behind authentication barriers is strictly excluded from V1. Publicly shared, indexable opportunities may be referenced, but no user sessions, cookies, or account bypasses will be engineered.
2. **No Anti-Bot Bypass or CAPTCHA Breaking:** The system does not incorporate CAPTCHA-solving APIs (e.g., 2Captcha), TLS fingerprint spoofers, or residential proxy rotators. If a public source requires CAPTCHA bypass, it is deemed hostile and marked for manual/alternative ingestion.
3. **Transparent Identity & Polite Rate Limiting:** All outbound HTTP requests transmit a descriptive User-Agent header and enforce per-domain rate limiting (minimum 1–2 second delay between requests on identical hosts).

---

## 9. Observability & Auditability

Observability is integrated directly into the modular monolith using structured logging and lightweight PostgreSQL audit tables, avoiding bloated external monitoring infrastructure.

### 9.1 Audit Metrics Tracked
- **Collector Metrics:** Run start/finish timestamps, total requests, HTTP 2xx/4xx/5xx counts, records discovered, records failed.
- **Pipeline Metrics:** Records normalized, duplicates detected (by stage), verification pass/fail counts by dimension.
- **Rule Outcomes:** Distribution of assigned statuses (`ELIGIBLE`, `REVIEW`, `REJECTED`, `EXPIRED`) and counts of specific reason codes (`EXPERIENCE_TOO_HIGH`, `SALARY_BELOW_MINIMUM`, `LOCATION_REMOTE_REVIEW`, etc.).
- **AI Token & Quota Tracking:** API requests per provider, cache hit ratio, token consumption, fallback events to local model runtime (e.g., Ollama).

### 9.2 Structured Logging Standard
Logs are emitted in structured JSON format with execution context (e.g., `job_id`, `source`, `run_id`, `collector_name`, `error_code`), allowing straightforward analysis using standard CLI tools (`jq`, `grep`).

---

## 10. Testing & Verification Strategy

The architecture is built for comprehensive test-driven development (TDD), ensuring each pipeline step is testable in complete isolation.

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                             TESTING SUITE                                   │
├───────────────────┬─────────────────────────────────────────────────────────┤
│ Unit Tests        │ • Normalization functions (title, salary, location)     │
│                   │ • Rule engine evaluations across experience bands       │
│                   │ • Deduplication matching logic (Stages 1–4)             │
├───────────────────┼─────────────────────────────────────────────────────────┤
│ Fixture-Based     │ • Recorded mock JSON responses for Greenhouse/Lever/Ash │
│ Source Tests      │ • Static HTML fixtures for custom career pages          │
│                   │ • Zero live network dependency during test suite runs   │
├───────────────────┼─────────────────────────────────────────────────────────┤
│ Gateway Mocking   │ • Mocked LLM responses asserting Pydantic JSON parsing  │
│                   │ • Fallback cascade tests (Free API 429 -> Local Model Runtime) │
├───────────────────┼─────────────────────────────────────────────────────────┤
│ Integration Tests │ • End-to-end processing: Raw Payload -> Canonical Job   │
│                   │ • PostgreSQL schema constraints and transaction rollbacks│
└───────────────────┴─────────────────────────────────────────────────────────┘
```

---

## 11. Scalability Considerations & Bottlenecks

### 11.1 Projected V1 Volume
- **Company Registry:** 500 – 1,000 tracked target companies.
- **Daily Ingestion:** 2,000 – 5,000 raw job records collected daily.
- **Active Canonical Jobs:** ~1,000 – 2,000 active postings in the curated feed at any time.

### 11.2 Bottleneck Analysis & Mitigations
- **Database Storage:** With raw payloads averaging 10–50 KB, 5,000 daily jobs consume ~100–250 MB daily. PostgreSQL handles this volume effortlessly on single-node hardware. Periodic archiving of expired `raw_jobs` older than 180 days keeps index sizes minimal.
- **LLM Rate Limits:** Free-tier cloud rate limits and quotas are protected by:
  1. Deterministic-first evaluation: maximize deterministic evaluation and invoke AI only when ambiguity or semantic interpretation requires it.
  2. SHA256 composite content caching (eliminating repeat analyses of known JDs).
  3. Seamless failover to a local model runtime (e.g., Ollama).
- **Network I/O:** Asynchronous batching with `httpx` ensures 1,000 ATS endpoints can be polled in under 5 minutes without blocking the application.

---

## 12. Unresolved Research Questions & Future Extensions

The following items are identified for continued research during Phase 1/2:
1. **Dynamic CSR Page Detection Heuristics:** What is the optimal low-overhead heuristic to decide when a company career page requires headless Playwright rendering versus simple `httpx` static parsing?
2. **Common Crawl Index Extraction Efficiency:** Evaluating whether querying Common Crawl's CDX index directly is faster than targeted DNS/subdomain discovery for identifying unlisted career pages.
3. **Automated ATS Identification Signatures:** Compiling an exhaustive signature map of URL redirects and DOM markers for secondary ATS providers (e.g., Workable, BambooHR, Recruitee).
4. **Firecrawl Integration (Optional Future Adapter):** If self-hosted Firecrawl or a free community connector matures, it may be evaluated as an optional adapter for complex JavaScript pages, but V1 remains completely operational without it.

---

## 13. V1 Scope Boundaries

| Capability | Included in V1 Architecture | Formally Deferred Beyond V1 |
| :--- | :--- | :--- |
| **Multi-Source Ingestion** | Yes (Greenhouse, Lever, Ashby, direct pages, portals) | Distributed scrapers on Kubernetes |
| **Raw Payload Preservation** | Yes (Immutable `raw_jobs` table) | External cold-storage object stores (S3/GCS) |
| **Deduplication** | Yes (4-stage deterministic evidence hierarchy) | Vector embedding similarity models |
| **Rule Filtering** | Yes (P1/P2/P3, 0–2yr rules, location, salary, freshness)| Automated resume matching |
| **AI Gateway** | Yes (Multi-provider gateway, provider adapters, local fallback, hash cache)| Fine-tuned private LLMs |
| **Job Applications** | Strictly **NO** (Zero automated application) | Manual application assistant workflows |
| **LinkedIn Ingestion** | Strictly **NO** authenticated scraping | Official LinkedIn Job Search API (if granted) |
| **Infrastructure** | Single-node Modular Monolith + PostgreSQL | Kafka, Celery, Redis, Elasticsearch, Microservices |
