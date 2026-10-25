"""Unit tests for the operator CLI module.

Validates:
1. CLI argument validation and help flags.
2. Company name normalization and slug generation.
3. Onboarding logic with new vs. existing entities.
4. Token validation, conflict detection, and anti-overwrite guarantees.
5. Summary and JSON formatting across all collection outcome statuses.
6. Process exit code contract (0 on success/empty/partial, 1 on failure, 2 on arg errors).
7. Non-leakage of database passwords or sensitive environment variables.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.cli.greenhouse import (
    execute_greenhouse_collection,
    format_collection_json,
    format_collection_summary,
    get_or_create_company,
    get_or_create_greenhouse_source,
    normalize_company_name,
    onboard_greenhouse_mapping,
)
from app.cli.main import main
from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
)
from app.orchestration.collection_runner import (
    STATUS_SKIPPED_INACTIVE,
    STATUS_UNSUPPORTED_SOURCE,
    MappingCollectionOutcome,
)
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.source_repository import SourceRepository


# =============================================================================
# 1. Argument Parsing & Help Tests
# =============================================================================
class TestCLIArgumentValidation:
    """Validate CLI argument parsing, flags, and error exit codes."""

    def test_root_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify root --help prints usage and exits cleanly with 0."""
        code = main(["--help"])
        assert code == 0
        captured = capsys.readouterr()
        assert "AI-Job-Intelligence Operator CLI" in captured.out
        assert "greenhouse" in captured.out

    def test_no_arguments_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify executing without arguments prints help and returns 0."""
        code = main([])
        assert code == 0
        captured = capsys.readouterr()
        assert "usage:" in captured.out

    def test_greenhouse_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify 'greenhouse --help' shows subcommands."""
        code = main(["greenhouse", "--help"])
        assert code == 0
        captured = capsys.readouterr()
        assert "collect" in captured.out
        assert "onboard" in captured.out
        assert "list" in captured.out

    def test_greenhouse_collect_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify 'greenhouse collect --help' shows all options."""
        code = main(["greenhouse", "collect", "--help"])
        assert code == 0
        captured = capsys.readouterr()
        assert "--company-name" in captured.out
        assert "--board-token" in captured.out
        assert "--mapping-id" in captured.out
        assert "--json" in captured.out

    def test_collect_missing_arguments(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify 'greenhouse collect' without arguments returns exit code 2."""
        code = main(["greenhouse", "collect"])
        assert code == 2
        captured = capsys.readouterr()
        assert "ERROR: Missing required arguments" in captured.err

    def test_collect_conflicting_arguments(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify specifying both --mapping-id and company/token returns exit code 2."""
        code = main(
            [
                "greenhouse",
                "collect",
                "--mapping-id",
                str(uuid.uuid4()),
                "--company-name",
                "Acme",
                "--board-token",
                "acme",
            ]
        )
        assert code == 2
        captured = capsys.readouterr()
        assert "ERROR: Provide either --mapping-id OR" in captured.err

    def test_collect_invalid_uuid(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify non-UUID string in --mapping-id returns exit code 2."""
        code = main(["greenhouse", "collect", "--mapping-id", "not-a-valid-uuid"])
        assert code == 2
        captured = capsys.readouterr()
        assert "Must be a valid UUID" in captured.err

    def test_onboard_missing_required_args(self) -> None:
        """Verify 'greenhouse onboard' requires both company-name and board-token."""
        code = main(["greenhouse", "onboard", "--company-name", "Acme"])
        assert code == 2


# =============================================================================
# 2. Company Name Normalization Tests
# =============================================================================
class TestNormalizeCompanyName:
    """Validate company name normalization and slug generation."""

    def test_clean_simple_name(self) -> None:
        assert normalize_company_name("Stripe") == "stripe"
        assert normalize_company_name("Figma") == "figma"

    def test_strips_legal_suffixes(self) -> None:
        assert normalize_company_name("Stripe, Inc.") == "stripe"
        assert normalize_company_name("Acme Corporation") == "acme"
        assert normalize_company_name("Acme Corp.") == "acme"
        assert normalize_company_name("Beta LLC") == "beta"
        assert normalize_company_name("Gamma Ltd") == "gamma"
        assert normalize_company_name("Delta Limited") == "delta"
        assert normalize_company_name("Epsilon Pvt Ltd") == "epsilon"
        assert normalize_company_name("Zeta Private Limited") == "zeta"
        assert normalize_company_name("Eta Pvt. Ltd.") == "eta"
        assert normalize_company_name("Theta Company") == "theta"
        assert normalize_company_name("Iota Co.") == "iota"

    def test_converts_spaces_and_symbols_to_hyphens(self) -> None:
        assert normalize_company_name("AI Job Intelligence") == "ai-job-intelligence"
        assert normalize_company_name("Razorpay & Partners") == "razorpay-partners"

    def test_collapses_consecutive_hyphens(self) -> None:
        assert normalize_company_name("Foo - - Bar") == "foo-bar"

    def test_invalid_names_raise_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            normalize_company_name("")
        with pytest.raises(ValueError, match="blank or whitespace-only"):
            normalize_company_name("   ")
        with pytest.raises(ValueError, match="does not contain any valid alphanumeric"):
            normalize_company_name("---!@#$---")


# =============================================================================
# 3. Source and Company Resolution Unit Tests
# =============================================================================
class TestEntityResolutionUnit:
    """Validate idempotent source and company resolution using mocked repositories."""

    @pytest.mark.asyncio
    async def test_get_or_create_greenhouse_source_creates_when_absent(self) -> None:
        mock_repo = MagicMock(spec=SourceRepository)
        mock_repo.get_by_name = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(side_effect=lambda s: s)

        source, created = await get_or_create_greenhouse_source(mock_repo)
        assert created is True
        assert source.source_name == "GREENHOUSE"
        assert source.source_type == "ATS_API"
        mock_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_or_create_greenhouse_source_reuses_when_present(self) -> None:
        existing = Source(
            id=uuid.uuid4(),
            source_name="GREENHOUSE",
            source_type="ATS_API",
        )
        mock_repo = MagicMock(spec=SourceRepository)
        mock_repo.get_by_name = AsyncMock(return_value=existing)
        mock_repo.create = AsyncMock()

        source, created = await get_or_create_greenhouse_source(mock_repo)
        assert created is False
        assert source.id == existing.id
        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_or_create_company_creates_when_absent(self) -> None:
        mock_repo = MagicMock(spec=CompanyRepository)
        mock_repo.get_by_normalized_name = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(side_effect=lambda c: c)

        company, created = await get_or_create_company(mock_repo, "Acme Corp")
        assert created is True
        assert company.name == "Acme Corp"
        assert company.normalized_name == "acme"
        mock_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_or_create_company_reuses_existing(self) -> None:
        existing = Company(
            id=uuid.uuid4(),
            name="Acme Inc",
            normalized_name="acme",
        )
        mock_repo = MagicMock(spec=CompanyRepository)
        mock_repo.get_by_normalized_name = AsyncMock(return_value=existing)
        mock_repo.create = AsyncMock()

        # Same company name modulo punctuation/casing reuses cleanly
        company, created = await get_or_create_company(mock_repo, "Acme, Inc.")
        assert created is False
        assert company.id == existing.id
        mock_repo.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_or_create_company_detects_legal_suffix_collision_and_raises(self) -> None:
        """Verify distinct companies differing only by legal suffix (e.g. Acme Inc vs Acme LLC) raise ambiguous identity conflict."""
        existing = Company(
            id=uuid.uuid4(),
            name="Acme Inc",
            normalized_name="acme",
        )
        mock_repo = MagicMock(spec=CompanyRepository)
        mock_repo.get_by_normalized_name = AsyncMock(return_value=existing)

        # "Acme LLC" produces slug "acme", which collides with "Acme Inc"
        with pytest.raises(ValueError, match="Ambiguous company identity"):
            await get_or_create_company(mock_repo, "Acme LLC")

        # "Acme Corp" also produces slug "acme" and must also raise
        with pytest.raises(ValueError, match="Ambiguous company identity"):
            await get_or_create_company(mock_repo, "Acme Corp")

    @pytest.mark.asyncio
    async def test_get_or_create_company_with_explicit_slug_resolves_collision(self) -> None:
        """Verify operator can resolve ambiguous identity by providing explicit --company-slug."""
        existing = Company(
            id=uuid.uuid4(),
            name="Acme Inc",
            normalized_name="acme",
        )
        mock_repo = MagicMock(spec=CompanyRepository)
        # "acme-llc" does not exist yet
        mock_repo.get_by_normalized_name = AsyncMock(
            side_effect=lambda slug: existing if slug == "acme" else None
        )
        mock_repo.create = AsyncMock(side_effect=lambda c: c)

        company, created = await get_or_create_company(
            mock_repo, "Acme LLC", company_slug="acme-llc"
        )
        assert created is True
        assert company.name == "Acme LLC"
        assert company.normalized_name == "acme-llc"
        mock_repo.create.assert_awaited_once()


# =============================================================================
# 4. Onboarding Logic and Invariant Checks
# =============================================================================
class TestOnboardGreenhouseMappingUnit:
    """Validate Greenhouse onboarding logic, token uniqueness, and overwrite prevention."""

    @pytest.mark.asyncio
    async def test_onboard_new_mapping_succeeds(self) -> None:
        mock_session = AsyncMock()
        company_id = uuid.uuid4()
        source_id = uuid.uuid4()

        company = Company(id=company_id, name="Acme", normalized_name="acme")
        source = Source(id=source_id, source_name="GREENHOUSE", source_type="ATS_API")

        with (
            patch("app.cli.greenhouse.SourceRepository") as mock_s_cls,
            patch("app.cli.greenhouse.CompanyRepository") as mock_c_cls,
            patch("app.cli.greenhouse.CompanySourceRepository") as mock_cs_cls,
        ):
            s_repo = mock_s_cls.return_value
            c_repo = mock_c_cls.return_value
            cs_repo = mock_cs_cls.return_value

            s_repo.get_by_name = AsyncMock(return_value=source)
            c_repo.get_by_normalized_name = AsyncMock(return_value=company)
            cs_repo.get_by_ats_identifier = AsyncMock(return_value=None)
            cs_repo.list_by_company_id = AsyncMock(return_value=[])
            cs_repo.create = AsyncMock(side_effect=lambda cs: cs)

            comp, src, cs, created = await onboard_greenhouse_mapping(
                mock_session, "Acme", "acme-board"
            )

            assert created is True
            assert cs.company_id == company_id
            assert cs.source_id == source_id
            assert cs.ats_identifier == "acme-board"
            cs_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_onboard_idempotent_reuse_exact_mapping(self) -> None:
        """Verify calling onboard with identical company and token reuses existing mapping."""
        mock_session = AsyncMock()
        company_id = uuid.uuid4()
        source_id = uuid.uuid4()

        company = Company(id=company_id, name="Acme", normalized_name="acme")
        source = Source(id=source_id, source_name="GREENHOUSE", source_type="ATS_API")
        existing_cs = CompanySource(
            id=uuid.uuid4(),
            company_id=company_id,
            source_id=source_id,
            ats_identifier="acme-board",
            discovery_method="MANUAL_OPERATOR",
        )

        with (
            patch("app.cli.greenhouse.SourceRepository") as mock_s_cls,
            patch("app.cli.greenhouse.CompanyRepository") as mock_c_cls,
            patch("app.cli.greenhouse.CompanySourceRepository") as mock_cs_cls,
        ):
            s_repo = mock_s_cls.return_value
            c_repo = mock_c_cls.return_value
            cs_repo = mock_cs_cls.return_value

            s_repo.get_by_name = AsyncMock(return_value=source)
            c_repo.get_by_normalized_name = AsyncMock(return_value=company)
            cs_repo.get_by_ats_identifier = AsyncMock(return_value=existing_cs)

            comp, src, cs, created = await onboard_greenhouse_mapping(
                mock_session, "Acme", "acme-board"
            )

            assert created is False
            assert cs.id == existing_cs.id
            cs_repo.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_onboard_rejects_token_mapped_to_different_company(self) -> None:
        """Verify token already belonging to another company raises ValueError."""
        mock_session = AsyncMock()
        company_id_1 = uuid.uuid4()
        company_id_2 = uuid.uuid4()
        source_id = uuid.uuid4()

        company_1 = Company(id=company_id_1, name="Acme", normalized_name="acme")
        source = Source(id=source_id, source_name="GREENHOUSE", source_type="ATS_API")
        existing_cs_other_company = CompanySource(
            id=uuid.uuid4(),
            company_id=company_id_2,  # Different company!
            source_id=source_id,
            ats_identifier="shared-token",
            discovery_method="MANUAL_OPERATOR",
        )

        with (
            patch("app.cli.greenhouse.SourceRepository") as mock_s_cls,
            patch("app.cli.greenhouse.CompanyRepository") as mock_c_cls,
            patch("app.cli.greenhouse.CompanySourceRepository") as mock_cs_cls,
        ):
            s_repo = mock_s_cls.return_value
            c_repo = mock_c_cls.return_value
            cs_repo = mock_cs_cls.return_value

            s_repo.get_by_name = AsyncMock(return_value=source)
            c_repo.get_by_normalized_name = AsyncMock(return_value=company_1)
            cs_repo.get_by_ats_identifier = AsyncMock(return_value=existing_cs_other_company)

            with pytest.raises(ValueError, match="already mapped to a different company"):
                await onboard_greenhouse_mapping(mock_session, "Acme", "shared-token")

    @pytest.mark.asyncio
    async def test_onboard_prohibits_silent_token_overwrite(self) -> None:
        """Verify attempting to map a new token to a company that already has a token raises ValueError."""
        mock_session = AsyncMock()
        company_id = uuid.uuid4()
        source_id = uuid.uuid4()

        company = Company(id=company_id, name="Acme", normalized_name="acme")
        source = Source(id=source_id, source_name="GREENHOUSE", source_type="ATS_API")
        existing_mapping = CompanySource(
            id=uuid.uuid4(),
            company_id=company_id,
            source_id=source_id,
            ats_identifier="acme-original",
            discovery_method="MANUAL_OPERATOR",
        )

        with (
            patch("app.cli.greenhouse.SourceRepository") as mock_s_cls,
            patch("app.cli.greenhouse.CompanyRepository") as mock_c_cls,
            patch("app.cli.greenhouse.CompanySourceRepository") as mock_cs_cls,
        ):
            s_repo = mock_s_cls.return_value
            c_repo = mock_c_cls.return_value
            cs_repo = mock_cs_cls.return_value

            s_repo.get_by_name = AsyncMock(return_value=source)
            c_repo.get_by_normalized_name = AsyncMock(return_value=company)
            cs_repo.get_by_ats_identifier = AsyncMock(return_value=None)
            cs_repo.list_by_company_id = AsyncMock(return_value=[existing_mapping])

            with pytest.raises(
                ValueError, match="Silently overwriting with 'acme-new' is prohibited"
            ):
                await onboard_greenhouse_mapping(mock_session, "Acme", "acme-new")


# =============================================================================
# 5. Summary Formatting and Output Tests
# =============================================================================
class TestSummaryFormatting:
    """Validate summary text and JSON output formats."""

    @pytest.fixture
    def sample_outcome(self) -> MappingCollectionOutcome:
        return MappingCollectionOutcome(
            company_source_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            company_name="Acme Corp",
            ats_identifier="acme",
            status=STATUS_SUCCESS,
            collection_run_id=uuid.uuid4(),
            records_discovered=15,
            records_ingested=12,
            records_failed=0,
            duplicates_detected=3,
            http_2xx_count=16,
            http_4xx_count=0,
            http_5xx_count=0,
            started_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 3, 1, 10, 0, 5, tzinfo=UTC),
        )

    def test_summary_text_contains_all_metrics(
        self, sample_outcome: MappingCollectionOutcome
    ) -> None:
        text = format_collection_summary(sample_outcome)
        assert "Acme Corp" in text
        assert "GREENHOUSE" in text
        assert "acme" in text
        assert "SUCCESS" in text
        assert "Records Discovered:  15" in text
        assert "Records Ingested:    12" in text
        assert "Duplicates Detected: 3" in text
        assert "Records Failed:      0" in text
        assert "HTTP 2xx (Success):  16" in text
        assert "Duration:            5.00s" in text

    def test_summary_text_shows_diagnostic_error_when_failed(self) -> None:
        failed_outcome = MappingCollectionOutcome(
            company_source_id=uuid.uuid4(),
            company_name="Beta Inc",
            ats_identifier="beta",
            status=STATUS_FAILED,
            collection_run_id=uuid.uuid4(),
            error_message="HTTP 404: Greenhouse board not found",
            started_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 3, 1, 10, 0, 1, tzinfo=UTC),
        )
        text = format_collection_summary(failed_outcome)
        assert "Beta Inc" in text
        assert "FAILED" in text
        assert "Diagnostic Error:    HTTP 404: Greenhouse board not found" in text

    def test_json_summary_valid_structure(self, sample_outcome: MappingCollectionOutcome) -> None:
        json_str = format_collection_json(sample_outcome)
        data = json.loads(json_str)
        assert data["company_name"] == "Acme Corp"
        assert data["platform"] == "GREENHOUSE"
        assert data["board_token"] == "acme"
        assert data["status"] == "SUCCESS"
        assert data["records_discovered"] == 15
        assert data["records_ingested"] == 12
        assert data["duplicates_detected"] == 3
        assert data["records_failed"] == 0
        assert data["duration_seconds"] == 5.0


