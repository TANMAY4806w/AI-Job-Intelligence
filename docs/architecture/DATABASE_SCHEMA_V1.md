# Database Schema Specification - V1

> **Document Type:** Relational Data Model & Persistence Architecture<br>
> **Status:** Approved Baseline<br>
> **Version:** 1.0<br>
> **Target Phase:** Phase 1 (Architecture & System Design)<br>
> **Parent Specifications:** [`SYSTEM_ARCHITECTURE_V1.md`](./SYSTEM_ARCHITECTURE_V1.md) | [`TECH_STACK_V1.md`](./TECH_STACK_V1.md) | [`../requirements/JOB_DATA_CONTRACT.md`](../requirements/JOB_DATA_CONTRACT.md)<br>
> **Rules & Constraints:** [`../../rules/JOB_FILTERS_V1.md`](../../rules/JOB_FILTERS_V1.md) | [`../../rules/DEDUPLICATION_V1.md`](../../rules/DEDUPLICATION_V1.md) | [`../../rules/VERIFICATION_V1.md`](../../rules/VERIFICATION_V1.md)

---

## 1. Architectural Persistence Principles

The data architecture for **AI Job Intelligence** balances relational rigor for canonical business entities with schemaless flexibility for raw external payloads. The design strictly enforces the project's zero-cost, high-recall, and auditability requirements.

### 1.1 The Evidence & Decision Triad
All data within the system is strictly partitioned into three conceptual layers:

```text
┌──────────────────────────┐       ┌──────────────────────────┐       ┌──────────────────────────┐
│       RAW EVIDENCE       │       │      CANONICAL DATA      │       │    DERIVED DECISIONS     │
│   (Immutable Ingestion)  │──────>│ (Deduplicated Entities)  │──────>│   (Audit & Evaluations)  │
├──────────────────────────┤       ├──────────────────────────┤       ├──────────────────────────┤
│ • raw_jobs (JSONB)       │       │ • companies              │       │ • verification_records   │
│ • job_source_references  │       │ • jobs                   │       │ • filter_decisions       │
│ • company_sources        │       │ • sources                │       │ • ai_analysis_results    │
└──────────────────────────┘       └──────────────────────────┘       └──────────────────────────┘
```

1. **Raw Evidence (Immutable Provenance):** External job board and ATS payloads are stored in their native JSON/HTML representations without alteration. This guarantees that parser refinements, schema changes, or bug fixes can re-process historical discoveries without re-scraping external endpoints.
2. **Canonical Data (Normalized State):** Canonical entities (`jobs`, `companies`, `sources`) provide clean, strongly typed representations of unique physical entities after normalization and deduplication.
3. **Derived Decisions (Auditable History):** Rule filtering outcomes, multi-dimensional verification records, and AI ambiguity resolutions are stored as append-only historical events. When rules or prompts are updated, historical evaluations remain intact, enabling reproducible regression testing and auditability.

### 1.2 Core Data Design Directives
- **Raw Payload Preservation:** `raw_jobs.raw_payload` is stored as PostgreSQL `JSONB`. Raw records are never destructively modified or overwritten.
- **Relational Integrity:** Foreign key constraints enforce referential integrity across companies, jobs, sources, and audit tables, preventing orphaned records.
- **Append-Only Auditing:** Verification events, filter evaluations, and AI inference runs are append-only. A job re-evaluated under a new rule version receives a new `filter_decisions` entry rather than overwriting historical evaluations.
- **No Premature Deletions:** Postings older than the active feed window (> 5 days) transition their operational status to `EXPIRED`, but their underlying records, source links, and audit trails remain permanently stored in the database.

---

## 2. Entity Relationship Overview

```text
┌─────────────────────────┐       ┌─────────────────────────┐       ┌─────────────────────────┐
│        companies        │       │         sources         │       │     company_sources     │
├─────────────────────────┤       ├─────────────────────────┤       ├─────────────────────────┤
│ id (PK: UUID)           │1     *│ id (PK: UUID)           │1     *│ id (PK: UUID)           │
│ name                    ├───────┤ source_name (UNIQUE)    ├───────┤ company_id (FK: UUID)   │
│ normalized_name (UNIQUE)│       │ source_type             │       │ source_id (FK: UUID)    │
│ website                 │       │ base_url                │       │ ats_identifier (NULLABLE)
│ sector                  │       │ is_active               │       │ career_url              │
│ business_model          │       │ rate_limit_rpm          │       │ api_endpoint            │
│ is_verified             │       │ requires_browser        │       │ confidence              │
└───────────┬─────────────┘       └────────────┬────────────┘       └────────────┬────────────┘
            │ 1                                │ 1                               │ 1 (Provenance)
            │                                  │                                 │
            │ *                                │ *                               │ *
┌───────────┴─────────────┐1     *┌────────────┴────────────┐*     1┌────────────┴────────────┐
│          jobs           ├───────┤  job_source_references  ├───────┤        raw_jobs         │
├─────────────────────────┤       ├─────────────────────────┤       ├─────────────────────────┤
│ id (PK: UUID)           │       │ id (PK: UUID)           │       │ id (PK: UUID)           │
│ company_id (FK: UUID)   │       │ job_id (FK: RESTRICT)   │       │ company_source_id (FK)  │
│ title                   │       │ raw_job_id (FK: UUID)   │       │ collection_run_id (FK)  │
│ normalized_title        │       │ source_id (FK: UUID)    │       │ source_job_id           │
│ normalized_location(NULL)│       │ source_job_id           │       │ source_job_id_type      │
│ eligibility_status      │       │ source_job_id_type      │       │ source_url              │
│ role_priority           │       │ source_url              │       │ raw_payload (JSONB)     │
│ verification_confidence │       │ application_url         │       │ content_hash (SHA256)   │
└───────────┬─────────────┘       │ extracted_data (JSONB)  │       │ collected_at            │
                                  │ is_primary              │       └─────────────────────────┘
                                  └─────────────────────────┘
            │ 1
            ├─────────────────────────────────┬─────────────────────────────────┐
            │ 1                               │ 1                               │ 1
            │ *                               │ *                               │ *
┌───────────┴─────────────┐       ┌───────────┴─────────────┐       ┌───────────┴─────────────┐
│  verification_records   │       │    filter_decisions     │       │   ai_analysis_results   │
├─────────────────────────┤       ├─────────────────────────┤       ├─────────────────────────┤
│ id (PK: UUID)           │       │ id (PK: UUID)           │       │ id (PK: UUID)           │
│ job_id (FK: RESTRICT)   │       │ job_id (FK: RESTRICT)   │       │ job_id (FK: SET NULL)   │
│ verification_run_at     │       │ rule_version            │       │ cache_key (UNIQUE)      │
│ jd_verified             │       │ final_status            │       │ provider                │
│ source_verified         │       │ role_priority           │       │ model                   │
│ company_verified        │       │ reason_codes (JSONB)    │       │ prompt_version          │
│ experience_verified     │       │ evaluation_context(JSONB│       │ input_hash              │
│ salary_verified         │       │ evaluated_at            │       │ structured_output(JSONB)│
│ location_verified       │       └─────────────────────────┘       │ confidence              │
│ audit_trail (JSONB)     │                                         │ tokens_used             │
└─────────────────────────┘                                         └─────────────────────────┘
```

---

## 3. Detailed Entity Specifications

