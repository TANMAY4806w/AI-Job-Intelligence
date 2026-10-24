"""Unit tests for Alembic migration configuration and revision scripts.

Verifies that Alembic configuration, env.py, and initial schema revision
are reproducible and can generate valid PostgreSQL DDL offline without
requiring a live PostgreSQL server.
"""

import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.persistence.models import Base


def test_alembic_config_and_script_directory() -> None:
    """Verify that alembic.ini points to the migrations directory and discovers revisions."""
    config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    assert config_path.exists(), "alembic.ini must exist in the project root"

    alembic_cfg = Config(str(config_path))
    script_dir = ScriptDirectory.from_config(alembic_cfg)
    heads = script_dir.get_heads()

    assert len(heads) == 1, f"Expected exactly 1 migration head revision, found {heads}"
    assert heads[0] == "05377f2ddb14", f"Unexpected head revision: {heads[0]}"

    for rev_id in ("091bce25752c", "0dcb0808bacc", "05377f2ddb14"):
        rev = script_dir.get_revision(rev_id)
        assert rev is not None, f"Revision {rev_id} must exist"
        assert hasattr(rev.module, "upgrade"), f"Revision {rev_id} must define upgrade()"
        assert hasattr(rev.module, "downgrade"), f"Revision {rev_id} must define downgrade()"


def test_alembic_env_targets_base_metadata() -> None:
    """Verify that migrations/env.py specifies Base.metadata covering all 11 tables."""
    env_path = Path(__file__).resolve().parents[2] / "migrations" / "env.py"
    assert env_path.exists(), "migrations/env.py must exist"

    content = env_path.read_text(encoding="utf-8")
    assert "target_metadata = Base.metadata" in content
    assert "from app.persistence.models import Base" in content

    # Verify Base metadata has all 11 tables
    assert len(Base.metadata.tables) == 11


def test_offline_sql_generation_succeeds() -> None:
    """Verify that alembic upgrade head --sql executes cleanly in offline mode."""
    root_dir = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        "--sql",
    ]
    result = subprocess.run(
        cmd,
        cwd=root_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic upgrade head --sql failed:\n{result.stderr}"
    sql_output = result.stdout

    # Verify critical DDL statements are present in emitted SQL
    assert "CREATE TABLE companies" in sql_output
    assert "CREATE TABLE sources" in sql_output
    assert "CREATE TABLE company_sources" in sql_output
    assert "CREATE TABLE raw_jobs" in sql_output
    assert "CREATE TABLE jobs" in sql_output
    assert "CREATE TABLE job_source_references" in sql_output
    assert "CREATE TABLE verification_records" in sql_output
    assert "CREATE TABLE filter_decisions" in sql_output
    assert "CREATE TABLE ai_analysis_results" in sql_output
    assert "CREATE TABLE discovery_runs" in sql_output
    assert "CREATE TABLE collection_runs" in sql_output

    # Verify partial unique indexes on company_sources
    assert "CREATE UNIQUE INDEX uq_company_sources_ats" in sql_output
    assert "CREATE UNIQUE INDEX uq_company_sources_career_url" in sql_output
    assert "CREATE UNIQUE INDEX uq_company_sources_api_endpoint" in sql_output
    assert "CREATE UNIQUE INDEX uq_company_sources_bare_mapping" in sql_output

    # Verify check constraint
    assert "chk_company_sources_has_locator" in sql_output

    # Verify deletion rules
    assert "ON DELETE RESTRICT" in sql_output
    assert "ON DELETE SET NULL" in sql_output

    # Verify exact raw body addition in forward migration
    assert "ALTER TABLE raw_jobs ADD COLUMN raw_body BYTEA NOT NULL" in sql_output

    # Verify format-aware column addition in forward migration
    assert (
        "ALTER TABLE raw_jobs ADD COLUMN payload_format VARCHAR(20) DEFAULT 'JSON' NOT NULL"
        in sql_output
    )