# =============================================================================
# 6. Collection Execution & Exit Code Contract Tests
# =============================================================================
class TestCollectionExecutionAndExitCodes:
    """Validate execute_greenhouse_collection across all status codes and transactional rollback."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "expected_code"),
        [
            (STATUS_SUCCESS, 0),
            (STATUS_EMPTY, 0),
            (STATUS_PARTIAL_SUCCESS, 0),
            (STATUS_FAILED, 1),
            (STATUS_UNSUPPORTED_SOURCE, 1),
            (STATUS_SKIPPED_INACTIVE, 1),
        ],
    )
    async def test_exit_codes_per_status(
        self, status: str, expected_code: int, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_session = AsyncMock()
        company_source = CompanySource(
            id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            ats_identifier="test-token",
            discovery_method="MANUAL_OPERATOR",
        )

        outcome = MappingCollectionOutcome(
            company_source_id=company_source.id,
            status=status,
            records_discovered=5 if status != STATUS_EMPTY else 0,
            records_ingested=5 if status == STATUS_SUCCESS else 0,
            collection_run_id=uuid.uuid4(),
        )

        with patch("app.cli.greenhouse.CollectionRunner") as mock_runner_cls:
            runner_instance = mock_runner_cls.from_session.return_value
            runner_instance.run_mapping = AsyncMock(return_value=outcome)

            ret_outcome, code = await execute_greenhouse_collection(
                mock_session, company_source, company_name="Test Co"
            )

            assert code == expected_code
            assert ret_outcome.status == status
            mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_database_error_rolls_back_and_raises(self) -> None:
        """Verify that an unexpected database flush/commit failure rolls back session."""
        mock_session = AsyncMock()
        mock_session.commit.side_effect = SQLAlchemyError("Connection reset by peer")
        company_source = CompanySource(
            id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            ats_identifier="test-token",
            discovery_method="MANUAL_OPERATOR",
        )

        outcome = MappingCollectionOutcome(
            company_source_id=company_source.id,
            status=STATUS_SUCCESS,
        )

        with patch("app.cli.greenhouse.CollectionRunner") as mock_runner_cls:
            runner_instance = mock_runner_cls.from_session.return_value
            runner_instance.run_mapping = AsyncMock(return_value=outcome)

            with pytest.raises(SQLAlchemyError):
                await execute_greenhouse_collection(
                    mock_session, company_source, company_name="Test Co"
                )

            mock_session.rollback.assert_awaited_once()


# =============================================================================
# 7. Credential & Secret Protection Tests
# =============================================================================
class TestSecretProtection:
    """Verify that credentials and environment secrets are never printed in outputs."""

    def test_summary_and_json_never_contain_connection_strings(self) -> None:
        outcome = MappingCollectionOutcome(
            company_source_id=uuid.uuid4(),
            company_name="Secret Corp",
            ats_identifier="secret-board",
            status=STATUS_SUCCESS,
            collection_run_id=uuid.uuid4(),
        )

        text = format_collection_summary(outcome)
        json_output = format_collection_json(outcome)

        for out in (text, json_output):
            assert "postgresql://" not in out
            assert "password" not in out.lower()
            assert "postgres" not in out.lower()
            assert "secret" in out  # Only company/board name contains 'secret'