### 3.1 `companies`
- **Purpose:** Represents canonical legal or operating corporate entities discovered through public registries, ATS career boards, or manual curation. Standardizes company names, domains, and business classifications across all collected job postings.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Canonical unique corporate identifier.
  - `name`: `VARCHAR(255)` — Reported display name of the employer (e.g., "Razorpay Software Private Limited").
  - `normalized_name`: `VARCHAR(255)` — Standardized, lowercased name stripped of legal entity suffixes (e.g., "razorpay").
  - `website`: `VARCHAR(512)` — Canonical corporate domain (e.g., "https://razorpay.com").
  - `industry`: `VARCHAR(128)` — Specific industry vertical (e.g., "FinTech", "HealthTech", "Enterprise SaaS").
  - `sector`: `VARCHAR(32)` — High-level sector classification: `IT`, `NON_IT`, or `UNKNOWN`. Default: `UNKNOWN`.
  - `business_model`: `VARCHAR(32)` — Operating model: `PRODUCT`, `SERVICES`, `BOTH`, or `UNKNOWN`. Default: `UNKNOWN`.
  - `is_verified`: `BOOLEAN` — True if company existence is confirmed via official domain or verified registry. Default: `false`.
  - `verification_source`: `VARCHAR(128)` — Verification origin (e.g., `OFFICIAL_DOMAIN`, `ATS_PORTAL`, `MANUAL_CURATION`).
  - `confidence`: `NUMERIC(3,2)` — Entity confidence score ($0.00 \le C \le 1.00$). Default: `0.50`.
  - `created_at`: `TIMESTAMPTZ` — Timestamp of initial record creation.
  - `updated_at`: `TIMESTAMPTZ` — Timestamp of last metadata update.
- **Nullability:**
  - *Required:* `id`, `name`, `normalized_name`, `sector`, `business_model`, `is_verified`, `confidence`, `created_at`, `updated_at`.
  - *Nullable:* `website`, `industry`, `verification_source`.
- **Uniqueness & Partial Unique Index Architecture:**
  - `UNIQUE (normalized_name)` — Prevents duplicate corporate records.
- **Important Indexes:**
  - `idx_companies_normalized_name` ON `normalized_name` (B-Tree) — Used for exact matching during company ingestion and deduplication.
  - `idx_companies_sector_model` ON `(sector, business_model)` (B-Tree) — Used for cohort analysis and filtering audits.
  - `idx_companies_verified` ON `is_verified` (B-Tree) — Used for targeted company verification sweeps.
- **Relationships:**
  - One-to-many with `company_sources` (`companies.id -> company_sources.company_id`).
  - One-to-many with `jobs` (`companies.id -> jobs.company_id`).
- **Lifecycle Expectations:**
  - Created during company discovery sweeps or manual seeding.
  - Remains persistent indefinitely; never deleted when individual job vacancies expire.

### 3.1.1 Company Stub Policy for Unverified Employers
- **The Referential Integrity & Anti-Collision Mandate:**
  Jobs require a non-null `company_id` to establish relational integrity, facilitate company-level querying, and drive Stage 3 composite deduplication. However, external aggregators and raw postings frequently reference new, unvetted, or unverified hiring entities.
  > [!CRITICAL]
  > **Zero Generic Merging & Anti-Collision Mandate:** The system **must NEVER** merge unrelated unidentified employers into a single generic "Unknown Company" record, nor may it merge unrelated employers that happen to share identical or ambiguous generic names (e.g., *"Stealth Startup"*, *"Confidential"*, *"Top MNC"*, *"Client of Recruiter"*). If multiple distinct employers shared a single `company_id`, Stage 3 deduplication `(company_id, normalized_title, normalized_location)` would cause independent jobs at completely different companies to falsely collide and merge.
- **Two-Tiered Stub Creation Protocol:**
  To prevent company directory fragmentation while strictly avoiding false collisions, the system distinguishes between *identifiable employers* and *generic or ambiguous employer identities*:
  1. **Identifiable Employers (Stable Company-Level Evidence Available):**
     - When an unverified employer has stable company-level evidence (such as an extracted corporate website domain, an official career page URL host, or a distinct non-generic company name like *"Acme Robotics Private Limited"*):
     - The system **must NOT** create a separate company record for every job vacancy. Instead, multiple openings from the same identifiable employer share a single stable company stub:
       - *Domain-anchored:* If an official domain or career host is detected:
         $$\text{normalized\_name} = \text{"stub-comp-" } + \text{SHA256}(\text{normalized\_domain})[:16]$$
       - *Source + Name-anchored:* If a distinct non-generic company name is present without a verified domain:
         $$\text{normalized\_name} = \text{"stub-comp-" } + \text{SHA256}(\text{source\_id} + \text{":"} + \text{normalized\_company\_name})[:16]$$
     - This links all jobs from that identifiable employer to a single `companies.id`, preserving relational integrity and company-level queryability without table bloat.
  2. **Generic or Ambiguous Employer Identities (Isolated by Posting):**
     - When an employer identity is masked, placeholder, or ambiguous (e.g., *"Stealth Startup"*, *"Confidential"*, *"Top MNC"*, *"Leading IT Client"*, *"Client of Recruiter"*, or unstated):
     - Company-level grouping is strictly forbidden, as multiple completely unrelated employers utilize identical placeholder terms.
     - The system isolates generic or ambiguous employer identities by posting, assigning each posting its own discrete stub:
       $$\text{normalized\_name} = \text{"stub-post-" } + \text{SHA256}(\text{source\_id} + \text{":"} + \text{source\_job\_id} + \text{":"} + \text{raw\_name})[:16]$$
     - Each posting from a generic or anonymous employer maintains an isolated stub record in `companies`, preventing unrelated postings from falsely merging in Stage 3 deduplication.
  3. **Explicit Uncertainty State:** Every stub record is initialized with:
     - `is_verified = false`
     - `confidence = 0.50` (or `0.00` if purely unstated)
     - `verification_source = 'UNVERIFIED_INGESTION_STUB'`
     - `sector = 'UNKNOWN'`, `business_model = 'UNKNOWN'`
  4. **Job Verification Reflection:** Postings referencing any unverified stub receive `company_verified = false` and transition to `eligibility_status = 'REVIEW'` with `review_reason = 'COMPANY_UNVERIFIED'` (if company verification is required or ambiguous).
  5. **Strict Evidence-Based Merging Policy:**
     - Stubs must **NEVER** be merged automatically based on fuzzy text matching or identical unverified names alone.
     - Merging is permitted **ONLY** when reliable, verifiable corroborating evidence confirms company identity:
       a) **Authoritative Domain Match:** Confirmed DNS-verified official corporate domain or validated company email host.
       b) **Verified ATS Board Resolution:** Both endpoints resolve to the exact same verified ATS board token (`company_sources.ats_identifier`).
       c) **Manual Human Verification:** Explicit manual review and verification by the system operator.
     - Once confirmed, a formal database alias migration merges the stub references into the canonical verified company record, updating `jobs.company_id` and logging an auditable merge entry.

---

### 3.2 `sources`
- **Purpose:** Master catalog of all supported external collection platforms, direct ATS software engines, and job portals. Governs platform capabilities, default crawl parameters, and rate limits.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique platform identifier.
  - `source_name`: `VARCHAR(64)` — Platform enum/token: `GREENHOUSE`, `LEVER`, `ASHBY`, `WORKABLE`, `CAREER_PAGE`, `NAUKRI`, `INDEED`.
  - `source_type`: `VARCHAR(32)` — Channel category: `ATS_API`, `DIRECT_PORTAL`, `PUBLIC_JOB_BOARD`.
  - `base_url`: `VARCHAR(512)` — Base URI pattern or API endpoint root for the platform.
  - `is_active`: `BOOLEAN` — Operational flag toggling ingestion for this source. Default: `true`.
  - `rate_limit_rpm`: `INTEGER` — Maximum requests per minute permitted for polite crawling. Default: `60`.
  - `requires_browser`: `BOOLEAN` — True if source requires Playwright headless rendering; false for static HTTP/JSON. Default: `false`.
  - `created_at`: `TIMESTAMPTZ` — Timestamp when source was registered.
  - `updated_at`: `TIMESTAMPTZ` — Timestamp of last configuration update.
- **Nullability:**
  - *Required:* `id`, `source_name`, `source_type`, `is_active`, `rate_limit_rpm`, `requires_browser`, `created_at`, `updated_at`.
  - *Nullable:* `base_url`.
- **Uniqueness & Partial Unique Index Architecture:**
  - `UNIQUE (source_name)` — Ensures each platform has exactly one registered configuration.
- **Important Indexes:**
  - `idx_sources_active` ON `(is_active, source_type)` (B-Tree) — Used by schedulers to dispatch active collector modules.
