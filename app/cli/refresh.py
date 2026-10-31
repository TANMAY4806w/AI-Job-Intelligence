"""CLI command execution handler for controlled persistence refresh."""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.reevaluate import format_table
from app.orchestration.refresh_service import (
    ALLOWED_MUTATING_DATABASES,
    DEV_AUTHORIZATION_TOKEN_ENV_VAR,
    DEV_DATABASE_NAME,
    EXPECTED_DEV_AUTHORIZATION_TOKEN,
    JobRefreshService,
    RefreshPlan,
    RefreshResult,
)

logger = logging.getLogger("app.cli.refresh")


def print_preview_report(plan: RefreshPlan) -> None:
    """Print structured, human-readable terminal preview plan."""
    print("=" * 86)
    print("          AI-JOB-INTELLIGENCE: CONTROLLED PERSISTENCE REFRESH (PREVIEW)")
    print("=" * 86)
    print(f"Target Company:            {plan.company_name}")
    print(f"Company ID:                {plan.company_id}")
    print(f"Connected Database:        {plan.current_database}")
    print("Execution Mode:            PREVIEW (Read-only, 0 writes executed)")
    print(f"Total Canonical Jobs:      {plan.total_jobs}")
    print(f"Jobs Requiring Update:     {plan.jobs_to_update}")
    print(f"Jobs Already Up-to-Date:   {plan.jobs_to_skip}")
    print(f"Verification Rows to Add:  {plan.verification_records_to_insert}")
    print(f"Filter Decisions to Add:   {plan.filter_decisions_to_insert}")
    print("=" * 86)
    print()

    # Status Transitions
    print("1. PROPOSED STATUS TRANSITIONS")
    if not plan.status_transitions:
        print("  None. All jobs maintain identical operational status.")
    else:
        trans_rows = [[k, str(v)] for k, v in sorted(plan.status_transitions.items())]
        print(format_table(["Transition", "Job Count"], trans_rows, [40, 15]))
    print()

    # Detail on jobs that require updates
    changed_items = [item for item in plan.items if item.action == "UPDATE"]
    print(f"2. JOBS REQUIRING REFRESH ({len(changed_items)} Total)")
    if not changed_items:
        print("  All jobs are already synchronized with latest verification and filter rules.")
    else:
        # Group by status change vs confidence-only change
        status_changes = [item for item in changed_items if item.status_changed]
        conf_changes = [item for item in changed_items if not item.status_changed]

        if status_changes:
            print("  [A] Operational Status & Reason Transitions:")
            for sc in status_changes:
                p_reason = sc.persisted_rejection_reason or sc.persisted_review_reason or "NONE"
                pr_reason = sc.proposed_rejection_reason or sc.proposed_review_reason or "NONE"
                print(f"      • {sc.title}")
                print(f"        Location:   {sc.location or 'N/A'}")
                print(f"        Persisted:  Status={sc.persisted_status} (Reason={p_reason})")
                print(f"        Proposed:   Status={sc.proposed_status} (Reason={pr_reason})")
                print(f"        Confidence: {sc.persisted_confidence} -> {sc.proposed_confidence}")
            print()

        print("  [B] Summary of Confidence and Source Validation Updates:")
        print(f"      • Jobs with updated source validation & confidence: {len(changed_items)}")
        if conf_changes:
            sample = conf_changes[:3]
            for c in sample:
                print(
                    f"        - {c.title[:45]:<45} (Conf: {c.persisted_confidence} -> {c.proposed_confidence})"
                )
            if len(conf_changes) > 3:
                print(f"        ... and {len(conf_changes) - 3} more jobs.")
    print()
    print("=" * 86)
    print("  PREVIEW COMPLETE: Zero database modifications were executed.")
    print("  To apply these changes transactionally, run with:")
    print(
        f'    python -m app.cli refresh -c "{plan.company_name}" --apply --target-database <DB_NAME>'
    )
    print("=" * 86)


