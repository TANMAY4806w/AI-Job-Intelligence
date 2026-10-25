# Test Suite Architecture & Guidelines

This directory contains the automated test suites for **AI Job Intelligence**, structured to cleanly isolate fast, offline unit tests from PostgreSQL integration tests.

---

## Directory Organization

- `tests/unit/`: **Unit Tests**
  - Offline, isolated, and fast.
  - Requires **zero external infrastructure** (no running PostgreSQL database, no network).
  - Validates API routers, declarative SQLAlchemy ORM models, repository query construction, Alembic offline DDL generation, session lifecycle, transaction rollback, and FastAPI lifespan handlers using in-memory objects or mocks.
- `tests/integration/`: **PostgreSQL Integration Tests**
  - Requires an active, reachable PostgreSQL instance with migrations applied.
  - Connects strictly to a dedicated test database configured via `TEST_DATABASE_URL` in `.env`.
  - **Fail-Fast Isolation Guards:** Integration tests refuse to execute against `DATABASE_URL` (the development database `job_intelligence_dev`) or any protected production database to guarantee zero test data leakage into development data.
  - **Automated Database Cleanup:** Fixtures safely reset (truncate) application tables in the dedicated test database before and after each test run, leaving zero orphaned records.
- `tests/fixtures/`: Shared test data payloads and mocks.
- `tests/conftest.py`: Root pytest configuration, FastAPI `TestClient`, and integration database isolation guards.
- `tests/database_isolation.py`: Fail-fast URL validation and safe test database cleanup helpers.

---

## Running Tests

### 1. Default Unit Tests (Fast & Offline)
Runs all unit tests. Does not require PostgreSQL to be running or configured with live credentials:

```bash
uv run pytest tests/unit -v
```

> **Note:** The default pytest search path (`testpaths`) in `pyproject.toml` is configured to `tests/unit`, so running `uv run pytest -v` without arguments also executes only the unit suite.

### 2. Integration Tests (Dedicated Test Database Required)
Runs integration tests against the dedicated test database (`job_intelligence_test`):

```bash
uv run pytest tests/integration -v
```

**Prerequisites:**
- PostgreSQL service running on `localhost:5432`.
- Dedicated test database created:
  ```sql
  CREATE DATABASE job_intelligence_test;
  ```
- Dedicated test database migrated to latest revision:
  ```powershell
  $env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/job_intelligence_test"; uv run alembic upgrade head
  ```
- `TEST_DATABASE_URL` configured in `.env`:
  ```ini
  TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/job_intelligence_test
  ```

If `TEST_DATABASE_URL` is omitted or points to `job_intelligence_dev`, integration tests immediately fail-fast to protect development data.

### 3. Full Test Suite (Unit + Integration)
To run both unit and integration suites together:

```bash
uv run pytest tests -v
```
