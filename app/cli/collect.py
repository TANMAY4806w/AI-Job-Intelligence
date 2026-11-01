"""Generic bulk collection CLI command handler.

Boundary: Orchestrates bulk job harvesting across registered company endpoints:
1. Loads verified company entries from registry.
2. Supports three clearly distinguished operational modes:
   - Mode A: Plan-Only Dry Run (--dry-run): 0 network calls, 0 DB queries or mutations.
   - Mode B: Live Preview (--preview-live): Read-only public ATS requests, 0 DB writes.
   - Mode C: Apply (--apply): Mutating harvest and DB persistence with company-level atomicity.
3. Enforces strict database identity verification & complete authorization separation:
   - Bulk collection is strictly restricted to allowlisted test databases: {'job_intelligence_test'}.
   - Execution against 'job_intelligence_dev' is unconditionally rejected under all circumstances.
   - Database overrides and refresh environment confirmation tokens are strictly ignored/rejected.
   - Unknown, production, and staging databases remain forbidden.
   - The actual connected database is always verified via SQL SELECT current_database().
4. Company-level atomicity: Each company executes inside its own transaction. If company A
   succeeds and company B fails, company A's data remains committed to PostgreSQL.
5. Accurate status reporting: Empty boards are reported as STATUS_EMPTY ('EMPTY'), not SUCCESS.
6. A batch never reports full success ('COMPLETED') if any company failed or was truncated.
"""

import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
)
from app.collectors.base.http import PoliteHttpClient
from app.discovery.company_registry import (
    DEFAULT_REGISTRY_PATH,
    CompanyRegistry,
    CompanyRegistryEntry,
)
from app.orchestration.collection_runner import (
    STATUS_SKIPPED_INACTIVE,
    STATUS_UNSUPPORTED_SOURCE,
    CollectionRunner,
    CollectionRunnerResult,
    MappingCollectionOutcome,
)
from app.persistence.session import session_scope

logger = logging.getLogger("app.cli.collect")

# Restrict mutating bulk collection strictly to isolated test databases only
ALLOWED_COLLECT_DATABASES: frozenset[str] = frozenset({"job_intelligence_test"})
DEV_DATABASE_NAME: str = "job_intelligence_dev"


def format_bulk_summary(
    runner_result: CollectionRunnerResult,
    entries: list[CompanyRegistryEntry],
    elapsed_seconds: float,
) -> str:
    """Format human-readable summary of bulk collection execution.

    Args:
        runner_result: CollectionRunnerResult aggregating mapping outcomes.
        entries: List of targeted company registry entries.
        elapsed_seconds: Total wall-clock time in seconds.

    Returns:
        str: Formatted multi-line summary table.
    """
    total_empty = len([o for o in runner_result.mapping_outcomes if o.status == STATUS_EMPTY])
    total_succeeded = len([o for o in runner_result.mapping_outcomes if o.status == STATUS_SUCCESS])
    total_partial = len(
        [o for o in runner_result.mapping_outcomes if o.status == STATUS_PARTIAL_SUCCESS]
    )
    total_failed = len(
        [
            o
            for o in runner_result.mapping_outcomes
            if o.status in (STATUS_FAILED, STATUS_UNSUPPORTED_SOURCE)
        ]
    )
    total_skipped = len(
        [o for o in runner_result.mapping_outcomes if o.status == STATUS_SKIPPED_INACTIVE]
    )

    lines = [
        "=" * 80,
        "BULK ATS COLLECTION SUMMARY",
        "=" * 80,
        f"Execution Time:    {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Duration:          {elapsed_seconds:.2f} seconds",
        f"Companies Planned: {len(entries)}",
        f"Mappings Executed: {len(runner_result.mapping_outcomes)}",
        f"  - Succeeded:     {total_succeeded}",
        f"  - Empty Boards:  {total_empty}",
        f"  - Partial/Trunc: {total_partial}",
        f"  - Failed:        {total_failed}",
        f"  - Skipped:       {total_skipped}",
        "-" * 80,
        f"Total Discovered:  {runner_result.records_discovered}",
        f"Total Ingested:    {runner_result.records_ingested}",
        f"  - New Canonical: {runner_result.records_ingested - runner_result.duplicates_detected}",
        f"  - Duplicates:    {runner_result.duplicates_detected}",
        f"Total Failed Jobs: {runner_result.records_failed}",
        "-" * 80,
        f"{'Company':<20} | {'ATS':<10} | {'Status':<15} | {'Discovered':<10} | {'Ingested':<10} | {'Failed':<8}",
        "-" * 80,
    ]

    for outcome in runner_result.mapping_outcomes:
        c_name = outcome.company_name or "Unknown"
        ats = (outcome.ats_identifier or "")[:10]
        status = outcome.status
        disc = outcome.records_discovered
        ing = outcome.records_ingested
        fail = outcome.records_failed

        lines.append(
            f"{c_name:<20} | {ats:<10} | {status:<15} | {disc:<10} | {ing:<10} | {fail:<8}"
        )
        if outcome.error_message and outcome.status in (
            STATUS_FAILED,
            STATUS_UNSUPPORTED_SOURCE,
            STATUS_PARTIAL_SUCCESS,
        ):
            lines.append(f"  └─ DIAGNOSTIC: {outcome.error_message[:70]}")

    lines.append("=" * 80)
    return "\n".join(lines)


