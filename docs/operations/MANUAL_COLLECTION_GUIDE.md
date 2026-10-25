# Manual Greenhouse Collection Operator Guide

This guide describes how to use the operator CLI to configure public Greenhouse company boards and trigger manual collection and ingestion workflows end to end.

---

## 1. Prerequisites & Environment Setup

### 1.1 Local Development Environment
The CLI requires Python 3.13+ and a running local PostgreSQL 18 instance with migrations applied up to head (`05377f2ddb14`).

Ensure your `.env` file exists locally and points to your local development database:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/job_intelligence_dev
ENVIRONMENT=development
LOG_LEVEL=INFO
```

> [!WARNING]
> Always verify that `DATABASE_URL` targets your local development database (`job_intelligence_dev`). Never point manual collection runs against a production database, and never commit or expose your `.env` credentials.

### 1.2 Verification Commands
Before running collections, confirm that local dependencies and database schema are synchronized:

```bash
uv run alembic current
# Expected: 05377f2ddb14 (head)

uv run ruff check .
```

---

## 2. Public Greenhouse Board Tokens

### 2.1 What is a Board Token?
A Greenhouse board token is the URL slug used by companies on their public Greenhouse job board.

For example, if a company's career page URL is:
`https://boards.greenhouse.io/stripe`
`https://boards.greenhouse.io/figma`
`https://boards.greenhouse.io/hashicorp`

The respective board tokens are `stripe`, `figma`, and `hashicorp`.

### 2.2 Board Token Format & Rules
- **Format:** Alphanumeric characters with hyphens (`-`) or underscores (`_`).
- **Length:** Maximum 128 characters.
- **Verification:** You can manually verify a board token in your browser or with curl:
  `https://boards-api.greenhouse.io/v1/boards/<board_token>/jobs?content=true`
  A valid board returns JSON containing a `"jobs"` list. A nonexistent board returns HTTP 404.

> [!IMPORTANT]
> The CLI never guesses or fabricates board tokens. You must supply the exact, verified board token.

---

## 3. Operator CLI Command Syntax

The CLI entrypoint is executed via:

```bash
uv run python -m app.cli greenhouse <action> [options]
```

### 3.1 Displaying Help
```bash
# Root help
uv run python -m app.cli --help

# Greenhouse subcommand help
uv run python -m app.cli greenhouse --help

# Collect action help
uv run python -m app.cli greenhouse collect --help

# Onboard action help
uv run python -m app.cli greenhouse onboard --help

# List action help
uv run python -m app.cli greenhouse list --help
```

---

## 4. Workflows

### 4.1 Onboard and Trigger Collection in One Step
You can supply the company name and board token directly to `collect`. The CLI will automatically onboard the company and source mapping if not yet present, then trigger the harvest:

```bash
uv run python -m app.cli greenhouse collect --company-name "Figma" --board-token "figma"
```

Short flags:
```bash
uv run python -m app.cli greenhouse collect -c "Figma" -t "figma"
```

JSON output:
```bash
uv run python -m app.cli greenhouse collect -c "Figma" -t "figma" --json
```

### 4.2 Onboard Without Collecting Immediately
If you wish to configure a board mapping ahead of time:

```bash
uv run python -m app.cli greenhouse onboard --company-name "Stripe" --board-token "stripe"
```

Optional explicit slug:
```bash
uv run python -m app.cli greenhouse onboard -c "Stripe, Inc." -t "stripe" -s "stripe"
```

### 4.3 List Configured Greenhouse Boards
Inspect all registered Greenhouse company mappings:

```bash
# Table format
uv run python -m app.cli greenhouse list

# Include inactive mappings
uv run python -m app.cli greenhouse list --all

# JSON format
uv run python -m app.cli greenhouse list --json
```

### 4.4 Collect an Existing Mapping by ID
If a company board is already onboarded, you can trigger collection directly using its CompanySource UUID (`mapping-id`):

```bash
uv run python -m app.cli greenhouse collect --mapping-id "3fa85f64-5717-4562-b3fc-2c963f66afa6"
```

---

## 5. Interpreting Collection Output & Statuses

### 5.1 Text Summary Example
When a collection finishes, the CLI outputs a structured summary box:

```text
================================================================
              GREENHOUSE COLLECTION SUMMARY
================================================================
Company:             Figma
Platform:            GREENHOUSE
Board Token:         figma
Mapping ID:          f47ac10b-58cc-4372-a567-0e02b2c3d479
Collection Run ID:   6ba7b810-9dad-11d1-80b4-00c04fd430c8
Final Status:        SUCCESS
----------------------------------------------------------------
Records Discovered:  24
Records Ingested:    24
Duplicates Detected: 0
Records Failed:      0
----------------------------------------------------------------
HTTP 2xx (Success):  25
HTTP 4xx (Client):   0
HTTP 5xx (Server):   0
Duration:            3.14s
Started At:          2026-10-25 15:00:00 UTC
Finished At:         2026-10-25 15:00:03 UTC
================================================================
```

### 5.2 Status Meanings
| Status | Meaning | Exit Code | Action Needed |
| :--- | :--- | :--- | :--- |
| **`SUCCESS`** | All jobs retrieved and successfully ingested without error. | `0` | None; jobs are persisted and classified. |
| **`EMPTY`** | Board endpoint was reached, but no open jobs were listed. | `0` | None; normal for companies with no open vacancies. |
| **`PARTIAL_SUCCESS`** | Some job postings were ingested, but others failed parsing or detail fetch. | `0` | Inspect failed record counts in telemetry. |
| **`FAILED`** | Board listing could not be fetched (HTTP 404, rate limit 429, server 5xx, or network timeout). | `1` | Check board token validity, network, or rate limits. |
| **`SKIPPED_INACTIVE`** | CompanySource mapping or Greenhouse platform is marked inactive (`is_active=False`). | `1` | Activate the mapping in `company_sources` if needed. |

---

## 6. Diagnosing Failures & Inspecting Database Records

### 6.1 Diagnostic Collection Runs
Even when a collection fails (e.g. HTTP 404), the CLI persists a diagnostic `CollectionRun` record with `status="FAILED"`, error timestamps, and HTTP telemetry into PostgreSQL.

You can inspect the failed run directly in PostgreSQL using the reported `Collection Run ID`:

```sql
SELECT id, status, started_at, finished_at, telemetry
FROM collection_runs
WHERE id = '<Collection Run ID>';
```

### 6.2 Inspecting Harvested Raw Jobs
To inspect un-mutated external job responses collected during a run:

```sql
SELECT id, source_job_id, payload_hash, collected_at
FROM raw_jobs
WHERE company_source_id = '<Mapping ID>'
ORDER BY collected_at DESC;
```

### 6.3 Inspecting Canonical Ingested Jobs
To check the normalized jobs generated from collection:

```sql
SELECT id, title, location_raw, posted_at, created_at
FROM jobs
WHERE company_id = (SELECT company_id FROM company_sources WHERE id = '<Mapping ID>')
ORDER BY created_at DESC;
```
