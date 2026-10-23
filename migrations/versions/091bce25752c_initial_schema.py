"""Initial database migration defining all 11 persistence schema tables.

Revision ID: 091bce25752c
Revises:
Create Date: 2026-10-21 01:04:44.868229
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "091bce25752c"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all 11 approved schema tables, constraints, foreign keys, and indexes."""
    # 1. companies
    op.create_table(
        "companies",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("website", sa.String(length=512), nullable=True),
        sa.Column("industry", sa.String(length=128), nullable=True),
        sa.Column(
            "sector",
            sa.String(length=32),
            server_default=sa.text("'UNKNOWN'"),
            nullable=False,
        ),
        sa.Column(
            "business_model",
            sa.String(length=32),
            server_default=sa.text("'UNKNOWN'"),
            nullable=False,
        ),
        sa.Column(
            "is_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("verification_source", sa.String(length=128), nullable=True),
        sa.Column(
            "confidence",
            sa.Numeric(precision=3, scale=2),
            server_default=sa.text("0.50"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name"),
    )
    op.create_index("idx_companies_normalized_name", "companies", ["normalized_name"])
    op.create_index(
        "idx_companies_sector_model",
        "companies",
        ["sector", "business_model"],
    )
    op.create_index("idx_companies_verified", "companies", ["is_verified"])

    # 2. sources
    op.create_table(
        "sources",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "rate_limit_rpm",
            sa.Integer(),
            server_default=sa.text("60"),
            nullable=False,
        ),
        sa.Column(
            "requires_browser",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_name"),
    )
    op.create_index("idx_sources_active", "sources", ["is_active", "source_type"])

    # 3. discovery_runs
    op.create_table(
        "discovery_runs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("discovery_methods", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "companies_probed",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "endpoints_discovered",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "errors_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("telemetry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_discovery_runs_time",
        "discovery_runs",
        [sa.text("started_at DESC")],
    )

    # 4. company_sources
    op.create_table(
        "company_sources",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("ats_identifier", sa.String(length=128), nullable=True),
        sa.Column("career_url", sa.Text(), nullable=True),
        sa.Column("api_endpoint", sa.Text(), nullable=True),
        sa.Column("discovery_method", sa.String(length=64), nullable=False),
        sa.Column(
            "confidence",
            sa.Numeric(precision=3, scale=2),
            server_default=sa.text("1.00"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ats_identifier IS NOT NULL OR career_url IS NOT NULL OR api_endpoint IS NOT NULL",
            name="chk_company_sources_has_locator",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Partial unique indexes
    op.create_index(
        "uq_company_sources_ats",
        "company_sources",
        ["company_id", "source_id", "ats_identifier"],
        unique=True,
        postgresql_where=sa.text("ats_identifier IS NOT NULL"),
    )
    op.create_index(
        "uq_company_sources_career_url",
        "company_sources",
        ["company_id", "source_id", "career_url"],
        unique=True,
        postgresql_where=sa.text("ats_identifier IS NULL AND career_url IS NOT NULL"),
    )
    op.create_index(
        "uq_company_sources_api_endpoint",
        "company_sources",
        ["company_id", "source_id", "api_endpoint"],
        unique=True,
        postgresql_where=sa.text(
            "ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_company_sources_bare_mapping",
        "company_sources",
        ["company_id", "source_id"],
        unique=True,
        postgresql_where=sa.text(
            "ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NULL"
        ),
    )
    # Operational indexes
    op.create_index(
        "idx_company_sources_lookup",
        "company_sources",
        ["company_id", "source_id"],
    )
    op.create_index(
        "idx_company_sources_ats",
        "company_sources",
        ["source_id", "ats_identifier"],
        postgresql_where=sa.text("ats_identifier IS NOT NULL"),
    )
    op.create_index(
        "idx_company_sources_schedule",
        "company_sources",
        ["is_active", "last_collected_at"],
    )

    # 5. collection_runs
    op.create_table(
        "collection_runs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("company_source_id", sa.Uuid(), nullable=True),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "records_discovered",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "records_ingested",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "records_failed",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "duplicates_detected",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "http_2xx_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "http_4xx_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "http_5xx_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("telemetry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_source_id"],
            ["company_sources.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_collection_runs_time",
        "collection_runs",
        [sa.text("started_at DESC")],
    )
    op.create_index(
        "idx_collection_runs_status",
        "collection_runs",
        ["status", "run_type"],
    )

    # 6. raw_jobs
    op.create_table(
        "raw_jobs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("company_source_id", sa.Uuid(), nullable=False),
        sa.Column("collection_run_id", sa.Uuid(), nullable=True),
        sa.Column("source_job_id", sa.String(length=255), nullable=False),
        sa.Column(
            "source_job_id_type",
            sa.String(length=20),
            server_default=sa.text("'NATIVE'"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            ["collection_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_source_id"],
            ["company_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_raw_jobs_comp_src_ext",
        "raw_jobs",
        ["company_source_id", "source_job_id"],
    )
    op.create_index("idx_raw_jobs_content_hash", "raw_jobs", ["content_hash"])
    op.create_index("idx_raw_jobs_collected_at", "raw_jobs", ["collected_at"])
    op.create_index("idx_raw_jobs_run", "raw_jobs", ["collection_run_id"])

    # 7. jobs
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("normalized_title", sa.String(length=255), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("normalized_location", sa.String(length=255), nullable=True),
        sa.Column("employment_type", sa.String(length=32), nullable=True),
        sa.Column(
            "remote_type",
            sa.String(length=32),
            server_default=sa.text("'UNKNOWN'"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("responsibilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requirements", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("experience_raw", sa.String(length=128), nullable=True),
        sa.Column("experience_min_years", sa.Numeric(precision=3, scale=1), nullable=True),
        sa.Column("experience_max_years", sa.Numeric(precision=3, scale=1), nullable=True),
        sa.Column("salary_min", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("salary_max", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "salary_currency",
            sa.String(length=3),
            server_default=sa.text("'INR'"),
            nullable=True,
        ),
        sa.Column("salary_period", sa.String(length=16), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("application_url", sa.Text(), nullable=True),
        sa.Column(
            "application_status",
            sa.String(length=32),
            server_default=sa.text("'NOT_APPLIED'"),
            nullable=False,
        ),
        sa.Column("role_priority", sa.String(length=16), nullable=False),
        sa.Column("relevance_status", sa.String(length=16), nullable=False),
        sa.Column(
            "eligibility_status",
            sa.String(length=32),
            server_default=sa.text("'NEW'"),
            nullable=False,
        ),
        sa.Column("rejection_reason", sa.String(length=64), nullable=True),
        sa.Column("review_reason", sa.String(length=64), nullable=True),
        sa.Column("source_verified", sa.Boolean(), nullable=False),
        sa.Column("jd_verified", sa.Boolean(), nullable=False),
        sa.Column("company_verified", sa.Boolean(), nullable=False),
        sa.Column("location_verified", sa.Boolean(), nullable=False),
        sa.Column("experience_verified", sa.Boolean(), nullable=False),
        sa.Column("salary_verified", sa.Boolean(), nullable=False),
        sa.Column("verification_confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_jobs_eligibility_priority",
        "jobs",
        ["eligibility_status", "role_priority"],
    )
    op.create_index(
        "idx_jobs_posted_at",
        "jobs",
        [sa.text("posted_at DESC NULLS LAST")],
    )
    op.create_index("idx_jobs_company_id", "jobs", ["company_id"])
    op.create_index(
        "idx_jobs_dedup_composite",
        "jobs",
        ["company_id", "normalized_title", "normalized_location"],
    )
    op.create_index(
        "idx_jobs_verification",
        "jobs",
        ["verification_confidence", "jd_verified"],
    )

    # 8. job_source_references
    op.create_table(
        "job_source_references",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("raw_job_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_job_id", sa.String(length=255), nullable=False),
        sa.Column(
            "source_job_id_type",
            sa.String(length=20),
            server_default=sa.text("'NATIVE'"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("application_url", sa.Text(), nullable=True),
        sa.Column("extracted_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["raw_job_id"], ["raw_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "source_job_id", name="uq_job_source_references_source_ext"
        ),
    )
    op.create_index("idx_job_source_ref_job", "job_source_references", ["job_id"])
    op.create_index("idx_job_source_ref_raw", "job_source_references", ["raw_job_id"])
    op.create_index(
        "idx_job_source_ref_app_url",
        "job_source_references",
        ["application_url"],
    )

    # 9. verification_records
    op.create_table(
        "verification_records",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "verification_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source_verified", sa.Boolean(), nullable=False),
        sa.Column("jd_verified", sa.Boolean(), nullable=False),
        sa.Column("application_url_verified", sa.Boolean(), nullable=False),
        sa.Column("company_verified", sa.Boolean(), nullable=False),
        sa.Column("location_verified", sa.Boolean(), nullable=False),
        sa.Column("experience_verified", sa.Boolean(), nullable=False),
        sa.Column("salary_verified", sa.Boolean(), nullable=False),
        sa.Column("posting_date_verified", sa.Boolean(), nullable=False),
        sa.Column("verification_confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failure_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("review_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("audit_trail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_verification_job_time",
        "verification_records",
        ["job_id", sa.text("verification_run_at DESC")],
    )
    op.create_index(
        "idx_verification_confidence",
        "verification_records",
        ["verification_confidence"],
    )

    # 10. filter_decisions
    op.create_table(
        "filter_decisions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("final_status", sa.String(length=32), nullable=False),
        sa.Column("role_priority", sa.String(length=16), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "evaluation_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_filter_decisions_job_time",
        "filter_decisions",
        ["job_id", sa.text("evaluated_at DESC")],
    )
    op.create_index(
        "idx_filter_decisions_version_status",
        "filter_decisions",
        ["rule_version", "final_status"],
    )

    # 11. ai_analysis_results
    op.create_table(
        "ai_analysis_results",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "structured_output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "is_fallback",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cache_key"),
    )
    op.create_index("idx_ai_cache_key", "ai_analysis_results", ["cache_key"])
    op.create_index(
        "idx_ai_provider_model",
        "ai_analysis_results",
        ["provider", "model"],
    )


def downgrade() -> None:
    """Drop all 11 schema tables in reverse dependency order."""
    op.drop_table("ai_analysis_results")
    op.drop_table("filter_decisions")
    op.drop_table("verification_records")
    op.drop_table("job_source_references")
    op.drop_table("jobs")
    op.drop_table("raw_jobs")
    op.drop_table("collection_runs")
    op.drop_table("company_sources")
    op.drop_table("discovery_runs")
    op.drop_table("sources")
    op.drop_table("companies")
