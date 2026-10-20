# AI Job Intelligence

> **Current Status:** Planning / Phase 0  
> **Type:** Personal Portfolio Project

---

## 1. Project Name
**AI Job Intelligence**

---

## 2. Problem Statement
Job searching across modern platforms is fragmented, noisy, and inefficient. Candidates frequently encounter:
- Duplicate job postings across multiple job boards and aggregators.
- Outdated, ghost, or unverified job listings with missing or inaccurate details.
- Lack of granular classification (such as IT vs. non-IT or product-based vs. service-based companies).
- Overwhelming volume without adequate filtering for individual career criteria (exact role, experience, location, freshness, and compensation).

---

## 3. Project Goal
The goal of **AI Job Intelligence** is to build a centralized, reliable system that collects job openings across diverse sources, normalizes and deduplicates listings, verifies job posting authenticity and company data, evaluates openings against user-defined search criteria, and delivers an intelligent, curated feed of opportunities.

---

## 4. Current Scope
The project is currently in **Phase 0 (Project Initialization & Requirements Definition)**.
- Establishing foundational project documentation, scope boundaries, and development standards.
- Defining functional and non-functional requirements.
- **No application code, web scrapers, AI agents, or third-party runtime frameworks are implemented yet.**

---

## 5. Planned High-Level Capabilities
- **Centralized Job Discovery:** Ingesting job postings from multiple sources for wide coverage.
- **Data Normalization & Deduplication:** Standardizing disparate schemas and eliminating redundant postings.
- **Verification Engine:** Validating job description (JD) authenticity, freshness, and employer details.
- **Targeted Filtering:** Filtering candidates' opportunities based on role, years of experience, location, freshness, and salary criteria.
- **Company Classification:** Identifying business models (e.g., IT vs. non-IT, product-based vs. service-based).
- **AI/LLM Enrichment:** Enhancing role summaries, skill extractions, and alignment analysis in later stages.
- **Human Review Before Action:** Ensuring all candidate actions are reviewed; strictly no automated job applications in V1.

---

## 6. Development Philosophy
This project adheres to a disciplined, production-grade engineering philosophy:
1. **Build Incrementally:** Advance through clear, phased milestones.
2. **Do Not Over-Engineer:** Choose the simplest design that satisfies the current requirements.
3. **Avoid Hype-Driven Development:** Do not introduce technologies or libraries simply because they are popular.
4. **No Premature AI Agents:** Lay proper data structures, validation, and deterministic rules before introducing agentic or LLM components.
5. **No Premature Scrapers:** Structure models and requirements before building ingestion pipelines.
6. **Defer Final Architecture:** Allow data requirements and pipeline realities to inform system architecture rather than guessing upfront.
7. **Zero Unnecessary Dependencies:** Keep the dependency footprint lean, justified, and audited.
8. **Test-Driven Progress:** Every implementation step must be independently testable.
9. **Gate Milestones with Verification:** Advance to the next phase only when the current phase passes all validation tests.
10. **Portfolio-Quality & Production-Oriented:** Maintain clean code standards, comprehensive documentation, and robust error handling throughout.
11. **Transparent Technical Decisions:** Document and explain design trade-offs to foster continuous learning and auditability.

---

## 7. Current Project Status
**Current Status: Planning / Phase 0 (Requirements & Rules Frozen)**
- Project documentation and directory structure established.
- Core requirements defined in [`PROJECT_SCOPE.md`](docs/requirements/PROJECT_SCOPE.md).
- Authoritative system specification and data contracts frozen in [`V1_SPECIFICATION.md`](docs/requirements/V1_SPECIFICATION.md) and [`JOB_DATA_CONTRACT.md`](docs/requirements/JOB_DATA_CONTRACT.md).
- Deterministic filtering, deduplication, and verification rules frozen in [`JOB_FILTERS_V1.md`](rules/JOB_FILTERS_V1.md), [`DEDUPLICATION_V1.md`](rules/DEDUPLICATION_V1.md), and [`VERIFICATION_V1.md`](rules/VERIFICATION_V1.md).
- System architecture, technology stack specifications, database schema, repository structure, and decision records documented in [`SYSTEM_ARCHITECTURE_V1.md`](docs/architecture/SYSTEM_ARCHITECTURE_V1.md), [`TECH_STACK_V1.md`](docs/architecture/TECH_STACK_V1.md), [`DATABASE_SCHEMA_V1.md`](docs/architecture/DATABASE_SCHEMA_V1.md), [`REPOSITORY_STRUCTURE_V1.md`](docs/architecture/REPOSITORY_STRUCTURE_V1.md), and [`ARCHITECTURE_DECISIONS_V1.md`](docs/architecture/ARCHITECTURE_DECISIONS_V1.md).

---

## 8. Placeholder Roadmap
- [x] **Phase 0: Project Initialization & Scope Definition** (Current)
- [ ] **Phase 1: Domain Modeling & Canonical Schema Design**
- [ ] **Phase 2: Source Discovery & Ingestion Pipeline**
- [ ] **Phase 3: Normalization & Deduplication Engine**
- [ ] **Phase 4: Verification & Classification Modules**
- [ ] **Phase 5: Configurable Rules & Filtering Engine**
- [ ] **Phase 6: AI / LLM Enrichment Pipeline**
- [ ] **Phase 7: Centralized Feed & Review Workflow**

---

## 9. Portfolio Project Note
This project is an independent **personal portfolio project** created to showcase production-oriented software design, data pipeline construction, disciplined engineering hygiene, and practical AI application without unnecessary complexity.