def print_applied_report(result: RefreshResult) -> None:
    """Print structured terminal report for applied persistence refresh."""
    print("=" * 86)
    print("          AI-JOB-INTELLIGENCE: CONTROLLED PERSISTENCE REFRESH (APPLIED)")
    print("=" * 86)
    print(f"Target Company:                 {result.company_name}")
    print(f"Company ID:                     {result.company_id}")
    print(f"Target Database:                {result.target_database}")
    print(f"Execution Status:               {result.status}")
    print(
        f"Refreshed Timestamp:            {result.refreshed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    print(f"Total Canonical Jobs:           {result.total_jobs}")
    print(f"Canonical Jobs Updated:         {result.jobs_updated}")
    print(f"Jobs Skipped (Already Current): {result.jobs_skipped}")
    print(f"Verification Records Inserted:  {result.verification_records_inserted}")
    print(f"Filter Decisions Inserted:      {result.filter_decisions_inserted}")
    print("=" * 86)
    if result.status == "SKIPPED_ALREADY_APPLIED":
        print("  NOTICE: All jobs were already up to date. No database writes were performed.")
    else:
        print("  SUCCESS: Transaction committed successfully. Audit history preserved.")
    print("=" * 86)


async def execute_refresh_workflow(
    session: AsyncSession,
    *,
    company_name: str | None = None,
    company_id: str | None = None,
    preview: bool = True,
    apply: bool = False,
    target_database: str | None = None,
    allow_database_override: str | None = None,
    output_json: bool = False,
    output_file: str | None = None,
) -> tuple[RefreshPlan | RefreshResult | None, int]:
    """Execute refresh workflow (preview or apply) with safety boundaries."""
    target_identifier: uuid.UUID | str
    if company_id:
        try:
            target_identifier = uuid.UUID(company_id.strip())
        except ValueError:
            print(f"ERROR: Invalid company-id UUID '{company_id}'.", file=sys.stderr)
            return None, 2
    elif company_name:
        target_identifier = company_name.strip()
    else:
        print("ERROR: Specify either --company-name or --company-id.", file=sys.stderr)
        return None, 2

    service = JobRefreshService()

    # 1. Preview Mode (Default when --apply is not set)
    if not apply:
        try:
            plan = await service.preview_refresh(
                session=session,
                company_id_or_name=target_identifier,
                target_database=target_database,
            )
        except ValueError as val_err:
            print(f"ERROR: {val_err}", file=sys.stderr)
            return None, 1

        plan_dict = plan.to_dict()
        if output_file:
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(plan_dict, f, indent=2)
            except OSError as exc:
                print(f"ERROR: Failed writing output file '{output_file}': {exc}", file=sys.stderr)
                return None, 1

        if output_json:
            print(json.dumps(plan_dict, indent=2))
        else:
            print_preview_report(plan)
            if output_file:
                print(f"Detailed JSON plan written to: {output_file}")

        return plan, 0

    # 2. Mutating Apply Mode
    if not target_database or not target_database.strip():
        print(
            "ERROR: --target-database is required when executing --apply. "
            "Specify the exact expected database name (e.g. --target-database job_intelligence_test).",
            file=sys.stderr,
        )
        return None, 2

    target_db_norm = target_database.strip().lower()
    is_dev_target = target_db_norm == DEV_DATABASE_NAME

    # Validate database allowlist / scoped override
    if not is_dev_target and target_db_norm not in ALLOWED_MUTATING_DATABASES:
        print(
            f"FATAL ERROR: Mutating refresh is strictly restricted to allowlisted test database "
            f"{sorted(ALLOWED_MUTATING_DATABASES)}. Target database '{target_database}' is forbidden.",
            file=sys.stderr,
        )
        return None, 2

    if is_dev_target:
        override_norm = (allow_database_override or "").strip().lower()
        if override_norm != DEV_DATABASE_NAME:
            print(
                f"FATAL ERROR: Mutating refresh against '{DEV_DATABASE_NAME}' requires explicit authorization. "
                f"Pass --allow-database-override {DEV_DATABASE_NAME} and set {DEV_AUTHORIZATION_TOKEN_ENV_VAR}.",
                file=sys.stderr,
            )
            return None, 2

        env_token = os.environ.get(DEV_AUTHORIZATION_TOKEN_ENV_VAR)
        if env_token != EXPECTED_DEV_AUTHORIZATION_TOKEN:
            print(
                f"FATAL ERROR: Missing or invalid {DEV_AUTHORIZATION_TOKEN_ENV_VAR} environment variable. "
                f"Refusing mutating execution against '{DEV_DATABASE_NAME}'.",
                file=sys.stderr,
            )
            return None, 2

    try:
        result = await service.apply_refresh(
            session=session,
            company_id_or_name=target_identifier,
            expected_database=target_database.strip(),
            dry_run=False,
            allow_database_override=allow_database_override,
        )
        # Commit transaction
        await session.commit()
    except (ValueError, PermissionError, RuntimeError) as err:
        await session.rollback()
        print(f"ERROR: {err}", file=sys.stderr)
        return None, 1
    except Exception as exc:
        await session.rollback()
        logger.exception("Unexpected error during refresh execution: %s", exc)
        print(f"FATAL ERROR: {exc}", file=sys.stderr)
        return None, 1

    result_dict = result.to_dict()
    if output_file:
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result_dict, f, indent=2)
        except OSError as exc:
            logger.warning(
                "Refresh committed successfully to database, but writing output file '%s' failed: %s",
                output_file,
                exc,
            )
            print(
                f"WARNING: Persistence refresh was successfully committed to the database, "
                f"but writing output file '{output_file}' failed: {exc}",
                file=sys.stderr,
            )

    if output_json:
        print(json.dumps(result_dict, indent=2))
    else:
        print_applied_report(result)
        if output_file:
            print(f"Detailed JSON result written to: {output_file}")

    return result, 0
