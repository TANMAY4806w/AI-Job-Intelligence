"""Main CLI entrypoint for AI-Job-Intelligence operator commands."""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError

from app.cli.greenhouse import (
    execute_greenhouse_collection,
    onboard_greenhouse_mapping,
)
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.source_repository import SourceRepository
from app.persistence.session import dispose_engine, session_scope

logger = logging.getLogger("app.cli")


def build_parser() -> argparse.ArgumentParser:
    """Build and configure the operator CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="AI-Job-Intelligence Operator CLI: Configure ATS platforms and trigger collections.",
    )
    subparsers = parser.add_subparsers(
        dest="subcommand",
        title="Subcommands",
        description="Supported operational workflows",
    )

    # =========================================================================
    # Greenhouse Subcommand
    # =========================================================================
    gh_parser = subparsers.add_parser(
        "greenhouse",
        help="Greenhouse ATS operations (onboarding and manual collection)",
        description="Manage Greenhouse job boards and execute manual harvests.",
    )
    gh_subparsers = gh_parser.add_subparsers(
        dest="action",
        title="Actions",
        description="Greenhouse operations",
    )

    # 1. greenhouse collect
    collect_parser = gh_subparsers.add_parser(
        "collect",
        help="Trigger manual collection for a Greenhouse company board",
        description="Harvest and ingest jobs from a public Greenhouse board token or mapping ID.",
    )
    collect_parser.add_argument(
        "-c",
        "--company-name",
        type=str,
        help="Hiring organization name (e.g. 'Stripe', 'Figma')",
    )
    collect_parser.add_argument(
        "-t",
        "--board-token",
        type=str,
        help="Public Greenhouse board token (e.g. 'stripe', 'figma')",
    )
    collect_parser.add_argument(
        "-m",
        "--mapping-id",
        type=str,
        help="Existing CompanySource UUID to collect directly",
    )
    collect_parser.add_argument(
        "-s",
        "--company-slug",
        type=str,
        default=None,
        help="Optional explicit normalized slug override for the company",
    )
    collect_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output summary in JSON format",
    )

    # 2. greenhouse onboard
    onboard_parser = gh_subparsers.add_parser(
        "onboard",
        help="Configure a Greenhouse company-source mapping without running collection",
        description="Idempotently create or verify Source, Company, and CompanySource records.",
    )
    onboard_parser.add_argument(
        "-c",
        "--company-name",
        type=str,
        required=True,
        help="Hiring organization name (e.g. 'Stripe')",
    )
    onboard_parser.add_argument(
        "-t",
        "--board-token",
        type=str,
        required=True,
        help="Public Greenhouse board token (e.g. 'stripe')",
    )
    onboard_parser.add_argument(
        "-s",
        "--company-slug",
        type=str,
        default=None,
        help="Optional explicit normalized slug override",
    )
    onboard_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output result in JSON format",
    )

    # 3. greenhouse list
    list_parser = gh_subparsers.add_parser(
        "list",
        help="List configured Greenhouse company board mappings",
        description="Display all registered Greenhouse company-source records.",
    )
    list_parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Include inactive mappings (defaults to active only)",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output mappings in JSON format",
    )

    return parser


async def _handle_greenhouse_collect(args: argparse.Namespace) -> int:
    """Handle 'greenhouse collect' command execution."""
    has_mapping_id = bool(args.mapping_id and args.mapping_id.strip())
    has_company_and_token = bool(
        args.company_name
        and args.company_name.strip()
        and args.board_token
        and args.board_token.strip()
    )

    if has_mapping_id and has_company_and_token:
        print(
            "ERROR: Provide either --mapping-id OR (--company-name and --board-token), not both.",
            file=sys.stderr,
        )
        return 2

    if not has_mapping_id and not has_company_and_token:
        print(
            "ERROR: Missing required arguments. Specify either --mapping-id OR both --company-name and --board-token.",
            file=sys.stderr,
        )
        return 2

    async with session_scope() as session:
        cs_repo = CompanySourceRepository(session)
        source_repo = SourceRepository(session)
        company_repo = CompanyRepository(session)

        if has_mapping_id:
            try:
                mapping_uuid = uuid.UUID(args.mapping_id.strip())
            except ValueError:
                print(
                    f"ERROR: Invalid mapping ID '{args.mapping_id}'. Must be a valid UUID.",
                    file=sys.stderr,
                )
                return 2

            company_source = await cs_repo.get_by_id(mapping_uuid)
            if company_source is None:
                print(
                    f"ERROR: CompanySource mapping '{mapping_uuid}' not found in database.",
                    file=sys.stderr,
                )
                return 1

            source = await source_repo.get_by_id(company_source.source_id)
            if source is None or source.source_name.upper() != "GREENHOUSE":
                print(
                    f"ERROR: Mapping '{mapping_uuid}' is not associated with GREENHOUSE platform.",
                    file=sys.stderr,
                )
                return 1

            company = await company_repo.get_by_id(company_source.company_id)
            company_name = company.name if company else None
        else:
            # Onboard or resolve mapping
            company, source, company_source, _ = await onboard_greenhouse_mapping(
                session,
                args.company_name,
                args.board_token,
                company_slug=args.company_slug,
            )
            company_name = company.name

        # Execute collection and commit transaction
        _, exit_code = await execute_greenhouse_collection(
            session,
            company_source,
            company_name=company_name,
            output_json=args.json,
        )
        return exit_code


async def _handle_greenhouse_onboard(args: argparse.Namespace) -> int:
    """Handle 'greenhouse onboard' command execution."""
    async with session_scope() as session:
        company, source, company_source, created = await onboard_greenhouse_mapping(
            session,
            args.company_name,
            args.board_token,
            company_slug=args.company_slug,
        )
        # Commit onboarding transaction
        await session.commit()

        if args.json:
            payload = {
                "company_id": str(company.id),
                "company_name": company.name,
                "company_slug": company.normalized_name,
                "source_id": str(source.id),
                "source_name": source.source_name,
                "company_source_id": str(company_source.id),
                "board_token": company_source.ats_identifier,
                "status": "CREATED" if created else "REUSED_EXISTING",
                "is_active": company_source.is_active,
            }
            print(json.dumps(payload, indent=2))
        else:
            status_label = "CREATED NEW" if created else "REUSED EXISTING"
            print("=" * 64)
            print("         GREENHOUSE ONBOARDING SUCCESSFUL")
            print("=" * 64)
            print(f"Company:             {company.name}")
            print(f"Slug:                {company.normalized_name}")
            print(f"Company ID:          {company.id}")
            print(f"Platform:            {source.source_name}")
            print(f"Board Token:         {company_source.ats_identifier}")
            print(f"Mapping ID:          {company_source.id}")
            print(f"Mapping Status:      {status_label}")
            print(f"Active:              {company_source.is_active}")
            print("=" * 64)

        return 0


async def _handle_greenhouse_list(args: argparse.Namespace) -> int:
    """Handle 'greenhouse list' command execution."""
    async with session_scope() as session:
        source_repo = SourceRepository(session)
        cs_repo = CompanySourceRepository(session)
        company_repo = CompanyRepository(session)

        source = await source_repo.get_by_name("GREENHOUSE")
        if source is None:
            if args.json:
                print("[]")
            else:
                print("No Greenhouse source registered in database.")
            return 0

        mappings = await cs_repo.list_by_source_id(
            source.id,
            active_only=not args.all,
        )

        rows = []
        for cs in mappings:
            company = await company_repo.get_by_id(cs.company_id)
            comp_name = company.name if company else "Unknown"
            last_collected = (
                cs.last_collected_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                if cs.last_collected_at
                else "Never"
            )
            rows.append(
                {
                    "mapping_id": str(cs.id),
                    "company_name": comp_name,
                    "board_token": cs.ats_identifier or "",
                    "is_active": cs.is_active,
                    "last_collected": last_collected,
                }
            )

        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            if not rows:
                print("No configured Greenhouse mappings found.")
                return 0

            print("=" * 90)
            print("                   CONFIGURED GREENHOUSE COMPANY MAPPINGS")
            print("=" * 90)
            print(
                f"{'Company':<25} {'Board Token':<20} {'Active':<8} {'Last Collected':<22} {'Mapping ID'}"
            )
            print("-" * 90)
            for r in rows:
                active_str = "Yes" if r["is_active"] else "No"
                print(
                    f"{r['company_name'][:24]:<25} {r['board_token'][:19]:<20} {active_str:<8} {r['last_collected']:<22} {r['mapping_id']}"
                )
            print("=" * 90)

        return 0


async def async_main(args: argparse.Namespace) -> int:
    """Dispatch CLI arguments asynchronously to appropriate handler."""
    try:
        if args.subcommand == "greenhouse":
            if args.action == "collect":
                return await _handle_greenhouse_collect(args)
            elif args.action == "onboard":
                return await _handle_greenhouse_onboard(args)
            elif args.action == "list":
                return await _handle_greenhouse_list(args)
            else:
                print(
                    "ERROR: Please specify a greenhouse action: collect, onboard, or list.",
                    file=sys.stderr,
                )
                return 2
        else:
            print(
                "ERROR: Unrecognized subcommand. Use --help to view available commands.",
                file=sys.stderr,
            )
            return 2
    except ValueError as val_err:
        print(f"ERROR: {val_err}", file=sys.stderr)
        return 1
    except SQLAlchemyError as db_err:
        logger.error("Database operational error during CLI execution: %s", db_err)
        print(
            "FATAL ERROR: A database error occurred during operation. Details logged.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        logger.exception("Fatal operational exception during CLI execution: %s", exc)
        print(f"FATAL ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    """Synchronous entrypoint for command-line invocation."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)

    if not args.subcommand:
        parser.print_help()
        return 0

    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())
