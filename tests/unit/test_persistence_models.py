"""Unit tests for SQLAlchemy ORM persistence models.

Verifies metadata definitions, table schemas, nullability, foreign key
deletion policies, partial unique indexes, and mapper configurations
without requiring a live PostgreSQL connection.
"""

from sqlalchemy import CheckConstraint, LargeBinary, UniqueConstraint, Uuid
from sqlalchemy.orm import configure_mappers

from app.persistence.models import (
    AIAnalysisResult,
    CollectionRun,
    Company,
    CompanySource,
    DiscoveryRun,
    FilterDecision,
    Job,
    JobSourceReference,
    RawJob,
    Source,
    VerificationRecord,
    metadata,
)

EXPECTED_TABLE_NAMES = {
    "companies",
    "sources",
    "company_sources",
    "raw_jobs",
    "jobs",
    "job_source_references",
    "verification_records",
    "filter_decisions",
    "ai_analysis_results",
    "discovery_runs",
    "collection_runs",
}


def test_metadata_contains_all_11_tables() -> None:
    """Verify that metadata exports all 11 approved schema tables."""
    assert set(metadata.tables.keys()) == EXPECTED_TABLE_NAMES
    assert len(metadata.tables) == 11


def test_mapper_configuration_succeeds() -> None:
    """Verify SQLAlchemy ORM relationship mapping succeeds without circular dependencies."""
    configure_mappers()
    # Ensure classes map to the intended table names
    assert Company.__tablename__ == "companies"
    assert Source.__tablename__ == "sources"
    assert CompanySource.__tablename__ == "company_sources"
    assert RawJob.__tablename__ == "raw_jobs"
    assert Job.__tablename__ == "jobs"
    assert JobSourceReference.__tablename__ == "job_source_references"
    assert VerificationRecord.__tablename__ == "verification_records"
    assert FilterDecision.__tablename__ == "filter_decisions"
    assert AIAnalysisResult.__tablename__ == "ai_analysis_results"
    assert DiscoveryRun.__tablename__ == "discovery_runs"
    assert CollectionRun.__tablename__ == "collection_runs"


def test_all_tables_have_uuid_primary_key() -> None:
    """Verify that all 11 tables have a single UUID primary key named 'id'."""
    for table_name in EXPECTED_TABLE_NAMES:
        table = metadata.tables[table_name]
        assert "id" in table.c, f"Table {table_name} missing 'id' primary key"
        col = table.c["id"]
        assert col.primary_key is True, f"Table {table_name}.id is not primary key"
        assert isinstance(col.type, Uuid), f"Table {table_name}.id type is not Uuid"


def test_job_nullability_and_critical_dates() -> None:
    """Verify nullability rules on the canonical Job table.

    Critical requirements:
    - posted_at MUST be nullable (never substitute collected_at).
    - collected_at MUST be required (non-nullable).
    - location and normalized_location MUST be nullable (accommodate unstated locations).
    """
    jobs = metadata.tables["jobs"]

    assert jobs.c["posted_at"].nullable is True, "posted_at must be nullable"
    assert jobs.c["collected_at"].nullable is False, "collected_at must be required"
    assert jobs.c["location"].nullable is True, "location must be nullable"
    assert jobs.c["normalized_location"].nullable is True, "normalized_location must be nullable"

    # Core required fields
    assert jobs.c["company_id"].nullable is False
    assert jobs.c["company_name"].nullable is False
    assert jobs.c["title"].nullable is False
    assert jobs.c["normalized_title"].nullable is False
    assert jobs.c["description"].nullable is False
    assert jobs.c["eligibility_status"].nullable is False
    assert jobs.c["role_priority"].nullable is False
    assert jobs.c["relevance_status"].nullable is False
    assert jobs.c["verification_confidence"].nullable is False


