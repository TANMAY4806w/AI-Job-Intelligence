# Test Suite Architecture & Guidelines

This directory contains the automated test suites for **AI Job Intelligence**, structured to cleanly isolate fast, offline unit tests from PostgreSQL integration tests.

---

## Directory Organization

- `tests/unit/`: **Unit Tests**
  - Offline, isolated, and fast.
  - Requires **zero external infrastructure** (no running PostgreSQL database, no network).
  - Validates API routers, declarative SQLAlchemy ORM models, Alembic offline DDL generation, session lifecycle, transaction rollback, and FastAPI lifespan handlers using in-memory objects or mocks.
- `tests/integration/`: **PostgreSQL Integration Tests**
  - Requires an active, reachable PostgreSQL instance with migrations applied.
  - Connects using settings configured in `.env` (`DATABASE_URL`).
  - Verifies live connectivity, schema integrity, and ORM insertion, flushing, and querying.
  - **Transaction Isolation:** Uses uncommitted transactions or explicit rollback guarantees to ensure zero persistent test data leaks into the database.
- `tests/fixtures/`: Shared test data payloads and mocks.
- `tests/conftest.py`: Root pytest fixtures (e.g., FastAPI `TestClient`).

---

## Running Tests

### 1. Default Unit Tests (Fast & Offline)
Runs all unit tests. Does not require PostgreSQL to be running or configured with live credentials:

```bash
uv run pytest tests/unit -v
```

> **Note:** The default pytest search path (`testpaths`) in `pyproject.toml` is configured to `tests/unit`, so running `uv run pytest -v` without arguments also executes only the unit suite.

### 2. Integration Tests (PostgreSQL Required)
Runs integration tests against the local database (`job_intelligence_dev`):

```bash
uv run pytest tests/integration -v
```

**Prerequisites:**
- PostgreSQL service running on `localhost:5432`.
- Target database migrated (`uv run alembic upgrade head`).
- Valid connection string configured in `.env` (e.g., `postgresql+asyncpg://...`).

If PostgreSQL is not running or credentials fail, integration tests report clear diagnostic errors.

### 3. Full Test Suite (Unit + Integration)
To run both unit and integration suites together:

```bash
uv run pytest tests -v
```