- **Relationships:**
  - One-to-many with `company_sources` (`sources.id -> company_sources.source_id`).
  - Indirect one-to-many with `raw_jobs` via `company_sources` (`sources.id -> company_sources.source_id -> raw_jobs.company_source_id`).
  - One-to-many with `job_source_references` (`sources.id -> job_source_references.source_id`).
  - One-to-many with `collection_runs` (`sources.id -> collection_runs.source_id`).
- **Lifecycle Expectations:**
  - Seeded during system initialization; updated when new ATS integrations or ingestion pipelines are deployed.

---

### 3.3 `company_sources`
- **Purpose:** Maps specific companies to their detected ATS instances, career board tokens, and endpoints. Records how and when each career endpoint was discovered, its operational health, and confidence.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `company_id` REFERENCES `companies(id)` ON DELETE RESTRICT
  - `source_id` REFERENCES `sources(id)` ON DELETE RESTRICT
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique mapping identifier.
  - `company_id`: `UUID` — Foreign key referencing the canonical company.
  - `source_id`: `UUID` — Foreign key referencing the platform/ATS source.
  - `ats_identifier`: `VARCHAR(128)` — Provider-specific external identifier or slug (e.g., "stripe" for Greenhouse, "cred" for Lever). Nullable for generic career pages without external board tokens.
  - `career_url`: `TEXT` — Direct public web URL to the employer's career page.
  - `api_endpoint`: `TEXT` — Direct structured public API endpoint discovered for job harvesting.
  - `discovery_method`: `VARCHAR(64)` — Discovery provenance: `DIRECT_PROBE`, `COMMON_CRAWL`, `SITEMAP`, `DNS_LOOKUP`, `MANUAL_SEED`.
  - `confidence`: `NUMERIC(3,2)` — Discovery confidence score ($0.00 \le C \le 1.00$). Default: `1.00` for direct probe/seed.
  - `is_active`: `BOOLEAN` — Operational status of the endpoint. Default: `true`.
  - `last_discovered_at`: `TIMESTAMPTZ` — Timestamp when endpoint was first detected or confirmed active.
  - `last_collected_at`: `TIMESTAMPTZ` — Timestamp of the most recent collection run against this endpoint.
  - `created_at`: `TIMESTAMPTZ` — Record creation timestamp.
  - `updated_at`: `TIMESTAMPTZ` — Timestamp of last configuration modification.
- **Semantic Rules for Endpoint Identity:**
  - **ATS / API Source:** `ats_identifier` is normally expected (e.g., Lever company slug, Greenhouse board token, Ashby board name).
  - **Generic Career Page Source:** `ats_identifier` may be `NULL`.
  - **Endpoint Identification:** `career_url` and/or `api_endpoint` identify the endpoint where appropriate.
- **Nullability:**
  - *Required:* `id`, `company_id`, `source_id`, `discovery_method`, `confidence`, `is_active`, `created_at`, `updated_at`.
  - *Nullable:* `ats_identifier`, `career_url`, `api_endpoint`, `last_discovered_at`, `last_collected_at`.
- **Uniqueness & Partial Unique Index Architecture:**
  - `CREATE UNIQUE INDEX uq_company_sources_ats ON company_sources (company_id, source_id, ats_identifier) WHERE ats_identifier IS NOT NULL;` — Enforces unique board tokens/slugs per company and ATS source.
  - `CREATE UNIQUE INDEX uq_company_sources_career_url ON company_sources (company_id, source_id, career_url) WHERE ats_identifier IS NULL AND career_url IS NOT NULL;` — Enforces unique career page URLs for generic sources lacking ATS identifiers.
  - `CREATE UNIQUE INDEX uq_company_sources_api_endpoint ON company_sources (company_id, source_id, api_endpoint) WHERE ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NOT NULL;` — Enforces uniqueness for generic endpoints defined strictly by API URLs.
  - `CREATE UNIQUE INDEX uq_company_sources_bare_mapping ON company_sources (company_id, source_id) WHERE ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NULL;` — Ensures that unconfigured mappings (where all locator URLs are missing) cannot be duplicated for the same company and source.
  - Table Check Constraint: `CHECK (ats_identifier IS NOT NULL OR career_url IS NOT NULL OR api_endpoint IS NOT NULL)` ensures an active endpoint defines at least one locator.
- **Important Indexes:**
  - `idx_company_sources_lookup` ON `(company_id, source_id)` (B-Tree) — Fast lookup of an employer's configured endpoints.
  - `idx_company_sources_ats` ON `(source_id, ats_identifier) WHERE ats_identifier IS NOT NULL` (B-Tree) — Used during reverse lookup when an ATS feed is parsed.
  - `idx_company_sources_schedule` ON `(is_active, last_collected_at)` (B-Tree) — Used by the scheduler to select stale endpoints for harvesting.
- **Relationships:**
  - Many-to-one with `companies` and `sources`.
  - One-to-many with `raw_jobs` (`company_sources.id -> raw_jobs.company_source_id`).
  - One-to-many with `collection_runs` (`company_sources.id -> collection_runs.company_source_id`).
- **Lifecycle Expectations:**
  - Created by discovery workers or manual seeding. Updated periodically as harvest runs record `last_collected_at`.

---

### 3.4 `raw_jobs`
- **Purpose:** Immutable append-only store preserving external source responses exactly as ingested. Protects the pipeline against data loss, enabling complete re-parsing and rule backtesting without re-crawling. Primary ownership is anchored to `company_sources` (`company_source_id`) to identify the exact company/source endpoint configuration that produced the record, while preserving the ability to cleanly derive `company_id` and `source_id` without losing platform provenance.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `company_source_id` REFERENCES `company_sources(id)` ON DELETE RESTRICT
  - `collection_run_id` REFERENCES `collection_runs(id)` ON DELETE SET NULL
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique raw ingestion record ID.
  - `company_source_id`: `UUID` — Foreign key referencing the specific company source / ATS endpoint configuration (`company_sources(id)`). Identifies the exact company board or career endpoint that produced this record.
  - `collection_run_id`: `UUID` — Foreign key referencing the collection batch run that ingested this record.
  - `source_job_id`: `VARCHAR(255)` — External job identifier provided by the external system (or deterministic `"gen_<hash>"` for generic career pages lacking native IDs).
  - `source_job_id_type`: `VARCHAR(20)` — External identifier classification: `'NATIVE'` (structured ATS identifier provided by platform) or `'GENERATED'` (deterministic hash for generic career pages lacking native IDs). Default: `'NATIVE'`.
  - `source_url`: `TEXT` — Exact URL where this posting was fetched.
  - `raw_payload`: `JSONB` — Un-mutated, full external payload (JSON response or structured HTML text payload).
  - `content_hash`: `CHAR(64)` — SHA256 hexadecimal hash of the serialized raw payload for instant duplicate payload detection.
  - `http_status`: `INTEGER` — HTTP status code returned during capture (e.g., 200).
  - `collected_at`: `TIMESTAMPTZ` — Timestamp when this raw record was captured into the database (ingestion metadata only; does NOT serve as freshness basis).
- **Derived Provenance:**
  - `company_id`: Derived via `company_sources.company_id`.
  - `source_id`: Derived via `company_sources.source_id`.
  - `source_name`: Derived via `company_sources -> sources.source_name`.
- **Nullability:**
  - *Required:* `id`, `company_source_id`, `source_job_id`, `source_job_id_type`, `source_url`, `raw_payload`, `content_hash`, `collected_at`.
  - *Nullable:* `collection_run_id`, `http_status`.
- **Uniqueness & Partial Unique Index Architecture:**
  - Append-only design. No restrictive database-level unique constraint on `(company_source_id, source_job_id)` in `raw_jobs`, ensuring recurring daily observations of the same job on a configured endpoint are permanently recorded as temporal evidence.
- **Important Indexes:**
  - `idx_raw_jobs_comp_src_ext` ON `(company_source_id, source_job_id)` (B-Tree) — Rapidly retrieves historical payloads for a given company endpoint and external job ID.
  - `idx_raw_jobs_content_hash` ON `content_hash` (B-Tree) — Enables sub-millisecond detection of byte-for-byte identical raw payloads.
  - `idx_raw_jobs_collected_at` ON `collected_at` (B-Tree) — Supports time-window partitioning and audit queries.
  - `idx_raw_jobs_run` ON `collection_run_id` (B-Tree) — Facilitates batch telemetry analysis for a specific collection run.
  - *JSONB Indexing Note:* A GIN index is **NOT** applied indiscriminately across all `raw_payload` data. Instead, extraction code reads top-level keys directly, avoiding multi-gigabyte index bloat on high-volume raw JSON.
