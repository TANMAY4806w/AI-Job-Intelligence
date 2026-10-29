# Figma Job Eligibility & Data Quality Audit Report V1

**Target Database:** `job_intelligence_dev`<br>
**Schema Revision:** `05377f2ddb14`<br>
**Execution Timestamp:** 2026-10-29<br>
**Audit Type:** Read-Only Application & Database Verification<br>
**Repository Baseline:** `8a4d61a` (`fix: isolate integration tests from development database`)

---

## 1. Executive Summary

This report documents the exhaustive post-ingestion audit of the **138 canonical job postings** harvested from the public Greenhouse job board of **Figma, Inc.** (`ats_identifier = 'figma'`).

### Key Findings
1. **Zero Actionable Jobs for the Target Candidate:** Out of 138 ingested Figma positions, **0 jobs are ELIGIBLE** for an entry-level Fresher AI/ML Engineer based in India (Mumbai, Navi Mumbai, Thane, Pune, Bengaluru, Hyderabad).
2. **Eligibility Outcome Distribution:**
   - **REJECTED:** **137 jobs (99.28%)**
     - 123 jobs rejected due to `EXCLUDED_ROLE` (non-technical, sales, marketing, support, design, leadership).
     - 14 jobs rejected due to `LOCATION_NOT_ALLOWED` (software engineering positions physically based in US, UK, or Germany).
   - **REVIEW:** **1 job (0.72%)**
     - `Software Engineer - Figma Weave (Tel Aviv, Israel)` routed to `REVIEW` with primary review reason `POSTED_DATE_SUSPICIOUS` and secondary review codes `[LOCATION_AMBIGUOUS, EXPERIENCE_AMBIGUOUS, SALARY_UNKNOWN]`.
   - **ELIGIBLE:** **0 jobs (0.00%)**
   - **EXPIRED:** **0 jobs (0.00%)** (Due to freshness precedence gating explained in Section 6).
3. **India Presence Assessment:** Figma lists exactly **5 positions in India (Bengaluru)**. None of these 5 positions are software or AI engineering roles:
   - 2 Customer / Community Support Specialists
   - 1 Enterprise Support Specialist
   - 1 Technical Support Engineer (classified as IT/support exclusion)
   - 1 Account Executive (Enterprise Sales)
   All 5 India positions were legitimately rejected under role-exclusion rules (`EXCLUDED_ROLE`).
4. **Data Completeness & Quality:**
   - **100% Completeness** for titles, job descriptions, application URLs, and original publication timestamps (`posted_at`).
   - **0% Structured Coverage** for compensation (`salary_min`, `salary_max`) and experience requirements (`experience_min_years`, `experience_max_years`), as Greenhouse delivers these embedded in free-form HTML content rather than structured API keys.
   - **Verification Confidence:** All 138 jobs scored in the `LOW` confidence tier ($0.30 - 0.40$) because collector-level verification evidence was not piped into the ingestion envelope, causing default verification fallbacks.

---

## 2. Target Database Baseline Verification

Live database baseline checks executed against `job_intelligence_dev`:

| Entity | Identifier / Value | Verification Status | Notes |
| :--- | :--- | :--- | :--- |
| **Database** | `job_intelligence_dev` | **PASS** | `SELECT current_database()` matches dev environment |
| **Alembic Revision** | `05377f2ddb14` | **PASS** | Latest migration applied cleanly |
| **Company Record** | `4916b6da-2803-4a9e-b3ce-dd41ef2a5324` | **PASS** | Name: `Figma`, normalized: `figma` |
| **CompanySource Record** | `9a75b6d6-8295-4c3f-8e65-642188277113` | **PASS** | Source: `greenhouse`, ATS identifier: `figma` |
| **CollectionRun Record** | `9667b81a-50e9-4406-be19-53c5386a5917` | **PASS** | Status: `COMPLETED`, 138 discovered, 138 ingested, 0 failed |
| **RawJob Count** | `138` | **PASS** | Exact 1:1 match with discovered records |
| **Canonical Job Count** | `138` | **PASS** | Exactly 138 deduplicated canonical records |
| **JobSourceReference Count** | `138` | **PASS** | Exact 1:1 provenance link per job |
| **VerificationRecord Count** | `138` | **PASS** | Exactly 1:1 historical verification audit row |
| **FilterDecision Count** | `138` | **PASS** | Exactly 1:1 deterministic filter decision row |