def test_raw_job_identifier_type_and_immutability() -> None:
    """Verify source_job_id_type and payload immutability columns."""
    raw_jobs = metadata.tables["raw_jobs"]
    jsr = metadata.tables["job_source_references"]

    assert raw_jobs.c["raw_body"].nullable is False
    assert isinstance(raw_jobs.c["raw_body"].type, LargeBinary)
    assert raw_jobs.c["payload_format"].nullable is False
    assert raw_jobs.c["source_job_id_type"].nullable is False
    assert raw_jobs.c["raw_payload"].nullable is False
    assert raw_jobs.c["content_hash"].nullable is False
    assert raw_jobs.c["collected_at"].nullable is False

    assert jsr.c["source_job_id_type"].nullable is False
    assert jsr.c["is_primary"].nullable is False
    assert jsr.c["collected_at"].nullable is False
    assert jsr.c["posted_at"].nullable is True


def test_company_sources_partial_unique_indexes() -> None:
    """Verify PostgreSQL partial unique indexes on company_sources."""
    table = metadata.tables["company_sources"]
    index_names = {idx.name: idx for idx in table.indexes}

    expected_partial_indexes = {
        "uq_company_sources_ats",
        "uq_company_sources_career_url",
        "uq_company_sources_api_endpoint",
        "uq_company_sources_bare_mapping",
    }

    for name in expected_partial_indexes:
        assert name in index_names, f"Missing index {name} on company_sources"
        idx = index_names[name]
        assert idx.unique is True, f"Index {name} should be unique"
        where_clause = idx.dialect_options.get("postgresql", {}).get("where")
        assert where_clause is not None, f"Index {name} missing postgresql_where clause"


def test_company_sources_check_constraint() -> None:
    """Verify check constraint ensuring at least one locator is present."""
    table = metadata.tables["company_sources"]
    check_constraints = [c for c in table.constraints if isinstance(c, CheckConstraint)]
    constraint_names = {c.name for c in check_constraints}
    assert "chk_company_sources_has_locator" in constraint_names


def test_foreign_key_deletion_behaviors() -> None:
    """Verify foreign key referential integrity constraints and ondelete policies."""
    fk_expectations = {
        # (table, foreign_col): (target_table.col, ondelete)
        ("company_sources", "company_id"): ("companies.id", "RESTRICT"),
        ("company_sources", "source_id"): ("sources.id", "RESTRICT"),
        ("raw_jobs", "company_source_id"): ("company_sources.id", "RESTRICT"),
        ("raw_jobs", "collection_run_id"): ("collection_runs.id", "SET NULL"),
        ("jobs", "company_id"): ("companies.id", "RESTRICT"),
        ("job_source_references", "job_id"): ("jobs.id", "RESTRICT"),
        ("job_source_references", "raw_job_id"): ("raw_jobs.id", "RESTRICT"),
        ("job_source_references", "source_id"): ("sources.id", "RESTRICT"),
        ("verification_records", "job_id"): ("jobs.id", "RESTRICT"),
        ("filter_decisions", "job_id"): ("jobs.id", "RESTRICT"),
        ("ai_analysis_results", "job_id"): ("jobs.id", "SET NULL"),
        ("collection_runs", "source_id"): ("sources.id", "SET NULL"),
        ("collection_runs", "company_source_id"): ("company_sources.id", "SET NULL"),
    }

    for (table_name, col_name), (target, expected_ondelete) in fk_expectations.items():
        table = metadata.tables[table_name]
        col = table.c[col_name]
        assert len(col.foreign_keys) == 1, f"Missing FK on {table_name}.{col_name}"
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == target, (
            f"{table_name}.{col_name} targets {fk.target_fullname}, expected {target}"
        )
        assert (fk.ondelete or "").upper() == expected_ondelete, (
            f"{table_name}.{col_name} ondelete is {fk.ondelete}, expected {expected_ondelete}"
        )


def test_job_source_reference_uniqueness() -> None:
    """Verify UNIQUE (source_id, source_job_id) on job_source_references."""
    table = metadata.tables["job_source_references"]
    unique_constraints = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    matched = False
    for uc in unique_constraints:
        cols = {col.name for col in uc.columns}
        if cols == {"source_id", "source_job_id"}:
            matched = True
            break
    assert matched, (
        "Missing unique constraint on (source_id, source_job_id) in job_source_references"
    )