- **Relationships:**
  - Many-to-one with `company_sources` (`company_sources.id -> raw_jobs.company_source_id`) and `collection_runs`.
  - Transitive provenance access to `companies` and `sources` via `company_sources`.
  - One-to-many with `job_source_references` (`raw_jobs.id -> job_source_references.raw_job_id`).
- **Payload Immutability & Dedicated Metadata Architecture:**
  - `raw_payload` is stored as an immutable, byte-for-byte exact JSONB representation of the server response.
  - Ingestion metadata such as `source_job_id_type`, `collected_at`, and `content_hash` are stored exclusively in dedicated table columns. The pipeline **never** mutates or injects synthetic properties into `raw_payload`.
- **Lifecycle Expectations:**
  - Strictly append-only. Never updated or destructively pruned during normal operations.

---

### 3.5 `jobs`
- **Purpose:** The canonical, normalized representation of a unique job vacancy after multi-source normalization and deduplication. Serves as the single source of truth for the user's curated job feed and filter engine.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `company_id` REFERENCES `companies(id)` ON DELETE RESTRICT
- **Key Fields & Conceptual Types:**
  - **Identity & Association:**
    - `id`: `UUID` — Canonical job identifier.
    - `company_id`: `UUID` — Foreign key referencing the canonical hiring company.
    - `company_name`: `VARCHAR(255)` — Denormalized company display name (stored for query speed and feed presentation).
  - **Normalized Core Attributes:**
    - `title`: `VARCHAR(255)` — Raw reported job title (e.g., "Junior AI / Backend Engineer - Python").
    - `normalized_title`: `VARCHAR(255)` — Standardized title for matching and classification (e.g., "ai backend engineer").
    - `location`: `VARCHAR(255)` — Reported location string (e.g., "Bengaluru, Karnataka, India"). Nullable when missing or ambiguous in source text.
    - `normalized_location`: `VARCHAR(255)` — Standardized canonical city/tier string (e.g., "Bengaluru"). Nullable if location cannot be deterministically resolved.
    - `employment_type`: `VARCHAR(32)` — `FULL_TIME`, `PART_TIME`, `CONTRACT`, `INTERNSHIP`, `UNKNOWN`.
    - `remote_type`: `VARCHAR(32)` — `ON_SITE`, `HYBRID`, `REMOTE`, `UNKNOWN`. Default: `UNKNOWN`.
  - **Job Content:**
    - `description`: `TEXT` — Full-text job description body.
    - `responsibilities`: `JSONB` — Structured array of parsed duty bullet points (`["Build FastAPI backend", ...]`).
    - `requirements`: `JSONB` — Structured array of explicit prerequisites.
    - `skills`: `JSONB` — Array of extracted or stated technologies/skills (`["Python", "PostgreSQL", "Docker"]`).
  - **Experience & Compensation Extraction:**
    - `experience_raw`: `VARCHAR(128)` — Stated experience text (e.g., "0-2 years", "Fresher", "1 to 3 yrs").
    - `experience_min_years`: `NUMERIC(3,1)` — Extracted numeric minimum experience in years (e.g., `0.0`, `1.0`).
    - `experience_max_years`: `NUMERIC(3,1)` — Extracted numeric maximum experience in years (e.g., `2.0`, `3.0`).
    - `salary_min`: `NUMERIC(12,2)` — Stated minimum compensation value. Null if unstated.
    - `salary_max`: `NUMERIC(12,2)` — Stated maximum compensation value. Null if unstated.
    - `salary_currency`: `VARCHAR(3)` — ISO-4217 currency code (`INR`, `USD`). Default: `INR`.
    - `salary_period`: `VARCHAR(16)` — Compensation cadence: `ANNUAL`, `MONTHLY`, `HOURLY`, `UNKNOWN`.
  - **Regional Salary Threshold Policies:**
    - **Mumbai / Navi Mumbai / Thane:** No strict minimum threshold required.
    - **Pune:** Minimum ₹30,000 / month (or equivalent ₹3.6 LPA). Stated compensation below this: `REJECTED` (`rejection_reason = SALARY_BELOW_MINIMUM`).
    - **Bengaluru:** Minimum ₹5.0 LPA. Stated compensation below this: `REJECTED` (`rejection_reason = SALARY_BELOW_MINIMUM`).
    - **Hyderabad:** Minimum ₹5.0 LPA. Stated compensation below this: `REJECTED` (`rejection_reason = SALARY_BELOW_MINIMUM`).
    - **Unknown Salary Protocol:** If compensation is undisclosed or cannot be reliably extracted, the job is **never** automatically rejected. It transitions to `KEEP`/`REVIEW` with `salary_verified = false` and `review_reason = SALARY_UNKNOWN`.
  - **Temporal & Application Tracking:**
    - `posted_at`: `TIMESTAMPTZ` — Earliest reliable original publication timestamp reported by sources. Primary field used for the >5 day active-feed expiration decision. NULL when original publication date is unknown or unparseable.
    - `collected_at`: `TIMESTAMPTZ` — Timestamp when this opening was first captured into the database. Ingestion metadata only; serves solely as the provisional fallback specified by the frozen verification rule (Dimension 8) and must never alone establish active-feed eligibility or freshness.
  - **Freshness & Missing Publication Date Protocol:**
    - Active-feed freshness is evaluated **strictly** against original publication date `posted_at` (0–24h, 1–3d, 3–5d).
    - When the original publication date is unknown, unstated, or invalid:
      1. `posted_at` remains `NULL` in the canonical database schema (it is never overwritten with ingestion dates).
      2. Per the frozen `VERIFICATION_V1.md` rule (Dimension 8), `collected_at` serves solely as a provisional operational fallback for relative plausibility checks, and the job receives `review_reason = 'POSTED_DATE_SUSPICIOUS'`.
      3. The job **must remain in `REVIEW`** with `review_reason = 'POSTED_DATE_SUSPICIOUS'` until publication-date validity is established.
      4. `collected_at` alone is ingestion metadata and must **never** make a job eligible or establish that it is within the 5-day freshness window.
      5. Late-discovered jobs with verified original publication dates older than 5 days remain `EXPIRED` (`rejection_reason = 'JOB_TOO_OLD'`) and are excluded from the active feed.
    - `updated_at`: `TIMESTAMPTZ` — Timestamp of last canonical update or status evaluation.
    - `application_url`: `TEXT` — Direct canonical ATS application endpoint or official career link.
    - `application_status`: `VARCHAR(32)` — User workflow state: `NOT_APPLIED`, `SAVED`, `APPLIED_MANUALLY`, `IN_REVIEW`. Default: `NOT_APPLIED`.
  - **Classification & Operational Eligibility:**
    - `role_priority`: `VARCHAR(16)` — Priority tier: `P1`, `P2`, `P3`, `NOT_RELEVANT`.
    - `relevance_status`: `VARCHAR(16)` — Alignment category: `TARGET`, `ADJACENT`, `EXCLUDED`.
    - `eligibility_status`: `VARCHAR(32)` — Frozen operational lifecycle state: `NEW`, `ELIGIBLE`, `REVIEW`, `REJECTED`, `DUPLICATE`, `EXPIRED`, `VERIFICATION_FAILED`. Default: `NEW`.
    - `rejection_reason`: `VARCHAR(64)` — Frozen reason code if rejected (e.g., `EXPERIENCE_TOO_HIGH`, `LOCATION_NOT_ALLOWED`, `EXCLUDED_ROLE`, `JOB_TOO_OLD`, `SALARY_BELOW_MINIMUM`).
    - `review_reason`: `VARCHAR(64)` — Frozen reason code if routed to review (e.g., `SALARY_UNKNOWN`, `EXPERIENCE_AMBIGUOUS`, `LOCATION_AMBIGUOUS`, `LOCATION_REMOTE_REVIEW`, `SOURCE_CONFLICT_REVIEW`, `COMPANY_UNVERIFIED`, `POSTED_DATE_SUSPICIOUS`).
  - **Verification Composite Metrics:**
    - `source_verified`: `BOOLEAN` — True if origin platform is verified.
    - `jd_verified`: `BOOLEAN` — True if authentic JD content exists (>100 chars, not dead/tombstone).
    - `company_verified`: `BOOLEAN` — True if hiring entity is legitimate.
    - `location_verified`: `BOOLEAN` — True if location matches target criteria or remote clause.
    - `experience_verified`: `BOOLEAN` — True if stated experience aligns with 0–2 yr target policy.
    - `salary_verified`: `BOOLEAN` — True if compensation is explicitly disclosed and valid (false if unstated).
    - `verification_confidence`: `NUMERIC(3,2)` — Composite confidence metric ($0.00 \le C \le 1.00$).
