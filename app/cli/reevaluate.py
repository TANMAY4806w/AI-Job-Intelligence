"""CLI command execution handler for dry-run verification and eligibility re-evaluation."""

from __future__ import annotations

import json
import logging
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.reevaluation_service import (
    JobReevaluationService,
    ReevaluationReport,
)

logger = logging.getLogger("app.cli.reevaluate")


def format_table(headers: list[str], rows: list[list[str]], col_widths: list[int]) -> str:
    """Format tabular rows with consistent column widths."""
    line_parts = ["-" * w for w in col_widths]
    divider = "+-" + "-+-".join(line_parts) + "-+"

    hdr_parts = [f"{headers[i]:<{col_widths[i]}}" for i in range(len(headers))]
    hdr_line = "| " + " | ".join(hdr_parts) + " |"

    output_lines = [divider, hdr_line, divider]
    for row in rows:
        row_parts = [f"{str(row[i]):<{col_widths[i]}}" for i in range(len(row))]
        output_lines.append("| " + " | ".join(row_parts) + " |")
    output_lines.append(divider)
    return "\n".join(output_lines)


def print_human_report(report: ReevaluationReport) -> None:
    """Print structured, human-readable terminal report."""
    print("=" * 86)
    print("      AI-JOB-INTELLIGENCE: VERIFICATION & ELIGIBILITY RE-EVALUATION (DRY RUN)")
    print("=" * 86)
    print(f"Target Company:       {report.company_name}")
    print(f"Company ID:           {report.company_id}")
    print(f"Canonical Jobs:       {report.total_jobs}")
    print("Evaluation Mode:      DRY RUN (100% read-only, zero database mutations)")
    print(f"Reference Timestamp:  {report.reference_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 86)
    print()

    # 1. Eligibility Status Comparison
    print("1. ELIGIBILITY STATUS DISTRIBUTION")
    all_statuses = sorted(
        set(report.persisted_status_counts.keys()) | set(report.proposed_status_counts.keys())
    )
    status_rows = []
    for st in all_statuses:
        p_cnt = report.persisted_status_counts.get(st, 0)
        pr_cnt = report.proposed_status_counts.get(st, 0)
        diff = pr_cnt - p_cnt
        diff_str = f"{diff:+d}" if diff != 0 else "="
        status_rows.append([st, str(p_cnt), str(pr_cnt), diff_str])
    print(
        format_table(
            ["Status", "Persisted Count", "Proposed Count", "Net Delta"],
            status_rows,
            [20, 18, 18, 12],
        )
    )
    print()

    # 2. Decision Reason Comparison
    print("2. DECISION REASON CODES")
    all_reasons = sorted(
        set(report.persisted_reason_counts.keys()) | set(report.proposed_reason_counts.keys())
    )
    reason_rows = []
    for r in all_reasons:
        p_cnt = report.persisted_reason_counts.get(r, 0)
        pr_cnt = report.proposed_reason_counts.get(r, 0)
        diff = pr_cnt - p_cnt
        diff_str = f"{diff:+d}" if diff != 0 else "="
        reason_rows.append([r, str(p_cnt), str(pr_cnt), diff_str])
    print(
        format_table(
            ["Reason Code", "Persisted Count", "Proposed Count", "Net Delta"],
            reason_rows,
            [30, 16, 16, 10],
        )
    )
    print()

    # 3. Verification Dimension Pass Rates
    print("3. VERIFICATION DIMENSION PASS RATES")
    all_dims = [
        ("source_validity", "Dimension 1: Source Legitimacy"),
        ("jd_existence", "Dimension 2: Actual JD Existence"),
        ("application_url_validity", "Dimension 3: Application URL"),
        ("company_legitimacy", "Dimension 4: Company Legitimacy"),
        ("location_consistency", "Dimension 5: Location Consistency"),
        ("experience_consistency", "Dimension 6: Experience Consistency"),
        ("salary_statedness", "Dimension 7: Salary Statedness"),
        ("posting_date_authenticity", "Dimension 8: Posting Date Plausibility"),
    ]
    dim_rows = []
    for dim_key, dim_label in all_dims:
        p_cnt = report.persisted_dimension_counts.get(dim_key, 0)
        pr_cnt = report.proposed_dimension_counts.get(dim_key, 0)
        p_pct = f"{(p_cnt / report.total_jobs * 100):.1f}%" if report.total_jobs else "0.0%"
        pr_pct = f"{(pr_cnt / report.total_jobs * 100):.1f}%" if report.total_jobs else "0.0%"
        dim_rows.append(
            [
                dim_label,
                f"{p_cnt}/{report.total_jobs} ({p_pct})",
                f"{pr_cnt}/{report.total_jobs} ({pr_pct})",
            ]
        )
    print(
        format_table(
            ["Verification Dimension", "Persisted Pass Rate", "Proposed Pass Rate"],
            dim_rows,
            [40, 20, 20],
        )
    )
    print()

    # 4. Confidence Distribution
    print("4. VERIFICATION CONFIDENCE SCORE DISTRIBUTION")
    all_confs = sorted(
        set(report.persisted_confidence_counts.keys())
        | set(report.proposed_confidence_counts.keys())
    )
    conf_rows = []
    for conf in all_confs:
        p_cnt = report.persisted_confidence_counts.get(conf, 0)
        pr_cnt = report.proposed_confidence_counts.get(conf, 0)
        conf_rows.append([conf, str(p_cnt), str(pr_cnt)])
    conf_rows.append(
        [
            "Average Score",
            f"{report.average_persisted_confidence:.3f}",
            f"{report.average_proposed_confidence:.3f}",
        ]
    )
    print(
        format_table(
            ["Confidence Score", "Persisted Jobs", "Proposed Jobs"],
            conf_rows,
            [22, 18, 18],
        )
    )
    print()

    # 5. Missing Evidence vs Confirmed Failures
    print("5. EVIDENCE CLASSIFICATION: MISSING EVIDENCE VS CONFIRMED FAILURES")
    print("  [A] Missing Evidence (Omitted / Ambiguous / Unprobed Attributes):")
    for k, v in sorted(report.missing_evidence_counts.items()):
        print(f"      - {k:<34} : {v:>4} jobs")
    print()
    print("  [B] Confirmed Failures (Hard Policy Rejections & Technical Defects):")
    for k, v in sorted(report.confirmed_failure_counts.items()):
        print(f"      - {k:<34} : {v:>4} jobs")
    print()

    # 6. Changed Jobs Detail
    changed_jobs = [c for c in report.job_comparisons if c.status_changed]
    print(f"6. JOBS WITH STATUS CHANGES ({len(changed_jobs)} Total)")
    if not changed_jobs:
        print("  None. All jobs maintain identical operational status.")
    else:
        for c in changed_jobs:
            p_reason = c.persisted_rejection_reason or c.persisted_review_reason or "NONE"
            pr_reason = c.proposed_rejection_reason or c.proposed_review_reason or "NONE"
            print(f"  • Title:      {c.title}")
            print(f"    Location:   {c.location or 'N/A'}")
            print(f"    Persisted:  Status={c.persisted_status} (Reason={p_reason})")
            print(f"    Proposed:   Status={c.proposed_status} (Reason={pr_reason})")
            print(
                f"    Confidence: {c.persisted_verification_confidence} -> {c.proposed_verification_confidence}"
            )
            print()
    print("=" * 86)


async def execute_dry_run_reevaluation(
    session: AsyncSession,
    *,
    company_name: str | None = None,
    company_id: str | None = None,
    dry_run: bool = True,
    output_json: bool = False,
    output_file: str | None = None,
    limit: int | None = None,
) -> tuple[ReevaluationReport | None, int]:
    """Execute dry-run re-evaluation on stored canonical jobs."""
    if not dry_run:
        print(
            "FATAL ERROR: Mutating re-evaluation is disabled in V1. Only --dry-run is permitted.",
            file=sys.stderr,
        )
        return None, 2

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

    service = JobReevaluationService()
    try:
        report = await service.reevaluate_company(
            session=session,
            company_id_or_name=target_identifier,
            dry_run=True,
            limit=limit,
        )
    except ValueError as val_err:
        print(f"ERROR: {val_err}", file=sys.stderr)
        return None, 1

    report_dict = report.to_dict()

    if output_file:
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(report_dict, f, indent=2)
            logger.info("Saved re-evaluation report to %s", output_file)
        except OSError as exc:
            print(f"ERROR: Failed writing output file '{output_file}': {exc}", file=sys.stderr)
            return None, 1

    if output_json:
        print(json.dumps(report_dict, indent=2))
    else:
        print_human_report(report)
        if output_file:
            print(f"Detailed JSON report written to: {output_file}")

    return report, 0