---

## 3. Persisted Outcomes Summary

### 3.1 Eligibility Status Breakdown

```
+--------------------+-------+------------+
| Eligibility Status | Count | Percentage |
+--------------------+-------+------------+
| REJECTED           |   137 |     99.28% |
| REVIEW             |     1 |      0.72% |
| ELIGIBLE           |     0 |      0.00% |
| EXPIRED            |     0 |      0.00% |
+--------------------+-------+------------+
| Total              |   138 |    100.00% |
+--------------------+-------+------------+
```

### 3.2 Rejection Reasons Breakdown (`jobs.rejection_reason`)

```
+----------------------+-------+------------+-------------------------------------------------------------+
| Rejection Reason     | Count | Percentage | Primary Drivers                                             |
+----------------------+-------+------------+-------------------------------------------------------------+
| EXCLUDED_ROLE        |   123 |     89.13% | Sales, Support, Recruiting, Design, Operations, Leadership  |
| LOCATION_NOT_ALLOWED |    14 |     10.14% | Software engineering roles confirmed in SF, NYC, or London  |
+----------------------+-------+------------+-------------------------------------------------------------+
| Total Rejections     |   137 |     99.28% |                                                             |
+----------------------+-------+------------+-------------------------------------------------------------+
```

### 3.3 Review Reasons Breakdown

In canonical `jobs.review_reason` (singular primary review reason):
- `POSTED_DATE_SUSPICIOUS`: **1 job** (`Software Engineer - Figma Weave (Tel Aviv, Israel)`).

In `verification_records.review_reasons` (multi-dimensional JSONB audit array across all 138 jobs):
- `APPLICATION_LINK_BROKEN`: **138 jobs (100.0%)** (HTTP reachability probe not executed during ingestion)
- `COMPANY_UNVERIFIED`: **138 jobs (100.0%)** (Company registry corroboration flag not passed)
- `EXPERIENCE_AMBIGUOUS`: **138 jobs (100.0%)** (Unstructured experience in HTML)
- `SALARY_UNKNOWN`: **138 jobs (100.0%)** (Unstructured compensation in HTML)
- `POSTED_DATE_SUSPICIOUS`: **138 jobs (100.0%)** (Publication date corroboration flag not passed)
- `LOCATION_AMBIGUOUS`: **133 jobs (96.38%)** (Foreign / non-target locations)

In `verification_records.failure_reasons`:
- `SOURCE_INVALID`: **138 jobs (100.0%)** (`source_type` defaulted to None during verification evaluation)

---

## 4. Role Priority and Relevance Analysis

Under the frozen V1 role taxonomy (`rules/JOB_FILTERS_V1.md` Section 2):

| Priority Tier | Role Category | Persisted Count | Percentage | Filter Status |
| :--- | :--- | :---: | :---: | :--- |
| **P1** | Core AI / GenAI / LLM Engineers | **0** | 0.00% | N/A |
| **P2** | Strong Targets / Accelerators / AI-Adjacent | **1** | 0.72% | 1 REJECTED (`LOCATION_NOT_ALLOWED`) |
| **P3** | Foundational Tech / SWE / ML Engineer | **14** | 10.14% | 13 REJECTED, 1 REVIEW |
| **NOT_RELEVANT**| Non-Technical / Management / Excluded | **123** | 89.13% | 123 REJECTED (`EXCLUDED_ROLE`) |
| **Total** | | **138** | **100.00%** | |

### Inventory of All Technical Positions (P1, P2, P3)

The 15 technical positions harvested from Figma:

| Priority | Job Title | Location | Status | Reason Code |
| :--- | :--- | :--- | :--- | :--- |
| **P2** | Forward Deployed Engineer | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - Machine Learning | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - AI Product | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - AI Product (London) | London, England | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - AI Platforms | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - Full Stack | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - Distributed Systems | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - C++ | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - Data Infrastructure | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - Developer Experience | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - Growth & Monetization | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | PhD Intern, Software Engineer - Graphics (2027) | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer Intern (Summer 2027) | San Francisco, CA • New York, NY | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer Intern (London) (Summer 2027)| London, England | REJECTED | `LOCATION_NOT_ALLOWED` |
| **P3** | Software Engineer - Figma Weave (Tel Aviv) | Tel Aviv, Israel | **REVIEW** | `POSTED_DATE_SUSPICIOUS` |

---

## 5. Geographic Distribution & India Assessment

### 5.1 Geographic Distribution of Figma Positions

```
+-------------------------------------------------------+-------+
| Raw Location Cluster                                  | Count |
+-------------------------------------------------------+-------+
| San Francisco, CA • New York, NY • United States       |    68 |
| London, England                                       |    18 |
| San Francisco, CA • New York, NY                      |    13 |
| Berlin, Germany                                       |     7 |
| Bengaluru, India                                      |     5 |
| São Paulo, Brazil                                     |     5 |
| San Francisco, CA                                     |     4 |
| Paris, France                                         |     4 |
| New York, NY • United States                          |     3 |
| Singapore                                             |     2 |
| Tokyo, Japan                                          |     2 |
| Sydney, Australia                                     |     2 |
| Tel Aviv, Israel                                      |     2 |
| Sydney, Australia • Melbourne, Australia              |     1 |
| Other US / Distributed Listings                       |     2 |
+-------------------------------------------------------+-------+
| Total                                                 |   138 |
+-------------------------------------------------------+-------+
```

### 5.2 India Postings Audit

Figma has exactly **5 positions** located in India, all based in Bengaluru:

| Job Title | Location | Role Priority | Eligibility | Rejection Reason |
| :--- | :--- | :--- | :--- | :--- |
| Account Executive, Enterprise (Bengaluru, India) | Bengaluru, India | `NOT_RELEVANT` | `REJECTED` | `EXCLUDED_ROLE` |
| Community Support Specialist (Bengaluru, India) | Bengaluru, India | `NOT_RELEVANT` | `REJECTED` | `EXCLUDED_ROLE` |
| Customer Support Specialist (Bengaluru, India) | Bengaluru, India | `NOT_RELEVANT` | `REJECTED` | `EXCLUDED_ROLE` |
| Technical Support Engineer (Bengaluru, India) | Bengaluru, India | `NOT_RELEVANT` | `REJECTED` | `EXCLUDED_ROLE` |
| Enterprise Support Specialist (Bengaluru, India) | Bengaluru, India | `NOT_RELEVANT` | `REJECTED` | `EXCLUDED_ROLE` |

**Candidate Assessment:** None of these jobs are relevant for an AI/ML or Software Engineer candidate. The Bengaluru office functions primarily as an international customer support, community, and enterprise sales hub.

---

## 6. Freshness and Publication Date Analysis

### 6.1 Publication Date Statistics
- **Total Postings with Publication Timestamp:** **138 / 138 (100.0%)**
- **Earliest Publication Date:** `2025-01-13 12:43:20 UTC` (650 days prior to collection)
- **Latest Publication Date:** `2026-10-23 04:53:28 UTC` (2.5 days prior to collection)
- **Mean Publication Age at Collection:** **157.8 days (~5.2 months)**

### 6.2 Publication Age Distribution at Collection Time

```
+-------------------------+-------+------------+--------------------------------------------+
| Age at Collection       | Count | Percentage | Characteristics                            |
+-------------------------+-------+------------+--------------------------------------------+
| <= 5 Days (Fresh)       |     8 |      5.80% | All 8 are non-technical (Support / Sales)  |
| > 5 and <= 30 Days      |    21 |     15.22% | 1 tech intern, 20 non-technical roles      |
| > 30 and <= 90 Days     |    41 |     29.71% | 3 tech roles (Forward Deployed Eng, etc.)  |
| > 90 Days (Stale/Old)   |    68 |     49.28% | 11 tech roles (ML, AI Product, SWE, etc.)  |
+-------------------------+-------+------------+--------------------------------------------+
| Total                   |   138 |    100.00% |                                            |
+-------------------------+-------+------------+--------------------------------------------+
```