- **Nullability:**
  - *Required:* `id`, `company_id`, `company_name`, `title`, `normalized_title`, `description`, `collected_at`, `updated_at`, `application_status`, `role_priority`, `relevance_status`, `eligibility_status`, `source_verified`, `jd_verified`, `company_verified`, `location_verified`, `experience_verified`, `salary_verified`, `verification_confidence`.
  - *Nullable:* `location`, `normalized_location`, `employment_type`, `remote_type`, `responsibilities`, `requirements`, `skills`, `experience_raw`, `experience_min_years`, `experience_max_years`, `salary_min`, `salary_max`, `salary_currency`, `salary_period`, `posted_at`, `application_url`, `rejection_reason`, `review_reason`.
  - **Missing & Ambiguous Location Protocol:**
    - In accordance with the Maximum Recall Principle, an opening with an unstated, missing, or ambiguous location is **never** assigned fabricated dummy values (such as `"UNKNOWN"`), nor is it discarded.
    - When location cannot be deterministically resolved: `location` retains the raw extracted text (or `NULL` if completely absent), `normalized_location` is stored as `NULL`, `location_verified` is set to `false`, and the record transitions to `eligibility_status = 'REVIEW'` with `review_reason = 'LOCATION_AMBIGUOUS'`.
- **Important Indexes:**
  - `idx_jobs_eligibility_priority` ON `(eligibility_status, role_priority)` (B-Tree) — Primary query index for user job feed.
  - `idx_jobs_posted_at` ON `posted_at DESC NULLS LAST` (B-Tree) — Feeds freshness ordering (0–24h, 1–3d, 3–5d).
  - `idx_jobs_company_id` ON `company_id` (B-Tree) — Fast join to company records and company-specific job queries.
  - `idx_jobs_dedup_composite` ON `(company_id, normalized_title, normalized_location)` (B-Tree) — Accelerates Stage 3 composite deduplication lookups. In PostgreSQL, B-Trees store and index `NULL` values. For postings where `normalized_location IS NULL`, deduplication queries match on `company_id` and `normalized_title` while handling `NULL` location comparison explicitly (`normalized_location IS NULL` or `IS NOT DISTINCT FROM`).
  - `idx_jobs_verification` ON `(verification_confidence, jd_verified)` (B-Tree) — Filter queries prioritizing high-confidence verified postings.
- **Relationships:**
  - Many-to-one with `companies`.
  - One-to-many with `job_source_references`, `verification_records`, `filter_decisions`, `ai_analysis_results`.
- **Lifecycle Expectations:**
  - Instantiated as `NEW` during normalization.
  - Evaluated deterministically to `ELIGIBLE`, `REVIEW`, or `REJECTED`.
  - Transitions to `EXPIRED` automatically via scheduled sweeps when original publication date (`posted_at`) exceeds 5 days (`rejection_reason = JOB_TOO_OLD`).
  - **Freshness & Expiration Clarification:** An old posting discovered late is permanently preserved as historical evidence, but if its original publication date (`posted_at`) is >5 days old, it is marked `EXPIRED` and never enters the active feed. `collected_at` records ingestion timing only, not the freshness basis. Expired records are never deleted.

---

### 3.6 `job_source_references`
- **Purpose:** Connects one canonical `jobs` record to one or more platform discovery events (`raw_jobs`). Preserves complete source provenance, platform-specific URLs, and handles cross-source attribute conflicts.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `job_id` REFERENCES `jobs(id)` ON DELETE RESTRICT
  - `raw_job_id` REFERENCES `raw_jobs(id)` ON DELETE RESTRICT
  - `source_id` REFERENCES `sources(id)` ON DELETE RESTRICT
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique reference link identifier.
  - `job_id`: `UUID` — Foreign key referencing the canonical job.
  - `raw_job_id`: `UUID` — Foreign key referencing the immutable raw payload.
  - `source_id`: `UUID` — Foreign key referencing the originating platform.
  - `source_job_id`: `VARCHAR(255)` — External identifier on this platform.
  - `source_job_id_type`: `VARCHAR(20)` — External identifier classification matching `raw_jobs.source_job_id_type`: `'NATIVE'` or `'GENERATED'`. Default: `'NATIVE'`.
  - `source_url`: `TEXT` — Discoverable public link on this platform.
  - `application_url`: `TEXT` — Direct application URL extracted specifically from this source.
  - `extracted_data`: `JSONB` — Snapshot of source-specific extracted values (salary, experience, location) enabling cross-source conflict detection.
  - `posted_at`: `TIMESTAMPTZ` — Publication date reported specifically by this source.
  - `collected_at`: `TIMESTAMPTZ` — Timestamp when this source discovered this opening.
  - `is_primary`: `BOOLEAN` — True if this reference provided the primary canonical description (e.g., direct ATS has precedence over aggregator). Default: `false`.
- **Nullability:**
  - *Required:* `id`, `job_id`, `raw_job_id`, `source_id`, `source_job_id`, `source_job_id_type`, `source_url`, `collected_at`, `is_primary`.
  - *Nullable:* `application_url`, `extracted_data`, `posted_at`.
- **Uniqueness & Partial Unique Index Architecture:**
  - `UNIQUE (source_id, source_job_id)` — Enforces that an identical platform job ID is mapped only once across canonical jobs.
- **Important Indexes:**
  - `idx_job_source_ref_job` ON `job_id` (B-Tree) — Rapidly retrieves all sources corroborating a canonical job.
  - `idx_job_source_ref_raw` ON `raw_job_id` (B-Tree) — Traverses from raw payload back to canonical reference.
  - `idx_job_source_ref_app_url` ON `application_url` (B-Tree) — Used for Stage 2 ATS application endpoint deduplication.
- **Relationships:**
  - Many-to-one with `jobs`, `raw_jobs`, and `sources`.
- **Lifecycle Expectations:**
  - Created whenever a job is newly ingested or whenever an incoming job matches an existing canonical record during deduplication.

### 3.6.1 Deterministic Source Identifier Strategy & Type Storage
- **Dedicated Storage of Identifier Type (`source_job_id_type`):**
  To maintain explicit classification without compromising payload immutability:
  1. **`raw_jobs.source_job_id_type` (`VARCHAR(20) NOT NULL`):** Explicitly stores `'NATIVE'` or `'GENERATED'` alongside `source_job_id`. This guarantees that `raw_payload` remains strictly immutable and unpolluted by synthetic pipeline metadata.
  2. **`job_source_references.source_job_id_type` (`VARCHAR(20) NOT NULL`):** Mirrors the identical identifier classification as a first-class column, maintaining schema uniformity across raw evidence and canonical provenance records (and is additionally preserved in the `extracted_data` snapshot for conflict auditing).
- **Native vs. Generated Identifiers:**
  - **Native Platform IDs (`source_job_id_type = 'NATIVE'`):** Structured ATS endpoints (Greenhouse, Lever, Ashby) provide persistent native identifiers (e.g., `gh_jid="12345"`, `lever_id="abc-123"`). These are stored directly in `source_job_id`.
  - **Deterministic Generated IDs (`source_job_id_type = 'GENERATED'`):** Generic corporate career pages (static HTML, DOM crawls) or scrapers lacking a discrete external job ID generate a deterministic hash identifier:
    $$\text{source\_job\_id} = \text{"gen\_"} + \text{SHA256}(\text{normalized\_canonical\_url})[:32]$$
    The explicit `"gen_"` prefix distinguishes generated synthetic IDs from native platform keys.