def format_bulk_json(
    runner_result: CollectionRunnerResult,
    entries: list[CompanyRegistryEntry],
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Format structured dictionary of bulk collection execution.

    Args:
        runner_result: CollectionRunnerResult aggregating mapping outcomes.
        entries: List of targeted company registry entries.
        elapsed_seconds: Total wall-clock time in seconds.

    Returns:
        dict[str, Any]: JSON-serializable report.
    """
    mapping_reports = []
    for outcome in runner_result.mapping_outcomes:
        mapping_reports.append(
            {
                "company_name": outcome.company_name,
                "company_source_id": str(outcome.company_source_id),
                "source_id": str(outcome.source_id) if outcome.source_id else None,
                "ats_identifier": outcome.ats_identifier,
                "status": outcome.status,
                "collection_run_id": str(outcome.collection_run_id)
                if outcome.collection_run_id
                else None,
                "records_discovered": outcome.records_discovered,
                "records_ingested": outcome.records_ingested,
                "records_failed": outcome.records_failed,
                "duplicates_detected": outcome.duplicates_detected,
                "error_message": outcome.error_message,
            }
        )

    has_failed = any(
        o.status in (STATUS_FAILED, STATUS_UNSUPPORTED_SOURCE)
        for o in runner_result.mapping_outcomes
    )
    has_partial = any(o.status == STATUS_PARTIAL_SUCCESS for o in runner_result.mapping_outcomes)

    if has_failed:
        batch_status = "PARTIAL_FAILURE"
    elif has_partial:
        batch_status = "PARTIAL_SUCCESS"
    else:
        batch_status = "COMPLETED"

    total_empty = len([o for o in runner_result.mapping_outcomes if o.status == STATUS_EMPTY])
    total_succeeded = len([o for o in runner_result.mapping_outcomes if o.status == STATUS_SUCCESS])
    total_partial = len(
        [o for o in runner_result.mapping_outcomes if o.status == STATUS_PARTIAL_SUCCESS]
    )

    return {
        "status": batch_status,
        "timestamp": datetime.now(UTC).isoformat(),
        "duration_seconds": round(elapsed_seconds, 2),
        "total_companies_planned": len(entries),
        "total_mappings_processed": runner_result.total_mappings_processed,
        "total_mappings_succeeded": total_succeeded,
        "total_mappings_empty": total_empty,
        "total_mappings_partial": total_partial,
        "total_mappings_failed": runner_result.total_mappings_failed,
        "total_mappings_skipped": runner_result.total_mappings_skipped,
        "records_discovered": runner_result.records_discovered,
        "records_ingested": runner_result.records_ingested,
        "records_new": runner_result.records_ingested - runner_result.duplicates_detected,
        "duplicates_detected": runner_result.duplicates_detected,
        "records_failed": runner_result.records_failed,
        "mappings": mapping_reports,
    }


async def preview_live_endpoints(
    entries: list[CompanyRegistryEntry],
    http_client: PoliteHttpClient | None = None,
) -> list[dict[str, Any]]:
    """Execute read-only public ATS HTTP queries without database access.

    Args:
        entries: Targeted CompanyRegistryEntry items.
        http_client: Optional PoliteHttpClient instance.

    Returns:
        list[dict[str, Any]]: Discovered counts and diagnostic details per company.
    """
    client = http_client or PoliteHttpClient()
    previews: list[dict[str, Any]] = []

    for entry in entries:
        token = entry.board_identifier
        prov = entry.ats_provider.upper()
        res = {
            "name": entry.name,
            "slug": entry.slug,
            "ats_provider": entry.ats_provider,
            "board_identifier": token,
            "status": "SUCCESS",
            "total_discovered": 0,
            "details": "",
            "error_message": None,
        }

        try:
            if prov == "GREENHOUSE":
                url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    jobs = data.get("jobs", []) if isinstance(data, dict) else []
                    res["total_discovered"] = len(jobs)
                    if len(jobs) == 0:
                        res["status"] = "EMPTY"
                        res["details"] = "0 vacancies (empty board)"
                    else:
                        res["status"] = "SUCCESS"
                        res["details"] = f"{len(jobs)} public vacancies"
                else:
                    res["status"] = "FAILED"
                    res["error_message"] = f"HTTP {resp.status_code}"
            elif prov == "LEVER":
                skip = 0
                limit = 100
                pages = 0
                seen_ids: set[str] = set()
                is_trunc = False

                while pages < 50:
                    url = f"https://api.lever.co/v0/postings/{token}?mode=json&skip={skip}&limit={limit}"
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        if pages == 0:
                            res["status"] = "FAILED"
                            res["error_message"] = f"HTTP {resp.status_code}"
                        break
                    p_data = resp.json()
                    if not isinstance(p_data, list) or not p_data:
                        break
                    for job in p_data:
                        if isinstance(job, dict):
                            jid = str(job.get("id") or "").strip()
                            if jid and jid not in seen_ids:
                                seen_ids.add(jid)
                    pages += 1
                    if len(p_data) < limit:
                        break
                    skip += len(p_data)
                    if pages >= 50 and len(p_data) == limit:
                        is_trunc = True

                if res["status"] != "FAILED":
                    res["total_discovered"] = len(seen_ids)
                    if len(seen_ids) == 0:
                        res["status"] = "EMPTY"
                        res["details"] = "0 vacancies (empty board)"
                    else:
                        trunc_str = " (truncated at 50 pages)" if is_trunc else ""
                        res["status"] = "PARTIAL_SUCCESS" if is_trunc else "SUCCESS"
                        res["details"] = (
                            f"{len(seen_ids)} public vacancies across {pages} page(s){trunc_str}"
                        )
            elif prov == "ASHBY":
                url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    all_jobs = data.get("jobs", []) if isinstance(data, dict) else []
                    listed = [
                        j
                        for j in all_jobs
                        if isinstance(j, dict) and j.get("isListed") is not False
                    ]
                    unlisted = len(all_jobs) - len(listed)
                    res["total_discovered"] = len(listed)
                    if len(listed) == 0:
                        res["status"] = "EMPTY"
                        res["details"] = (
                            f"0 listed vacancies ({unlisted} unlisted; empty board)"
                            if unlisted > 0
                            else "0 vacancies (empty board)"
                        )
                    else:
                        res["status"] = "SUCCESS"
                        res["details"] = f"{len(listed)} listed vacancies ({unlisted} unlisted)"
                else:
                    res["status"] = "FAILED"
                    res["error_message"] = f"HTTP {resp.status_code}"
            else:
                res["status"] = "UNSUPPORTED"
                res["error_message"] = f"Unsupported provider '{prov}'"
        except Exception as exc:
            res["status"] = "FAILED"
            res["error_message"] = str(exc)

        previews.append(res)

    return previews


async def execute_bulk_collection(
    *,
    registry_file: str | Path | None = None,
    provider: str | None = None,
    companies: list[str] | None = None,
    include_inactive: bool = False,
    dry_run: bool = False,
    preview_live: bool = False,
    apply: bool = False,
    target_database: str | None = None,
    allow_database_override: str | None = None,
    as_json: bool = False,
    output_file: str | None = None,
    run_type: str = "MANUAL_TRIGGER",
    http_client: PoliteHttpClient | None = None,
) -> int:
    """Execute bulk ATS collection across registered companies.

    Operational Modes:
    1. Plan-Only Dry Run (--dry-run or default): 0 network calls, 0 DB queries or writes.
    2. Live Preview (--preview-live): Read-only public ATS calls, 0 DB writes.
    3. Apply (--apply): Mutating harvest and DB persistence with company-level atomicity.

    Authorization Invariants:
    - Mutating bulk collection is strictly restricted to ALLOWED_COLLECT_DATABASES ('job_intelligence_test').
    - Under NO circumstances can bulk collection mutate 'job_intelligence_dev'.
    - Database overrides and refresh confirmation variables are strictly forbidden/ignored for collection.

    Args:
        registry_file: Path to company registry JSON file.
        provider: Optional ATS platform filter ('GREENHOUSE', 'LEVER', 'ASHBY').
        companies: Optional list of company names or slugs to target.
        include_inactive: If True, includes inactive registry entries.
        dry_run: If True, executes plan-only dry run (Mode A).
        preview_live: If True, executes read-only live preview (Mode B).
        apply: If True, executes mutating collection and DB persistence (Mode C).
        target_database: Expected database name (required when apply is True).
        allow_database_override: Explicit database override (strictly forbidden for bulk collection).
        as_json: If True, outputs results in JSON format.
        output_file: Optional path to write output report.
        run_type: Cadence label for CollectionRun telemetry.
        http_client: Optional custom or mocked PoliteHttpClient for network requests.

    Returns:
        int: Process exit code (0 on success or empty, 1 on batch error, 2 on safety violation).
    """
    # 0. Validate mutually exclusive modes
    if apply and dry_run:
        err_msg = "ERROR: --dry-run and --apply are mutually exclusive."
        if as_json:
            print(json.dumps({"status": "FAILED", "error": err_msg}))
        else:
            print(err_msg, file=sys.stderr)
        return 2

    if apply and preview_live:
        err_msg = "ERROR: --preview-live and --apply are mutually exclusive."
        if as_json:
            print(json.dumps({"status": "FAILED", "error": err_msg}))
        else:
            print(err_msg, file=sys.stderr)
        return 2

    # 1. Load and filter company registry
    path = Path(registry_file) if registry_file else DEFAULT_REGISTRY_PATH
    try:
        registry = CompanyRegistry.from_file(path)
    except Exception as exc:
        err_msg = f"Failed to load company registry from '{path}': {exc}"
        if as_json:
            print(json.dumps({"status": "FAILED", "error": err_msg}))
        else:
            print(f"ERROR: {err_msg}", file=sys.stderr)
        return 1

    entries = registry.filter(
        provider=provider,
        companies=companies,
        active_only=not include_inactive,
    )

    if not entries:
        msg = "No matching company registry entries found for specified filters."
        if as_json:
            print(
                json.dumps(
                    {
                        "status": "EMPTY",
                        "message": msg,
                        "provider_filter": provider,
                        "company_filter": companies,
                    }
                )
            )
        else:
            print(msg)
        return 0

    # 2. MODE A: Plan-Only Dry Run (Default when neither --apply nor --preview-live is passed, or when --dry-run is passed)
    if not apply and not preview_live:
        preview_data = {
            "mode": "PLAN_ONLY_DRY_RUN",
            "companies_count": len(entries),
            "companies": [
                {
                    "name": e.name,
                    "slug": e.slug,
                    "ats_provider": e.ats_provider,
                    "board_identifier": e.board_identifier,
                    "career_url": e.career_url,
                    "is_active": e.is_active,
                }
                for e in entries
            ],
        }
        if as_json:
            report_str = json.dumps(preview_data, indent=2)
            print(report_str)
        else:
            print("=" * 80)
            print(f"PLAN-ONLY DRY RUN: {len(entries)} Companies Selected for Collection")
            print("=" * 80)
            print(f"{'Company':<20} | {'ATS Provider':<12} | {'Board Identifier':<20} | {'Active'}")
            print("-" * 80)
            for e in entries:
                print(
                    f"{e.name:<20} | {e.ats_provider:<12} | {e.board_identifier:<20} | {e.is_active}"
                )
            print("=" * 80)
            print(
                "  PLAN-ONLY DRY RUN COMPLETE: Zero network requests and zero database queries executed."
            )
            print(
                "  To preview live vacancy counts without database writes, pass '--preview-live'."
            )
            print(
                "  To execute live harvesting and persistence, pass '--apply --target-database <DB_NAME>'."
            )
            print("=" * 80)

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(preview_data, f, indent=2)

        return 0

    # 3. MODE B: Live Preview (Read-only public ATS HTTP queries, 0 DB writes)
    if preview_live and not apply:
        t0 = time.perf_counter()
        preview_results = await preview_live_endpoints(entries, http_client=http_client)
        elapsed = time.perf_counter() - t0

        total_discovered = sum(r["total_discovered"] for r in preview_results)
        succeeded_count = sum(1 for r in preview_results if r["status"] == "SUCCESS")
        empty_count = sum(1 for r in preview_results if r["status"] == "EMPTY")
        partial_count = sum(1 for r in preview_results if r["status"] == "PARTIAL_SUCCESS")
        failed_count = sum(1 for r in preview_results if r["status"] == "FAILED")

        live_preview_data = {
            "mode": "LIVE_PREVIEW",
            "status": "COMPLETED"
            if failed_count == 0 and partial_count == 0
            else ("PARTIAL_FAILURE" if failed_count > 0 else "PARTIAL_SUCCESS"),
            "timestamp": datetime.now(UTC).isoformat(),
            "duration_seconds": round(elapsed, 2),
            "companies_planned": len(entries),
            "companies_succeeded": succeeded_count,
            "companies_empty": empty_count,
            "companies_partial": partial_count,
            "companies_failed": failed_count,
            "total_discovered": total_discovered,
            "companies": preview_results,
        }

        if as_json:
            report_str = json.dumps(live_preview_data, indent=2)
            print(report_str)
        else:
            print("=" * 80)
            print(f"LIVE PREVIEW: {len(entries)} Companies Probed (Zero Database Writes)")
            print("=" * 80)
            print(
                f"{'Company':<20} | {'ATS':<10} | {'Status':<15} | {'Discovered':<10} | {'Details'}"
            )
            print("-" * 80)
            for r in preview_results:
                c_name = r["name"][:20]
                ats = r["ats_provider"][:10]
                st = r["status"][:15]
                disc = r["total_discovered"]
                det = (r["error_message"] or r["details"])[:30]
                print(f"{c_name:<20} | {ats:<10} | {st:<15} | {disc:<10} | {det}")
            print("-" * 80)
            print(
                f"Total Discovered: {total_discovered} jobs across {len(entries)} companies in {elapsed:.2f}s "
                f"({succeeded_count} active, {empty_count} empty, {failed_count} failed)"
            )
            print("=" * 80)
            print("  LIVE PREVIEW COMPLETE: Read-only public ATS requests completed successfully.")
            print("  Zero database connections, queries, or modifications were executed.")
            print(
                "  To execute live harvesting and persistence, pass '--apply --target-database <DB_NAME>'."
            )
            print("=" * 80)

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(live_preview_data, f, indent=2)

        return 1 if (failed_count > 0 or partial_count > 0) else 0

    # 4. MODE C: Apply (Mutating Live Collection with Strict Identity Checks & Company-Level Atomicity)
    if not target_database or not target_database.strip():
        err_msg = (
            "ERROR: --target-database is required when executing bulk collection with --apply. "
            "Specify the exact expected database name (e.g. --target-database job_intelligence_test)."
        )
        if as_json:
            print(json.dumps({"status": "FAILED", "error": err_msg}))
        else:
            print(err_msg, file=sys.stderr)
        return 2

    target_db_norm = target_database.strip().lower()

    # 4.1. UNCONDITIONAL PROHIBITION: Bulk collection CANNOT target job_intelligence_dev under any circumstances
    if target_db_norm == DEV_DATABASE_NAME:
        err_msg = (
            f"FATAL ERROR: Mutating bulk collection against development database '{DEV_DATABASE_NAME}' is strictly prohibited. "
            "The development database is reserved exclusively for single-company controlled Figma rule refresh operations. "
            f"Bulk collection is restricted to {sorted(ALLOWED_COLLECT_DATABASES)}."
        )
        if as_json:
            print(json.dumps({"status": "FAILED", "error": err_msg}))
        else:
            print(err_msg, file=sys.stderr)
        return 2

    # 4.2. Database override is forbidden for bulk collection
    if allow_database_override:
        err_msg = (
            "FATAL ERROR: Database override is not permitted for bulk collection. "
            f"Bulk collection cannot mutate '{DEV_DATABASE_NAME}' under any override or environment flag. "
            f"Mutating collection is strictly restricted to {sorted(ALLOWED_COLLECT_DATABASES)}."
        )
        if as_json:
            print(json.dumps({"status": "FAILED", "error": err_msg}))
        else:
            print(err_msg, file=sys.stderr)
        return 2

    # 4.3. Validate database against collection allowlist ('job_intelligence_test')
    if target_db_norm not in ALLOWED_COLLECT_DATABASES:
        err_msg = (
            f"FATAL ERROR: Mutating bulk collection is strictly restricted to allowlisted test database "
            f"{sorted(ALLOWED_COLLECT_DATABASES)}. Target database '{target_database}' is forbidden."
        )
        if as_json:
            print(json.dumps({"status": "FAILED", "error": err_msg}))
        else:
            print(err_msg, file=sys.stderr)
        return 2

    # 4.4. Verify connected database identity directly via SQL before processing any company
    async with session_scope() as check_session:
        current_db = (await check_session.execute(text("SELECT current_database()"))).scalar()
        if not current_db or current_db.strip().lower() != target_db_norm:
            err_msg = (
                f"FATAL ERROR: Target database mismatch. Connected to '{current_db}', "
                f"but --target-database specified '{target_database}'. Refusing mutating execution."
            )
            if as_json:
                print(json.dumps({"status": "FAILED", "error": err_msg}))
            else:
                print(err_msg, file=sys.stderr)
            return 2

        if current_db.strip().lower() not in ALLOWED_COLLECT_DATABASES:
            err_msg = (
                f"FATAL ERROR: Connected database '{current_db}' is not the authorized test database "
                f"{sorted(ALLOWED_COLLECT_DATABASES)}. Refusing mutating execution."
            )
            if as_json:
                print(json.dumps({"status": "FAILED", "error": err_msg}))
            else:
                print(err_msg, file=sys.stderr)
            return 2

    t0 = time.perf_counter()
    mapping_outcomes: list[MappingCollectionOutcome] = []

    # 4.5. Execute collection with Company-Level Atomicity:
    # Each company executes in its own isolated transaction session. If company A succeeds and
    # company B fails, company A's data remains committed to PostgreSQL.
    for entry in entries:
        t_entry_start = datetime.now(UTC)
        try:
            async with session_scope() as comp_session:
                # 1. Sync Company, Source, and CompanySource mapping
                mapping = await registry.sync_entry(comp_session, entry)

                # 2. Run collection for this company mapping
                runner = CollectionRunner.from_session(comp_session, http_client=http_client)
                outcome = await runner.run_mapping(mapping, run_type=run_type)

                # 3. Explicit commit for this company's transaction
                await comp_session.commit()
                mapping_outcomes.append(outcome)
        except Exception as exc:
            logger.error("Company execution failed for '%s': %s", entry.name, exc)
            mapping_outcomes.append(
                MappingCollectionOutcome(
                    company_source_id=uuid.uuid4(),
                    company_name=entry.name,
                    ats_identifier=entry.board_identifier,
                    status=STATUS_FAILED,
                    error_message=f"Company execution failed: {exc}",
                    started_at=t_entry_start,
                    finished_at=datetime.now(UTC),
                )
            )

    # 4.6. Post-commit automatic notification dispatch (if enabled)
    # Executed strictly AFTER all ingestion transactions have committed to PostgreSQL.
    # Failures during notification dispatch NEVER interrupt or invalidate committed collection data.
    try:
        from app.config.settings import get_settings
        from app.notifications.email_service import EmailNotificationService

        settings = get_settings()
        if settings.EMAIL_NOTIFICATIONS_ENABLED and settings.NOTIFICATION_RECIPIENT_EMAIL:
            logger.info("Triggering post-collection automatic notification dispatch...")
            async with session_scope() as notify_session:
                notify_service = EmailNotificationService(notify_session)
                dispatch_res = await notify_service.process_batch(
                    recipient_email=settings.NOTIFICATION_RECIPIENT_EMAIL,
                    dry_run=False,
                )
                logger.info(
                    "Post-collection notifications completed: %d sent, %d failed",
                    dispatch_res.total_sent,
                    dispatch_res.total_failed,
                )
    except Exception as notify_err:
        logger.warning(
            "Post-collection notification dispatch failed (collection data remains committed): %s",
            notify_err,
        )

    t1 = time.perf_counter()
    elapsed = t1 - t0

    total_selected = len(entries)
    total_processed = len(
        [o for o in mapping_outcomes if o.status not in (STATUS_SKIPPED_INACTIVE,)]
    )
    total_succeeded = len([o for o in mapping_outcomes if o.status == STATUS_SUCCESS])
    total_failed = len(
        [o for o in mapping_outcomes if o.status in (STATUS_FAILED, STATUS_UNSUPPORTED_SOURCE)]
    )
    total_skipped = len([o for o in mapping_outcomes if o.status == STATUS_SKIPPED_INACTIVE])

    runner_result = CollectionRunnerResult(
        run_type=run_type,
        total_mappings_selected=total_selected,
        total_mappings_processed=total_processed,
        total_mappings_succeeded=total_succeeded,
        total_mappings_failed=total_failed,
        total_mappings_skipped=total_skipped,
        records_discovered=sum(o.records_discovered for o in mapping_outcomes),
        records_ingested=sum(o.records_ingested for o in mapping_outcomes),
        records_failed=sum(o.records_failed for o in mapping_outcomes),
        duplicates_detected=sum(o.duplicates_detected for o in mapping_outcomes),
        http_2xx_count=sum(o.http_2xx_count for o in mapping_outcomes),
        http_4xx_count=sum(o.http_4xx_count for o in mapping_outcomes),
        http_5xx_count=sum(o.http_5xx_count for o in mapping_outcomes),
        mapping_outcomes=mapping_outcomes,
        collection_runs=[],
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )

    # 4.6. Format and output report
    if as_json:
        report_dict = format_bulk_json(runner_result, entries, elapsed)
        report_str = json.dumps(report_dict, indent=2)
        print(report_str)
    else:
        report_str = format_bulk_summary(runner_result, entries, elapsed)
        print(report_str)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            if as_json:
                json.dump(report_dict, f, indent=2)
            else:
                f.write(report_str)

    # 4.7. Return exit code: A batch cannot report full success when companies failed or were truncated
    has_failed = any(
        o.status in (STATUS_FAILED, STATUS_UNSUPPORTED_SOURCE) for o in mapping_outcomes
    )
    has_partial = any(o.status == STATUS_PARTIAL_SUCCESS for o in mapping_outcomes)
    return 1 if (has_failed or has_partial) else 0