### 6.3 Technical Roles Freshness Audit

None of the 15 technical positions satisfied the 5-day freshness rule at collection time:
- The freshest technical role was `PhD Intern, Software Engineer - Graphics (2027)` at **12.0 days old**.
- `Forward Deployed Engineer` was **60.8 days old**.
- `Software Engineer - Figma Weave (Tel Aviv)` was **129.9 days old**.
- 8 Core Software Engineering roles (`Software Engineer - Machine Learning`, `Software Engineer - AI Product`, `Software Engineer - Full Stack`, etc.) were **between 263 and 510 days old (~9 to 17 months)**.

### 6.4 Why Older Postings Were Not Statused `EXPIRED`
Under `rules/JOB_FILTERS_V1.md` Section 1.2 and `JobFilterRuleEngine`:
1. **Rule Precedence Hierarchy:** Step 1 (`Role Exclusion`) and Step 3 (`Location Exclusion`) precede Step 5 (`Freshness Expiration`). 137 of the 138 jobs failed Step 1 or Step 3 and were immediately short-circuited to `REJECTED`.
2. **Freshness Gating Semantics:** Under `app/rules/freshness.py`, when `posting_date_verified is False`, the evaluation returns `status="SUSPICIOUS"` (`is_expired=False`). Because verification evidence was unverified, the engine routed unresolved postings to `REVIEW` with `POSTED_DATE_SUSPICIOUS` rather than silently assuming validity and expiring them with `JOB_TOO_OLD`.

---

## 7. Eight-Dimension Verification Audit

The verification engine evaluated each canonical job across the 8 frozen dimensions (`rules/VERIFICATION_V1.md`):

| Dimension | Description | Verified Count | Failed / Unverified Count | Assigned Review / Failure Reason |
| :--- | :--- | :---: | :---: | :--- |
| **D1: Source Validity** | ATS origin legitimacy | 0 | 138 (100%) | `SOURCE_INVALID` |
| **D2: JD Existence** | Description presence & length | **138 (100%)** | 0 (0%) | *Passed* (coherent HTML text) |
| **D3: URL Validity** | Application URL syntax & reachability | 0 | 138 (100%) | `APPLICATION_LINK_BROKEN` |
| **D4: Company Legitimacy** | Verified employer entity | 0 | 138 (100%) | `COMPANY_UNVERIFIED` |
| **D5: Location Consistency**| Approved target tech hub | 5 (3.6%) | 133 (96.4%) | `LOCATION_AMBIGUOUS` (133 jobs) |
| **D6: Experience Consistency**| Clear minimum & maximum years | 0 | 138 (100%) | `EXPERIENCE_AMBIGUOUS` |
| **D7: Salary Statedness** | Stated compensation interval | 0 | 138 (100%) | `SALARY_UNKNOWN` |
| **D8: Posting Date** | Corroborated publication date | 0 | 138 (100%) | `POSTED_DATE_SUSPICIOUS` |

### Confidence Scoring
- **Minimum Score:** `0.30` (133 jobs)
- **Maximum Score:** `0.40` (5 Bengaluru jobs with verified location matching P2 hub)
- **Mean Score:** `0.3036`
- **Confidence Tier Distribution:** **100% (138 / 138) in `LOW` tier (< 0.60)**.

---

## 8. Missing and Unknown Data Quality Analysis

| Data Field | Total Records | Missing / Unknown Count | Complete Count | Completeness |
| :--- | :---: | :---: | :---: | :---: |
| **Job Title** | 138 | 0 | 138 | **100.0%** |
| **Job Description** | 138 | 0 | 138 | **100.0%** |
| **Application URL** | 138 | 0 | 138 | **100.0%** |
| **Publication Timestamp (`posted_at`)** | 138 | 0 | 138 | **100.0%** |
| **Raw Location** | 138 | 0 | 138 | **100.0%** |
| **Normalized Location (`normalized_location`)** | 138 | 133 | 5 | **3.6%** |
| **Remote Arrangement (`remote_type`)** | 138 | 138 (`UNKNOWN`) | 0 | **0.0%** |
| **Minimum Salary (`salary_min`)** | 138 | 138 | 0 | **0.0%** |
| **Maximum Salary (`salary_max`)** | 138 | 138 | 0 | **0.0%** |
| **Minimum Experience (`experience_min_years`)** | 138 | 138 | 0 | **0.0%** |
| **Maximum Experience (`experience_max_years`)** | 138 | 138 | 0 | **0.0%** |