- **URL Normalization Mandate:**
  To prevent cosmetic link variations from mutating generated IDs:
  - All tracking query parameters (`utm_*`, `ref`, `gh_src`, session tokens, campaign IDs) are stripped.
  - Schemes (`https`), hosts, and path slashes are standardized (lowercased host, stripped trailing slash).
- **Resilience Against URL Changes & Migrations:**
  - If an employer alters its career page URL structure (e.g., migrating `/careers/job/101` $\to$ `/jobs/openings/python-engineer`):
    - A new generated `source_job_id` will be produced, meaning Stage 1 exact source ID lookup (`idx_job_source_ref_source_ext`) will treat it as a new discovery observation.
    - However, **Stage 3 Composite Deduplication** `(company_id, normalized_title, normalized_location)` and **Stage 4 Text Overlap** will match the posting to the existing canonical `Job`.
    - Rather than creating an orphaned duplicate job, the system merges the new discovery as an additional corroborating `JobSourceReference` on the existing `Job`, recording the updated URL while preserving full provenance history.

---

### 3.7 `verification_records`
- **Purpose:** Stores the detailed evaluation outcomes of each independent verification dimension. Preserves repeated verification attempts (e.g., initial discovery, link re-verification, weekly active check) rather than overwriting history.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `job_id` REFERENCES `jobs(id)` ON DELETE RESTRICT
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique verification event ID.
  - `job_id`: `UUID` — Foreign key referencing the canonical job.
  - `verification_run_at`: `TIMESTAMPTZ` — Timestamp of the verification execution.
  - **Dimension Check Outcomes (Boolean Flags):**
    - `source_verified`: `BOOLEAN` — Platform legitimacy check.
    - `jd_verified`: `BOOLEAN` — Actual JD existence (>100 chars, not 404/tombstone).
    - `application_url_verified`: `BOOLEAN` — Reachability check on direct ATS endpoint.
    - `company_verified`: `BOOLEAN` — Legal entity authenticity check.
    - `location_verified`: `BOOLEAN` — Stated location consistency with target cities/remote.
    - `experience_verified`: `BOOLEAN` — Experience consistency with 0–2 yr policy.
    - `salary_verified`: `BOOLEAN` — Stated compensation plausibility.
    - `posting_date_verified`: `BOOLEAN` — Publication date plausibility check.
  - **Aggregate Metrics & Audit:**
    - `verification_confidence`: `NUMERIC(3,2)` — Weighted confidence score ($0.00 \le C \le 1.00$).
    - `passed`: `BOOLEAN` — True if critical dimensions (JD authenticity, source validity) passed.
    - `failure_reasons`: `JSONB` — Array of hard technical failure codes (`["JD_NOT_FOUND", ...]`).
    - `review_reasons`: `JSONB` — Array of ambiguity codes (`["SALARY_UNKNOWN", "EXPERIENCE_AMBIGUOUS"]`).
    - `audit_trail`: `JSONB` — Granular diagnostic details (HTTP status codes, matched regex patterns, text snippet evidence).
- **Nullability:**
  - *Required:* `id`, `job_id`, `verification_run_at`, `source_verified`, `jd_verified`, `application_url_verified`, `company_verified`, `location_verified`, `experience_verified`, `salary_verified`, `posting_date_verified`, `verification_confidence`, `passed`, `audit_trail`.
  - *Nullable:* `failure_reasons`, `review_reasons`.
- **Uniqueness & Partial Unique Index Architecture:**
  - Append-only. No uniqueness constraint; multiple verification records exist per job over time.
- **Important Indexes:**
  - `idx_verification_job_time` ON `(job_id, verification_run_at DESC)` (B-Tree) — Retrieves latest verification status.
  - `idx_verification_confidence` ON `verification_confidence` (B-Tree) — Supports auditing of low-confidence postings.
- **Relationships:**
  - Many-to-one with `jobs`.
- **Lifecycle Expectations:**
  - Appended during initial ingestion and refreshed during periodic link verification sweeps.

---

### 3.8 `filter_decisions`
- **Purpose:** Immutable audit record documenting the execution of deterministic filtering business rules against a job posting. Tracks the exact rule version, assigned priority, eligibility status, and machine-readable reason codes.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `job_id` REFERENCES `jobs(id)` ON DELETE RESTRICT
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique evaluation decision ID.
  - `job_id`: `UUID` — Foreign key referencing the evaluated canonical job.
  - `rule_version`: `VARCHAR(32)` — Rule set identifier (e.g., `V1.0`, `V1.1-exp-fix`).
  - `final_status`: `VARCHAR(32)` — Outcome: `ELIGIBLE`, `REVIEW`, `REJECTED`, `EXPIRED`.
  - `role_priority`: `VARCHAR(16)` — Assigned priority: `P1`, `P2`, `P3`, `NOT_RELEVANT`.
  - `reason_codes`: `JSONB` — Array of assigned machine-readable reason codes (`["EXPERIENCE_AMBIGUOUS", "SALARY_UNKNOWN"]`).
  - `evaluation_context`: `JSONB` — Key-value snapshot of evaluated inputs (normalized title, detected experience band, city, salary values).
  - `evaluated_at`: `TIMESTAMPTZ` — Timestamp when rule engine executed.
- **Nullability:**
  - *Required:* `id`, `job_id`, `rule_version`, `final_status`, `role_priority`, `reason_codes`, `evaluation_context`, `evaluated_at`.
- **Uniqueness & Partial Unique Index Architecture:**
  - Append-only. A single job evaluated under multiple rule versions over its lifetime will retain a distinct record for each evaluation.
- **Important Indexes:**
  - `idx_filter_decisions_job_time` ON `(job_id, evaluated_at DESC)` (B-Tree) — Fast retrieval of current decision.
  - `idx_filter_decisions_version_status` ON `(rule_version, final_status)` (B-Tree) — Supports rule performance auditing and regression analysis.
- **Relationships:**
  - Many-to-one with `jobs`.
- **Lifecycle Expectations:**
  - Created during initial rule filtering and re-appended whenever rule sets are updated and backtested.

---

### 3.9 `ai_analysis_results`
- **Purpose:** Key-value extraction cache and audit store for semantic LLM operations. Encapsulates structured outputs, provider details, token telemetry, and composite content hashes to eliminate redundant API calls and enable seamless provider swapping.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `job_id` REFERENCES `jobs(id)` ON DELETE SET NULL (nullable for unassigned cache hits; preserves cached extractions if a job entity is detached or deleted)
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique extraction record ID.
  - `job_id`: `UUID` — Foreign key referencing the associated canonical job (if linked).
  - `cache_key`: `CHAR(64)` — Deterministic composite hash: `SHA256(JD Content + prompt_version + model_name)`.
  - `provider`: `VARCHAR(64)` — LLM provider adapter invoked (e.g., `gemini`, `groq`, `openrouter`, `ollama`).
  - `model`: `VARCHAR(64)` — Exact model identifier (e.g., `gemini-1.5-flash`, `llama-3-8b-instruct`).
  - `prompt_version`: `VARCHAR(32)` — Version token of the prompt template (e.g., `ambiguity_resolution_v1`).
  - `input_hash`: `CHAR(64)` — SHA256 hash of the sanitized text payload submitted to the model.
  - `structured_output`: `JSONB` — Validated Pydantic schema response containing extracted fields (responsibilities, skills, parsed experience).
  - `confidence`: `NUMERIC(3,2)` — Model-reported or calibrated confidence score ($0.00 \le C \le 1.00$).
  - `tokens_used`: `INTEGER` — Total prompt + completion tokens consumed.
  - `latency_ms`: `INTEGER` — Response turnaround latency in milliseconds.
  - `is_fallback`: `BOOLEAN` — True if this result was produced by the local offline model fallback. Default: `false`.
  - `created_at`: `TIMESTAMPTZ` — Timestamp when inference was executed.
- **Nullability:**
  - *Required:* `id`, `cache_key`, `provider`, `model`, `prompt_version`, `input_hash`, `structured_output`, `tokens_used`, `latency_ms`, `is_fallback`, `created_at`.
  - *Nullable:* `job_id`, `confidence`.
- **Uniqueness & Partial Unique Index Architecture:**
  - `UNIQUE (cache_key)` — Guarantees that identical input text, prompt version, and model will hit the cache and never execute twice.
- **Important Indexes:**
  - `idx_ai_cache_key` ON `cache_key` (B-Tree) — Instant $O(1)$ AI gateway cache lookup.
  - `idx_ai_provider_model` ON `(provider, model)` (B-Tree) — Telemetry analysis tracking quota utilization across providers.
- **Relationships:**
  - Many-to-one with `jobs` (optional).
- **Lifecycle Expectations:**
  - Created on AI invocation cache misses. Persisted indefinitely to serve as a warm cache across application restarts.

---

### 3.10 `discovery_runs`
- **Purpose:** Tracks execution batches that discover new companies, career pages, and ATS endpoints (e.g., DNS scans, Common Crawl index queries, public registry sweeps).
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique discovery run ID.
  - `run_type`: `VARCHAR(32)` — Discovery mode: `DNS_PROBE`, `COMMON_CRAWL_INDEX`, `REGISTRY_SWEEP`, `MANUAL_SEED`.
  - `started_at`: `TIMESTAMPTZ` — Execution start time.
  - `finished_at`: `TIMESTAMPTZ` — Execution completion time.
  - `status`: `VARCHAR(32)` — Run status: `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`.
  - `discovery_methods`: `JSONB` — Array of methods utilized in this run (`["DNS_LOOKUP", "SITEMAP"]`).
  - `companies_probed`: `INTEGER` — Total corporate domains evaluated. Default: `0`.
  - `endpoints_discovered`: `INTEGER` — Valid ATS or career endpoints identified. Default: `0`.
  - `errors_count`: `INTEGER` — Count of failed domain lookups or network errors. Default: `0`.
  - `telemetry`: `JSONB` — Structured performance metrics (duration breakdown, error logs, discovery yield).
- **Nullability:**
  - *Required:* `id`, `run_type`, `started_at`, `status`, `companies_probed`, `endpoints_discovered`, `errors_count`.
  - *Nullable:* `finished_at`, `discovery_methods`, `telemetry`.
- **Important Indexes:**
  - `idx_discovery_runs_time` ON `started_at DESC` (B-Tree) — Used for operational run monitoring.
- **Lifecycle Expectations:**
  - Appended at the beginning of a discovery cycle; updated upon cycle termination.

---

### 3.11 `collection_runs`
- **Purpose:** Tracks automated harvesting batches collecting job records from known sources and configured ATS endpoints.
- **Primary Key:** `id` (`UUID`, default `gen_random_uuid()`)
- **Foreign Keys:**
  - `source_id` REFERENCES `sources(id)` ON DELETE SET NULL (nullable if multi-source crawl)
  - `company_source_id` REFERENCES `company_sources(id)` ON DELETE SET NULL (nullable if multi-endpoint or platform-wide sweep)
- **Key Fields & Conceptual Types:**
  - `id`: `UUID` — Unique collection run ID.
  - `source_id`: `UUID` — Foreign key referencing specific platform (nullable for multi-source sweeps).
  - `company_source_id`: `UUID` — Foreign key referencing targeted company endpoint configuration (nullable for platform sweeps).
  - `run_type`: `VARCHAR(32)` — Cadence tier: `HIGH_FREQUENCY_ATS`, `DAILY_CRAWL`, `MANUAL_TRIGGER`.
  - `started_at`: `TIMESTAMPTZ` — Harvest start timestamp.
  - `finished_at`: `TIMESTAMPTZ` — Harvest completion timestamp.
  - `status`: `VARCHAR(32)` — Run status: `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`.
  - `records_discovered`: `INTEGER` — Total job listings identified on endpoints. Default: `0`.
  - `records_ingested`: `INTEGER` — New raw records written to `raw_jobs`. Default: `0`.
  - `records_failed`: `INTEGER` — HTTP or parsing failures encountered. Default: `0`.
  - `duplicates_detected`: `INTEGER` — Existing records matched during deduplication. Default: `0`.
  - `http_2xx_count`: `INTEGER` — Successful HTTP requests count. Default: `0`.
  - `http_4xx_count`: `INTEGER` — Client error count (e.g., 404 dead boards, 429 rate limits). Default: `0`.
  - `http_5xx_count`: `INTEGER` — Upstream server error count. Default: `0`.
  - `telemetry`: `JSONB` — Detailed performance metadata (latency per company, error traces).
- **Nullability:**
  - *Required:* `id`, `run_type`, `started_at`, `status`, `records_discovered`, `records_ingested`, `records_failed`, `duplicates_detected`, `http_2xx_count`, `http_4xx_count`, `http_5xx_count`.
  - *Nullable:* `source_id`, `company_source_id`, `finished_at`, `telemetry`.
- **Important Indexes:**
  - `idx_collection_runs_time` ON `started_at DESC` (B-Tree) — Monitoring dashboard queries.
  - `idx_collection_runs_status` ON `(status, run_type)` (B-Tree) — Schedulers check for active overlapping runs.
- **Relationships:**
  - Many-to-one with `sources` (optional).
  - Many-to-one with `company_sources` (optional).
  - One-to-many with `raw_jobs` (`collection_runs.id -> raw_jobs.collection_run_id`).
- **Lifecycle Expectations:**
  - Appended by APScheduler job triggers; updated upon completion.

---

## 4. Layered Deduplication & Identity Architecture

Deduplication operates across four distinct stages as defined in [`DEDUPLICATION_V1.md`](../../rules/DEDUPLICATION_V1.md). The schema is explicitly structured to support this hierarchy:

```text
                                Incoming Raw Job Record
                                           │
                                           ▼
                 ┌───────────────────────────────────────────────────┐
                 │ Stage 1: Exact Source Identity Match             │
                 │ Query: (source_id, source_job_id) via JSR         │
                 │ or (company_source_id, source_job_id) via raw_jobs│
                 │ Index: idx_job_source_ref_source_ext              │
                 └─────────────────────────┬─────────────────────────┘
                                           │ Match Found -> Update timestamps, record refresh
                                           │ No Match
                                           ▼
                 ┌───────────────────────────────────────────────────┐
                 │ Stage 2: Canonical Application / ATS URL Match   │
                 │ Query: application_url exact match                │
                 │ Index: idx_job_source_ref_app_url                 │
                 └─────────────────────────┬─────────────────────────┘
                                           │ Match Found -> Merge as new JobSourceReference
                                           │ No Match
                                           ▼
                 ┌───────────────────────────────────────────────────┐
                 │ Stage 3: Composite Deterministic Match           │
                 │ Query: (company_id, norm_title, norm_location)   │
                 │ Index: idx_jobs_dedup_composite                   │
                 └─────────────────────────┬─────────────────────────┘
                                           │ Match Found -> Merge as new JobSourceReference
                                           │ No Match
                                           ▼
                 ┌───────────────────────────────────────────────────┐
                 │ Stage 4: Semantic / Text Similarity Match        │
                 │ Application Layer: MinHash / Title Token Jaccard  │
                 │ (Evaluated in Python application memory)          │
                 └─────────────────────────┬─────────────────────────┘
                                           │ Match Found -> Merge as new JobSourceReference
                                           │ No Match
                                           ▼
                                Create New Canonical Job
```

> [!IMPORTANT]
> **Stage 4 is NOT a Database Constraint:**
> Stage 4 involves fuzzy token matching and text signature overlap. The PostgreSQL schema supports Stages 1–3 through targeted B-Tree indexes. Stage 4 evaluation is performed by the application layer (`app/deduplication/`) using memory-efficient algorithms (MinHash / Jaccard similarity), asserting database transactions only after a candidate match is resolved.

---

## 5. Indexing Strategy & Query Workflows

Indexes are strictly designed to support core operational query patterns without imposing write-amplification penalties on bulk ingestion:

| Target Table | Index Name | Indexed Columns | Type | Target Operational Query Workflow |
| :--- | :--- | :--- | :--- | :--- |
| `jobs` | `idx_jobs_feed` | `(eligibility_status, role_priority)` | B-Tree | Primary user feed rendering: `WHERE eligibility_status = 'ELIGIBLE' ORDER BY role_priority ASC` |
| `jobs` | `idx_jobs_posted` | `posted_at DESC NULLS LAST` | B-Tree | Freshness tiers: `WHERE posted_at >= NOW() - INTERVAL '24 hours'` |
| `jobs` | `idx_jobs_dedup_comp` | `(company_id, normalized_title, normalized_location)` | B-Tree | Stage 3 deduplication candidate lookup |
| `jobs` | `idx_jobs_company` | `company_id` | B-Tree | Fetch all active openings for a given employer |
| `companies` | `idx_companies_norm` | `normalized_name` | B-Tree | Company resolution during ingestion: `WHERE normalized_name = :slug` |
| `company_sources`| `idx_comp_src_poll`| `(is_active, last_collected_at NULLS FIRST)` | B-Tree | Scheduler selecting next stale ATS endpoints to poll |
| `company_sources`| `uq_comp_src_ats` | `(company_id, source_id, ats_identifier) WHERE ats_identifier IS NOT NULL` | B-Tree (Unique) | Enforce unique ATS board token per company and source |
| `company_sources`| `uq_comp_src_url` | `(company_id, source_id, career_url) WHERE ats_identifier IS NULL AND career_url IS NOT NULL` | B-Tree (Unique) | Enforce unique career URL for generic career pages lacking ATS tokens |
| `company_sources`| `uq_comp_src_api` | `(company_id, source_id, api_endpoint) WHERE ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NOT NULL` | B-Tree (Unique) | Enforce unique direct API endpoint when ATS token and career URL are absent |
| `company_sources`| `uq_comp_src_bare`| `(company_id, source_id) WHERE ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NULL` | B-Tree (Unique) | Prevent duplicate unconfigured endpoint configurations for same company and source |
| `raw_jobs` | `idx_raw_jobs_comp_src_ext` | `(company_source_id, source_job_id)` | B-Tree | Fast lookup of raw historical responses by company endpoint and external ID |
| `raw_jobs` | `idx_raw_jobs_hash`| `content_hash` | B-Tree | Deduplicating byte-identical payloads before parsing |
| `job_source_references` | `idx_jsr_app_url` | `application_url` | B-Tree | Stage 2 deduplication match on canonical ATS URL |
| `ai_analysis_results` | `idx_ai_cache_key` | `cache_key` | B-Tree | Instant $O(1)$ AI gateway cache lookup |
| `filter_decisions`| `idx_filter_job` | `(job_id, evaluated_at DESC)` | B-Tree | Audit trail display in job details UI |

### JSONB Indexing Policy
- Native `JSONB` columns (`raw_payload`, `audit_trail`, `telemetry`, `extracted_data`) are stored without full-column GIN indexes by default.
- Indiscriminate GIN indexing on large text payloads wastes disk space and slows down write throughput.
- If specific nested attributes require frequent SQL querying in future phases, targeted partial functional indexes will be added (e.g., `CREATE INDEX ON jobs ((responsibilities->0))`).

---

## 6. Data Retention & Archival Policy

To satisfy the system's core portfolio and production-quality mandates, the database strictly maintains historical evidence:

1. **Immutable Preservation:**
   - `raw_jobs`: Raw external payloads are preserved permanently. Even when an opening closes, its raw payload remains as historical evidence for model training, compensation benchmarks, and audit integrity.
   - `job_source_references`: Multi-platform references remain intact to analyze platform cross-posting patterns.
   - `verification_records`: All historical verification passes and failures remain stored to audit platform reliability.
   - `filter_decisions`: Decisions remain stored with their respective `rule_version` tag to allow rule regression testing.
   - `ai_analysis_results`: Cached AI extractions remain stored indefinitely to maximize cache hits.
   - `discovery_runs` & `collection_runs`: Run telemetry is preserved for system observability.
   - **Referential Delete Safeguards:** `job_source_references`, `verification_records`, and `filter_decisions` enforce `ON DELETE RESTRICT` on `jobs(id)` to prevent hard deletion of canonical jobs while audit or source evidence exists. `ai_analysis_results` enforces `ON DELETE SET NULL` on `job_id` to guarantee that cached inference outputs and token investments are never lost even if a linked job entity is unlinked or detached.
2. **Job Lifecycle Expiration (`EXPIRED`):**
   - Active-feed eligibility is strictly governed by original publication date (`posted_at`). When a job's `posted_at` exceeds the 5-day active feed threshold (or if an old job is discovered late with `posted_at` older than 5 days), the scheduler/rules engine transitions `jobs.eligibility_status = 'EXPIRED'` and sets `jobs.rejection_reason = 'JOB_TOO_OLD'`.
   - **Late-Discovered Postings:** A late-discovered job is stored permanently for historical data. If its verified original publication date is >5 days old, it transitions directly to `EXPIRED` (`rejection_reason = 'JOB_TOO_OLD'`) and does not enter the active feed.
   - **Missing or Unparseable Publication Date:** When `posted_at` is unknown or invalid, `posted_at` remains NULL. In accordance with the frozen `VERIFICATION_V1.md` rule, `collected_at` serves solely as a provisional operational fallback for plausibility evaluation, and the job must remain in `REVIEW` (`review_reason = 'POSTED_DATE_SUSPICIOUS'`) until publication-date validity is established. `collected_at` alone is ingestion metadata and never establishes active freshness or feed eligibility.
   - **No Deletion:** No records are deleted from `jobs`, `raw_jobs`, or associated tables. Expired jobs simply drop out of the default active feed query (`WHERE eligibility_status = 'ELIGIBLE'`).
3. **Storage Budgeting:**
   - At projected V1 volumes (2,000–5,000 raw jobs daily at 20 KB/record), monthly database growth is approximately 1.2–3.0 GB. Single-node PostgreSQL handles this volume effortlessly without external cold storage.

---

## 7. Status & Taxonomy Alignment

All operational states and classification enums in this schema strictly map to the frozen V1 specifications:

### 7.1 Operational Eligibility Status (`jobs.eligibility_status`)
- `NEW`: Freshly normalized record, awaiting initial rule evaluation.
- `ELIGIBLE`: Passed all deterministic filters, meets 0–2yr experience policy and target criteria; displayed in user feed.
- `REVIEW`: Ambiguous data detected (e.g., 1–3 years experience, unstated salary, missing or ambiguous location, missing/unparseable posting date, remote company location, multi-source attribute conflicts); routed to user review queue with explicit `review_reason` (e.g., `LOCATION_AMBIGUOUS`, `POSTED_DATE_SUSPICIOUS`, `SALARY_UNKNOWN`, `EXPERIENCE_AMBIGUOUS`, `COMPANY_UNVERIFIED`, `SOURCE_CONFLICT_REVIEW`).
- `REJECTED`: Fails deterministic criteria (e.g., minimum experience $\ge 2$ years, excluded role, disallowed location); assigned explicit `rejection_reason`.
- `DUPLICATE`: Matched an existing canonical job posting during deduplication; consolidated as a `JobSourceReference`.
- `EXPIRED`: Previously active or late-discovered job whose original publication date (`posted_at`) exceeds the 5-day feed window (`rejection_reason = JOB_TOO_OLD`). Late-discovered postings with verified `posted_at` > 5 days are preserved historically but excluded from the active feed; `collected_at` serves only as ingestion metadata and provisional verification fallback, never making old jobs fresh.
- `VERIFICATION_FAILED`: Hard technical failure during verification (e.g., 404 dead page, corrupt payload, unverified source).

### 7.2 Role Priority (`jobs.role_priority`)
- `P1`: Highest priority core target roles (AI Engineer, GenAI Engineer, Agentic AI Developer, LLM Engineer, RAG Engineer).
- `P2`: Strong target AI-adjacent and backend foundations (Forward Deployed Engineer, AI Backend Engineer, Python Backend Engineer).
- `P3`: Foundational technical roles (ML Engineer, Python Developer, Software Engineer, SDE).
- `NOT_RELEVANT`: Roles outside target disciplines.