---

## 9. Discovered Pipeline Logic & Data Discrepancies

### Discrepancy 1: Ingestion Verification Evidence Passthrough Omission
- **Observation:** `GreenhouseCollector` ingests valid job postings from Greenhouse but instantiates `JobIngestionEnvelope` without populating `verification_evidence: JobVerificationInput`.
- **Impact:** `JobIngestionService` falls back to `JobVerificationInput.from_job(job)`. Because `from_job` lacks context on `source_type="greenhouse"`, registry verification, and URL status, `source_verified` and `application_url_verified` evaluate to `False` across 100% of collected records, depressing verification confidence to `0.30`.
- **Remediation Recommendation:** Update `GreenhouseCollector` or `JobIngestionService` to build a populated `JobVerificationInput` specifying `source_type="greenhouse"`, `is_source_approved=True`, and `payload_format_valid=True`.

### Discrepancy 2: Foreign City Coverage in Location Normalization
- **Observation:** The single position routed to `REVIEW` instead of `REJECTED` was `Software Engineer - Figma Weave (Tel Aviv, Israel)`.
- **Root Cause:** In `app/rules/location.py`, the `_FOREIGN_OR_OTHER_CITY` exclusion regex includes London, New York, San Francisco, Berlin, Singapore, Sydney, etc., but omits `Tel Aviv` (as well as `São Paulo`, `Paris`, and `Tokyo`). While Paris, São Paulo, and Tokyo positions were rejected under `EXCLUDED_ROLE`, Tel Aviv was an engineering role that fell through to `AMBIGUOUS`.
- **Remediation Recommendation:** Expand the known unlisted foreign tech hub regex in `app/rules/location.py` and `app/normalization/location.py` to include Tel Aviv, São Paulo, Paris, Tokyo, and international country suffixes (e.g. `Israel`, `Brazil`, `France`, `Japan`).

### Discrepancy 3: Free-Text HTML Compensation & Experience Ingestion
- **Observation:** 100% of Figma jobs have `NULL` salary and experience columns.
- **Root Cause:** The public Greenhouse API returns compensation and qualifications inside free-text HTML `<p>` tags within `content` rather than discrete JSON attributes.
- **Remediation Recommendation:** In an upcoming normalization milestone, introduce regex/pattern-based extraction for Greenhouse HTML descriptions to parse compensation intervals (e.g. `$165,000 - $210,000 USD`) and years of experience (e.g. `5+ years of experience`).

---

## 10. Reproducible Read-Only SQL Queries

All findings in this report can be reproduced using the following read-only SQL queries against `job_intelligence_dev`:

```sql
-- 1. Verify live database identity and Alembic migration
SELECT current_database() AS target_database;
SELECT version_num AS alembic_revision FROM alembic_version;

-- 2. Verify Figma baseline company, source, and collection run
SELECT id, name, normalized_name FROM companies WHERE id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324';
SELECT id, company_id, ats_identifier FROM company_sources WHERE id = '9a75b6d6-8295-4c3f-8e65-642188277113';
SELECT id, status, records_discovered, records_ingested, records_failed FROM collection_runs WHERE id = '9667b81a-50e9-4406-be19-53c5386a5917';

-- 3. Verify exact entity counts
SELECT
    (SELECT count(*) FROM raw_jobs WHERE company_source_id = '9a75b6d6-8295-4c3f-8e65-642188277113') AS raw_jobs_count,
    (SELECT count(*) FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324') AS canonical_jobs_count,
    (SELECT count(*) FROM job_source_references jsr JOIN jobs j ON j.id = jsr.job_id WHERE j.company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324') AS jsr_count,
    (SELECT count(*) FROM verification_records vr JOIN jobs j ON j.id = vr.job_id WHERE j.company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324') AS vr_count,
    (SELECT count(*) FROM filter_decisions fd JOIN jobs j ON j.id = fd.job_id WHERE j.company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324') AS fd_count;

-- 4. Status and reason distribution
SELECT eligibility_status, count(*) AS count, round(count(*) * 100.0 / 138, 2) AS percentage
FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324'
GROUP BY eligibility_status ORDER BY count DESC;

SELECT rejection_reason, count(*) AS count
FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND rejection_reason IS NOT NULL
GROUP BY rejection_reason ORDER BY count DESC;

-- 5. Role priority breakdown
SELECT role_priority, count(*) AS count, round(count(*) * 100.0 / 138, 2) AS percentage
FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324'
GROUP BY role_priority ORDER BY count DESC;

-- 6. Inspect the 15 technical positions
SELECT title, location, role_priority, eligibility_status, rejection_reason, review_reason, posted_at
FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND role_priority IN ('P1', 'P2', 'P3')
ORDER BY posted_at DESC;

-- 7. Inspect the 5 India postings
SELECT title, location, normalized_location, role_priority, eligibility_status, rejection_reason
FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324' AND location ILIKE '%india%'
ORDER BY title ASC;

-- 8. Publication date age distribution at collection time
SELECT
    count(CASE WHEN (collected_at - posted_at) <= INTERVAL '5 days' THEN 1 END) AS count_le_5d,
    count(CASE WHEN (collected_at - posted_at) > INTERVAL '5 days' AND (collected_at - posted_at) <= INTERVAL '30 days' THEN 1 END) AS count_5d_to_30d,
    count(CASE WHEN (collected_at - posted_at) > INTERVAL '30 days' AND (collected_at - posted_at) <= INTERVAL '90 days' THEN 1 END) AS count_30d_to_90d,
    count(CASE WHEN (collected_at - posted_at) > INTERVAL '90 days' THEN 1 END) AS count_gt_90d
FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324';

-- 9. Verification dimension outcomes summary
SELECT
    sum(CASE WHEN source_verified THEN 1 ELSE 0 END) AS source_verified_count,
    sum(CASE WHEN jd_verified THEN 1 ELSE 0 END) AS jd_verified_count,
    sum(CASE WHEN company_verified THEN 1 ELSE 0 END) AS company_verified_count,
    sum(CASE WHEN location_verified THEN 1 ELSE 0 END) AS location_verified_count,
    sum(CASE WHEN experience_verified THEN 1 ELSE 0 END) AS experience_verified_count,
    sum(CASE WHEN salary_verified THEN 1 ELSE 0 END) AS salary_verified_count,
    avg(verification_confidence) AS avg_confidence
FROM jobs WHERE company_id = '4916b6da-2803-4a9e-b3ce-dd41ef2a5324';
```

---

## 11. Recommendations for Next Milestones

1. **Expand Company Ingestion Catalog (India Tech Focus):**
   Figma does not currently hire software or AI engineers in India. To populate the feed with genuine candidate-eligible roles, add Greenhouse board tokens for companies with active engineering hubs in Bengaluru, Pune, Mumbai, or Hyderabad (e.g., Stripe, Uber, Airbnb, Atlassian, Instawork, Postman).
2. **Pipeline Verification Evidence Integration:**
   Update `GreenhouseCollector` to build a populated `JobVerificationInput` containing `source_type="greenhouse"`, `source_name="Greenhouse ATS"`, and `is_source_approved=True`. This will ensure `source_verified` passes and raises confidence scores from `0.30` to expected thresholds.
3. **HTML Description Text Extraction:**
   Implement specialized regex extractors for compensation and required experience to parse unstructured job descriptions from Greenhouse.
4. **Foreign Tech Hub Dictionary Expansion:**
   Add `Tel Aviv`, `São Paulo`, `Paris`, `Tokyo`, and relevant country identifiers to the known unlisted locations in `app/rules/location.py` to prevent foreign engineering roles from falling into `AMBIGUOUS`.
