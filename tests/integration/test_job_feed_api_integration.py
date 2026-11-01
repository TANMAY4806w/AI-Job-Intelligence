"""Live PostgreSQL integration tests for Job Feed, Review Queue, and Web Dashboard APIs.

Boundary: End-to-end integration tests using PostgreSQL test database (job_intelligence_test).
Validates:
1. Real database querying, pagination, text search, sorting, and faceted filtering.
2. Eager loading and zero N+1 database queries.
3. Review triage persistence without modifying deterministic eligibility rules.
4. Dismissal lifecycle and feed exclusion.
5. HTML template rendering with real PostgreSQL records.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.main import app
from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource, Source
from app.persistence.session import get_sessionmaker

pytestmark = pytest.mark.integration


async def _seed_test_jobs() -> dict[str, uuid.UUID]:
    """Seed canonical companies, sources, jobs, and provenance references into job_intelligence_test."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # 1. Sources
        src_gh = Source(
            source_name="greenhouse",
            source_type="ATS",
            base_url="https://boards-api.greenhouse.io",
            is_active=True,
        )
        src_lever = Source(
            source_name="lever",
            source_type="ATS",
            base_url="https://api.lever.co",
            is_active=True,
        )
        session.add_all([src_gh, src_lever])
        await session.flush()

        # 2. Companies
        comp_stripe = Company(
            name="Stripe",
            normalized_name="stripe",
            website="https://stripe.com",
            industry="Fintech",
            sector="Technology",
            is_verified=True,
            confidence=Decimal("1.00"),
        )
        comp_datadog = Company(
            name="Datadog",
            normalized_name="datadog",
            website="https://datadoghq.com",
            industry="Cloud Monitoring",
            sector="Technology",
            is_verified=True,
            confidence=Decimal("1.00"),
        )
        session.add_all([comp_stripe, comp_datadog])
        await session.flush()

        # 3. CompanySources
        cs_stripe = CompanySource(
            company_id=comp_stripe.id,
            source_id=src_gh.id,
            ats_identifier="stripe",
            career_url="https://stripe.com/jobs",
            discovery_method="REGISTRY",
            is_active=True,
        )
        cs_datadog = CompanySource(
            company_id=comp_datadog.id,
            source_id=src_lever.id,
            ats_identifier="datadog",
            career_url="https://datadoghq.com/careers",
            discovery_method="REGISTRY",
            is_active=True,
        )
        session.add_all([cs_stripe, cs_datadog])
        await session.flush()

        # 4. RawJobs
        raw_1 = RawJob(
            company_source_id=cs_stripe.id,
            source_job_id="stripe-gh-1",
            source_url="https://boards.greenhouse.io/stripe/jobs/1",
            raw_body=b'{"id": "stripe-gh-1"}',
            raw_payload={"id": "stripe-gh-1"},
            content_hash="hash1",
        )
        raw_2 = RawJob(
            company_source_id=cs_stripe.id,
            source_job_id="stripe-gh-2",
            source_url="https://boards.greenhouse.io/stripe/jobs/2",
            raw_body=b'{"id": "stripe-gh-2"}',
            raw_payload={"id": "stripe-gh-2"},
            content_hash="hash2",
        )
        raw_3 = RawJob(
            company_source_id=cs_datadog.id,
            source_job_id="dd-lever-3",
            source_url="https://jobs.lever.co/datadog/3",
            raw_body=b'{"id": "dd-lever-3"}',
            raw_payload={"id": "dd-lever-3"},
            content_hash="hash3",
        )
        raw_4 = RawJob(
            company_source_id=cs_datadog.id,
            source_job_id="dd-lever-4",
            source_url="https://jobs.lever.co/datadog/4",
            raw_body=b'{"id": "dd-lever-4"}',
            raw_payload={"id": "dd-lever-4"},
            content_hash="hash4",
        )
        raw_5 = RawJob(
            company_source_id=cs_stripe.id,
            source_job_id="stripe-gh-5",
            source_url="https://boards.greenhouse.io/stripe/jobs/5",
            raw_body=b'{"id": "stripe-gh-5"}',
            raw_payload={"id": "stripe-gh-5"},
            content_hash="hash5",
        )
        session.add_all([raw_1, raw_2, raw_3, raw_4, raw_5])
        await session.flush()

        # 5. Canonical Jobs
        job_1 = Job(
            company_id=comp_stripe.id,
            company_name="Stripe",
            title="Staff Infrastructure Engineer",
            normalized_title="staff infrastructure engineer",
            location="Remote, US",
            normalized_location="remote",
            employment_type="FULL_TIME",
            remote_type="REMOTE",
            description="Build globally distributed financial ledger infrastructure.",
            responsibilities=["Scale core databases", "Maintain 99.999% uptime"],
            requirements=["8+ years systems engineering", "Distributed consensus expertise"],
            skills=["Python", "Go", "PostgreSQL"],
            experience_raw="8+ years",
            experience_min_years=Decimal("8.0"),
            experience_max_years=Decimal("14.0"),
            salary_min=Decimal("210000.00"),
            salary_max=Decimal("260000.00"),
            salary_currency="USD",
            salary_period="YEAR",
            posted_at=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
            collected_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            application_url="https://stripe.com/jobs/apply/1",
            application_status="NOT_APPLIED",
            role_priority="P1",
            relevance_status="TARGET",
            eligibility_status="ELIGIBLE",
            source_verified=True,
            jd_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=True,
            verification_confidence=Decimal("0.98"),
        )

        job_2 = Job(
            company_id=comp_stripe.id,
            company_name="Stripe",
            title="Senior Full Stack Engineer",
            normalized_title="senior full stack engineer",
            location="San Francisco, CA",
            normalized_location="san francisco",
            employment_type="FULL_TIME",
            remote_type="HYBRID",
            description="Design developer dashboards and API billing views.",
            responsibilities=["Develop web applications", "Collaborate with design"],
            requirements=["Experience ambiguous", "React and Node"],
            skills=["TypeScript", "React"],
            experience_raw="Multiple years",
            salary_min=Decimal("170000.00"),
            salary_max=Decimal("210000.00"),
            salary_currency="USD",
            salary_period="YEAR",
            posted_at=datetime(2026, 2, 28, 9, 0, tzinfo=UTC),
            collected_at=datetime(2026, 2, 28, 12, 0, tzinfo=UTC),
            application_url="https://stripe.com/jobs/apply/2",
            application_status="NOT_APPLIED",
            role_priority="P2",
            relevance_status="TARGET",
            eligibility_status="REVIEW",
            review_reason="EXPERIENCE_AMBIGUOUS",
            source_verified=True,
            jd_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=False,
            salary_verified=True,
            verification_confidence=Decimal("0.85"),
        )

        job_3 = Job(
            company_id=comp_datadog.id,
            company_name="Datadog",
            title="Senior Machine Learning Platform Engineer",
            normalized_title="senior machine learning platform engineer",
            location="New York, NY",
            normalized_location="new york",
            employment_type="FULL_TIME",
            remote_type="ONSITE",
            description="Build scalable GPU inference pipelines and streaming telemetry.",
            responsibilities=["Deploy model services", "Optimize latency"],
            requirements=["5+ years ML infrastructure", "Kubernetes"],
            skills=["Python", "C++", "PyTorch"],
            experience_raw="5+ years",
            experience_min_years=Decimal("5.0"),
            salary_min=Decimal("190000.00"),
            salary_max=Decimal("240000.00"),
            salary_currency="USD",
            salary_period="YEAR",
            posted_at=datetime(2026, 3, 2, 11, 0, tzinfo=UTC),
            collected_at=datetime(2026, 3, 2, 13, 0, tzinfo=UTC),
            application_url="https://datadoghq.com/careers/apply/3",
            application_status="NOT_APPLIED",
            role_priority="P1",
            relevance_status="TARGET",
            eligibility_status="ELIGIBLE",
            source_verified=True,
            jd_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=True,
            verification_confidence=Decimal("0.96"),
        )

        job_4 = Job(
            company_id=comp_datadog.id,
            company_name="Datadog",
            title="Sales Development Representative",
            normalized_title="sales development representative",
            location="Boston, MA",
            normalized_location="boston",
            employment_type="FULL_TIME",
            remote_type="ONSITE",
            description="Engage prospective enterprise software buyers.",
            posted_at=datetime(2026, 2, 20, 8, 0, tzinfo=UTC),
            collected_at=datetime(2026, 2, 20, 10, 0, tzinfo=UTC),
            application_url="https://datadoghq.com/careers/apply/4",
            application_status="NOT_APPLIED",
            role_priority="P3",
            relevance_status="EXCLUDED",
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            source_verified=True,
            jd_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=False,
            verification_confidence=Decimal("0.70"),
        )

        job_5 = Job(
            company_id=comp_stripe.id,
            company_name="Stripe",
            title="Archived Ops Specialist",
            normalized_title="archived ops specialist",
            location="Remote, US",
            normalized_location="remote",
            employment_type="FULL_TIME",
            remote_type="REMOTE",
            description="Operations role dismissed by reviewer.",
            posted_at=datetime(2026, 1, 15, 8, 0, tzinfo=UTC),
            collected_at=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
            application_url="https://stripe.com/jobs/apply/5",
            application_status="NOT_APPLIED",
            role_priority="P2",
            relevance_status="ADJACENT",
            eligibility_status="ELIGIBLE",
            source_verified=True,
            jd_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=False,
            verification_confidence=Decimal("0.80"),
        )

        session.add_all([job_1, job_2, job_3, job_4, job_5])
        await session.flush()

        # 6. Provenance references
        ref_1 = JobSourceReference(
            job_id=job_1.id,
            raw_job_id=raw_1.id,
            source_id=src_gh.id,
            source_job_id="stripe-gh-1",
            source_url="https://boards.greenhouse.io/stripe/jobs/1",
            application_url=job_1.application_url,
            is_primary=True,
        )
        ref_2 = JobSourceReference(
            job_id=job_2.id,
            raw_job_id=raw_2.id,
            source_id=src_gh.id,
            source_job_id="stripe-gh-2",
            source_url="https://boards.greenhouse.io/stripe/jobs/2",
            application_url=job_2.application_url,
            is_primary=True,
        )
        ref_3 = JobSourceReference(
            job_id=job_3.id,
            raw_job_id=raw_3.id,
            source_id=src_lever.id,
            source_job_id="dd-lever-3",
            source_url="https://jobs.lever.co/datadog/3",
            application_url=job_3.application_url,
            is_primary=True,
        )
        ref_4 = JobSourceReference(
            job_id=job_4.id,
            raw_job_id=raw_4.id,
            source_id=src_lever.id,
            source_job_id="dd-lever-4",
            source_url="https://jobs.lever.co/datadog/4",
            application_url=job_4.application_url,
            is_primary=True,
        )
        ref_5 = JobSourceReference(
            job_id=job_5.id,
            raw_job_id=raw_5.id,
            source_id=src_gh.id,
            source_job_id="stripe-gh-5",
            source_url="https://boards.greenhouse.io/stripe/jobs/5",
            application_url=job_5.application_url,
            is_primary=True,
        )
        session.add_all([ref_1, ref_2, ref_3, ref_4, ref_5])

        # 7. Initial verification and filter decision records
        vr_1 = VerificationRecord(
            job_id=job_1.id,
            source_verified=True,
            jd_verified=True,
            application_url_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=True,
            posting_date_verified=True,
            verification_confidence=Decimal("0.98"),
            passed=True,
            audit_trail={"initial": "pass"},
        )
        fd_1 = FilterDecision(
            job_id=job_1.id,
            rule_version="RULES_V1",
            final_status="ELIGIBLE",
            role_priority="P1",
            reason_codes=["TARGET_ROLE_MATCH"],
            evaluation_context={"model": "v1"},
        )
        fd_5 = FilterDecision(
            job_id=job_5.id,
            rule_version="HUMAN_REVIEW_V1",
            final_status="DISMISSED",
            role_priority="P2",
            reason_codes=["HUMAN_DISMISS"],
            evaluation_context={"reason": "Operations role dismissed by reviewer."},
            evaluated_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )
        session.add_all([vr_1, fd_1, fd_5])

        await session.commit()

        return {
            "stripe_infra": job_1.id,
            "stripe_frontend": job_2.id,
            "datadog_ml": job_3.id,
            "datadog_sales": job_4.id,
            "stripe_dismissed": job_5.id,
        }


@pytest.mark.asyncio
async def test_integration_list_jobs_feed() -> None:
    """Verify live GET /api/v1/jobs excludes dismissed vacancies by default and provides full pagination metadata."""
    seeded = await _seed_test_jobs()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/jobs")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4  # 4 active, 1 dismissed excluded!
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert len(data["items"]) == 4

    # Verify primary ATS sources are populated eagerly
    sources = {item["primary_source"] for item in data["items"]}
    assert "greenhouse" in sources
    assert "lever" in sources

    # Ensure dismissed job is absent
    returned_ids = {item["id"] for item in data["items"]}
    assert str(seeded["stripe_dismissed"]) not in returned_ids


@pytest.mark.asyncio
async def test_integration_list_jobs_filtering() -> None:
    """Verify faceted filtering by eligibility, priority, provider, and remote status."""
    seeded = await _seed_test_jobs()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Eligibility Filter
        res_elig = await client.get("/api/v1/jobs?eligibility=ELIGIBLE")
        assert res_elig.status_code == 200
        data_elig = res_elig.json()
        assert data_elig["total"] == 2
        assert {item["id"] for item in data_elig["items"]} == {
            str(seeded["stripe_infra"]),
            str(seeded["datadog_ml"]),
        }

        # 2. Priority Filter
        res_p1 = await client.get("/api/v1/jobs?priority=P1")
        assert res_p1.status_code == 200
        assert res_p1.json()["total"] == 2

        # 3. Provider Filter
        res_lever = await client.get("/api/v1/jobs?provider=lever")
        assert res_lever.status_code == 200
        data_lever = res_lever.json()
        assert data_lever["total"] == 2
        assert all(item["primary_source"] == "lever" for item in data_lever["items"])

        # 4. Remote Type Filter
        res_remote = await client.get("/api/v1/jobs?remote_type=REMOTE")
        assert res_remote.status_code == 200
        assert res_remote.json()["total"] == 1
        assert res_remote.json()["items"][0]["title"] == "Staff Infrastructure Engineer"

        # 5. Text Search
        res_search = await client.get("/api/v1/jobs?search=Machine")
        assert res_search.status_code == 200
        assert res_search.json()["total"] == 1
        assert res_search.json()["items"][0]["id"] == str(seeded["datadog_ml"])

        # 6. Include Dismissed
        res_all = await client.get("/api/v1/jobs?include_dismissed=true")
        assert res_all.status_code == 200
        assert res_all.json()["total"] == 5
        assert str(seeded["stripe_dismissed"]) in {item["id"] for item in res_all.json()["items"]}


@pytest.mark.asyncio
async def test_integration_get_job_detail_live() -> None:
    """Verify live GET /api/v1/jobs/{job_id} retrieves 7-dimension verification and audit decisions."""
    seeded = await _seed_test_jobs()
    infra_id = seeded["stripe_infra"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/jobs/{infra_id}")
        assert response.status_code == 200
        data = response.json()

        assert data["id"] == str(infra_id)
        assert data["title"] == "Staff Infrastructure Engineer"
        assert data["source_verified"] is True
        assert data["jd_verified"] is True
        assert data["application_url_verified"] is True
        assert data["company_verified"] is True
        assert data["location_verified"] is True
        assert data["experience_verified"] is True
        assert data["salary_verified"] is True
        assert data["posting_date_verified"] is True
        assert data["verification_confidence"] == 0.98

        # Provenance
        assert len(data["sources"]) == 1
        assert data["sources"][0]["source_name"] == "greenhouse"
        assert data["sources"][0]["source_job_id"] == "stripe-gh-1"

        # Decisions
        assert len(data["filter_decisions"]) >= 1
        assert data["filter_decisions"][0]["rule_version"] == "RULES_V1"

        # 404 Case
        res_404 = await client.get(f"/api/v1/jobs/{uuid.uuid4()}")
        assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_integration_filter_facets_live() -> None:
    """Verify live GET /api/v1/jobs/filters computes database aggregates correctly."""
    await _seed_test_jobs()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/jobs/filters")

    assert response.status_code == 200
    data = response.json()

    assert data["total_jobs"] == 4
    assert data["total_eligible"] == 2
    assert data["total_review"] == 1
    assert data["total_rejected"] == 1
    assert data["total_dismissed"] == 1
    assert data["provider_counts"]["greenhouse"] == 2
    assert data["provider_counts"]["lever"] == 2


@pytest.mark.asyncio
async def test_integration_companies_endpoint_live() -> None:
    """Verify live GET /api/v1/companies lists companies with active job counts."""
    await _seed_test_jobs()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/companies")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    comp_map = {c["name"]: c for c in data}
    assert "Stripe" in comp_map
    assert "Datadog" in comp_map

    # Stripe has 3 total jobs, 1 dismissed -> 2 active!
    assert comp_map["Stripe"]["active_jobs_count"] == 2
    assert comp_map["Stripe"]["sources"][0]["source_name"] == "greenhouse"

    # Datadog has 2 jobs, 0 dismissed -> 2 active!
    assert comp_map["Datadog"]["active_jobs_count"] == 2
    assert comp_map["Datadog"]["sources"][0]["source_name"] == "lever"


@pytest.mark.asyncio
async def test_integration_review_queue_and_actions() -> None:
    """Verify human review queue and approve/reject actions preserve deterministic eligibility and candidate tracking."""
    seeded = await _seed_test_jobs()
    review_job_id = seeded["stripe_frontend"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Review Queue Listing
        res_queue = await client.get("/api/v1/review-queue")
        assert res_queue.status_code == 200
        data_queue = res_queue.json()
        assert data_queue["total"] == 1
        assert data_queue["items"][0]["id"] == str(review_job_id)
        assert data_queue["items"][0]["review_reason"] == "EXPERIENCE_AMBIGUOUS"

        # 2. Human APPROVE Action
        res_approve = await client.post(
            f"/api/v1/jobs/{review_job_id}/review",
            json={
                "action": "APPROVE",
                "notes": "Reviewed by Lead Recruiter - acceptable React/TypeScript background",
                "reviewer": "recruiter_tanmay",
            },
        )
        assert res_approve.status_code == 200
        data_app = res_approve.json()
        assert data_app["human_review_status"] == "APPROVED"
        assert data_app["application_status"] == "NOT_APPLIED"  # Independent candidate tracking!
        assert data_app["eligibility_status"] == "REVIEW"  # CRITICAL: Deterministic rule preserved!

        # 3. Candidate Application Status Tracking Update
        res_track = await client.patch(
            f"/api/v1/jobs/{review_job_id}/application-status",
            json={"application_status": "APPLIED"},
        )
        assert res_track.status_code == 200
        assert res_track.json()["application_status"] == "APPLIED"

    # 4. Direct DB Check: Confirm rule immutability & audit record
    session_factory = get_sessionmaker()
    async with session_factory() as s:
        job = await s.get(Job, review_job_id)
        assert job is not None
        assert job.application_status == "APPLIED"
        assert job.eligibility_status == "REVIEW"

        # Check audit record
        stmt = select(FilterDecision).where(
            FilterDecision.job_id == review_job_id,
            FilterDecision.rule_version == "HUMAN_REVIEW_V1",
        )
        res = await s.execute(stmt)
        dec = res.scalar_one_or_none()
        assert dec is not None
        assert dec.final_status == "APPROVED"
        assert dec.evaluation_context["reviewer"] == "recruiter_tanmay"
        assert "acceptable React/TypeScript" in dec.evaluation_context["notes"]


@pytest.mark.asyncio
async def test_integration_dismiss_action_live() -> None:
    """Verify dismissing a job records dismissal audit record and excludes it from the default feed."""
    seeded = await _seed_test_jobs()
    target_id = seeded["datadog_ml"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Dismiss the job
        res_dismiss = await client.post(
            f"/api/v1/jobs/{target_id}/dismiss",
            json={"reason": "Candidate location incompatible", "reviewer": "ops_user"},
        )
        assert res_dismiss.status_code == 200
        data_dis = res_dismiss.json()
        assert data_dis["human_review_status"] == "DISMISSED"
        assert data_dis["application_status"] == "NOT_APPLIED"

        # 2. Verify excluded from default feed
        res_feed = await client.get("/api/v1/jobs")
        assert res_feed.status_code == 200
        active_ids = {item["id"] for item in res_feed.json()["items"]}
        assert str(target_id) not in active_ids

        # 3. Verify present with include_dismissed=true
        res_feed_all = await client.get("/api/v1/jobs?include_dismissed=true")
        assert res_feed_all.status_code == 200
        all_ids = {item["id"] for item in res_feed_all.json()["items"]}
        assert str(target_id) in all_ids


@pytest.mark.asyncio
async def test_integration_web_views_live() -> None:
    """Verify HTML dashboard, feed, detail, queue, and companies views render successfully against live PostgreSQL."""
    seeded = await _seed_test_jobs()
    infra_id = seeded["stripe_infra"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Dashboard
        res_dash = await client.get("/dashboard")
        assert res_dash.status_code == 200
        assert "Job Intelligence Dashboard" in res_dash.text
        assert "Stripe" in res_dash.text

        # 2. Feed
        res_feed = await client.get("/jobs")
        assert res_feed.status_code == 200
        assert "Staff Infrastructure Engineer" in res_feed.text

        # 3. Detail
        res_detail = await client.get(f"/jobs/{infra_id}")
        assert res_detail.status_code == 200
        assert "8-Dimension Deterministic Verification" in res_detail.text
        assert "Stripe" in res_detail.text

        # 4. Review Queue
        res_queue = await client.get("/review-queue")
        assert res_queue.status_code == 200
        assert "Human Review Queue" in res_queue.text

        # 5. Companies
        res_comp = await client.get("/companies")
        assert res_comp.status_code == 200
        assert "Tracked Companies & ATS Boards" in res_comp.text
        assert "Datadog" in res_comp.text


@pytest.mark.asyncio
async def test_integration_no_n_plus_one_queries() -> None:
    """Verify GET /api/v1/jobs executes a strictly bounded O(1) number of SQL queries regardless of item count."""
    from sqlalchemy import event

    await _seed_test_jobs()
    queries = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    session_factory = get_sessionmaker()
    engine = session_factory.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/v1/jobs?page=1&page_size=20")
            assert res.status_code == 200
            assert len(res.json()["items"]) > 0

        # Assert query count is bounded by a constant O(1) number of statements
        # (count, select jobs, select companies, select source refs, select sources, select filter decisions)
        # Exactly 6 or fewer queries for the entire batch.
        assert len(queries) <= 6, f"Expected <= 6 queries, got {len(queries)}: {queries}"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)


@pytest.mark.asyncio
async def test_integration_repeated_human_review_transitions() -> None:
    """Verify repeated human review transitions (dismissed -> approved -> dismissed).

    Validates:
    1. Main feed uses latest HUMAN_REVIEW_V1 decision via correlated scalar subquery.
    2. Deterministic Job.eligibility_status and candidate application_status are never modified.
    3. Dismissed job is excluded from main feed, reappears when approved, and disappears when dismissed again.
    4. Review queue excludes jobs once any human review decision exists.
    """
    seeded = await _seed_test_jobs()
    target_id = seeded["stripe_frontend"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Initial check: job is in review queue and visible in feed
        res_init_queue = await client.get("/api/v1/review-queue")
        assert res_init_queue.status_code == 200
        assert str(target_id) in {item["id"] for item in res_init_queue.json()["items"]}

        res_init_feed = await client.get("/api/v1/jobs")
        assert res_init_feed.status_code == 200
        assert str(target_id) in {item["id"] for item in res_init_feed.json()["items"]}

        # Transition 1: Dismiss the job
        res_dis1 = await client.post(
            f"/api/v1/jobs/{target_id}/dismiss",
            json={"reason": "Dismissed initially", "reviewer": "user1"},
        )
        assert res_dis1.status_code == 200
        assert res_dis1.json()["human_review_status"] == "DISMISSED"

        # Check: excluded from main feed, absent from review queue, present with include_dismissed=true
        res_feed_dis1 = await client.get("/api/v1/jobs")
        assert str(target_id) not in {item["id"] for item in res_feed_dis1.json()["items"]}

        res_queue_dis1 = await client.get("/api/v1/review-queue")
        assert str(target_id) not in {item["id"] for item in res_queue_dis1.json()["items"]}

        res_all_dis1 = await client.get("/api/v1/jobs?include_dismissed=true")
        assert str(target_id) in {item["id"] for item in res_all_dis1.json()["items"]}

        # Transition 2: Reviewer changes mind and approves the previously dismissed job
        res_app2 = await client.post(
            f"/api/v1/jobs/{target_id}/review",
            json={
                "action": "APPROVE",
                "notes": "Reconsidering candidate - looks strong",
                "reviewer": "user2",
            },
        )
        assert res_app2.status_code == 200
        assert res_app2.json()["human_review_status"] == "APPROVED"
        assert res_app2.json()["eligibility_status"] == "REVIEW"  # Deterministic status preserved!

        # Check: reappears in default main feed!
        res_feed_app2 = await client.get("/api/v1/jobs")
        assert str(target_id) in {item["id"] for item in res_feed_app2.json()["items"]}

        # Check: still excluded from review queue (since it has been reviewed)
        res_queue_app2 = await client.get("/api/v1/review-queue")
        assert str(target_id) not in {item["id"] for item in res_queue_app2.json()["items"]}

        # Check: matches human_review_status=APPROVED filter
        res_filt_app = await client.get("/api/v1/jobs?human_review_status=APPROVED")
        assert str(target_id) in {item["id"] for item in res_filt_app.json()["items"]}

        # Transition 3: Reviewer dismisses it again
        res_dis3 = await client.post(
            f"/api/v1/jobs/{target_id}/dismiss",
            json={"reason": "Closed vacancy externally", "reviewer": "user3"},
        )
        assert res_dis3.status_code == 200
        assert res_dis3.json()["human_review_status"] == "DISMISSED"

        # Check: excluded from main feed again
        res_feed_dis3 = await client.get("/api/v1/jobs")
        assert str(target_id) not in {item["id"] for item in res_feed_dis3.json()["items"]}

        # Check: does NOT match human_review_status=APPROVED anymore
        res_filt_app3 = await client.get("/api/v1/jobs?human_review_status=APPROVED")
        assert str(target_id) not in {item["id"] for item in res_filt_app3.json()["items"]}

        # Check: matches human_review_status=DISMISSED with include_dismissed=true
        res_filt_dis3 = await client.get(
            "/api/v1/jobs?human_review_status=DISMISSED&include_dismissed=true"
        )
        assert str(target_id) in {item["id"] for item in res_filt_dis3.json()["items"]}

    # Direct DB verification: check all 3 audit records exist, deterministic eligibility unchanged
    session_factory = get_sessionmaker()
    async with session_factory() as s:
        job = await s.get(Job, target_id)
        assert job is not None
        assert job.eligibility_status == "REVIEW"  # Still REVIEW!
        assert job.application_status == "NOT_APPLIED"  # Still NOT_APPLIED!

        stmt = (
            select(FilterDecision)
            .where(
                FilterDecision.job_id == target_id,
                FilterDecision.rule_version == "HUMAN_REVIEW_V1",
            )
            .order_by(FilterDecision.evaluated_at.asc())
        )
        res = await s.execute(stmt)
        decisions = res.scalars().all()
        assert len(decisions) == 3
        assert [d.final_status for d in decisions] == ["DISMISSED", "APPROVED", "DISMISSED"]


@pytest.mark.asyncio
async def test_integration_csrf_origin_protection_live() -> None:
    """Verify live state-changing endpoints reject untrusted origins with 403 Forbidden."""
    seeded = await _seed_test_jobs()
    target_id = seeded["stripe_frontend"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Untrusted origin on review endpoint
        res_review_evil = await client.post(
            f"/api/v1/jobs/{target_id}/review",
            json={"action": "APPROVE"},
            headers={"Origin": "https://malicious-phishing.com"},
        )
        assert res_review_evil.status_code == 403
        assert "CSRF origin validation failed" in res_review_evil.json()["detail"]

        # 2. Untrusted origin on dismiss endpoint
        res_dismiss_evil = await client.post(
            f"/api/v1/jobs/{target_id}/dismiss",
            json={"reason": "Attacker payload"},
            headers={"Origin": "https://attacker.org"},
        )
        assert res_dismiss_evil.status_code == 403
        assert "CSRF origin validation failed" in res_dismiss_evil.json()["detail"]

        # 3. Untrusted referer on application-status PATCH endpoint
        res_patch_evil = await client.patch(
            f"/api/v1/jobs/{target_id}/application-status",
            json={"application_status": "APPLIED"},
            headers={"Referer": "https://attacker.org/payload.html"},
        )
        assert res_patch_evil.status_code == 403
        assert "CSRF referer validation failed" in res_patch_evil.json()["detail"]

        # 4. Valid trusted Origin succeeds
        res_review_ok = await client.post(
            f"/api/v1/jobs/{target_id}/review",
            json={"action": "APPROVE", "notes": "Approved from trusted dashboard"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert res_review_ok.status_code == 200
        assert res_review_ok.json()["human_review_status"] == "APPROVED"

        # 5. Host header manipulation / spoofing attack
        res_host_spoof = await client.post(
            f"/api/v1/jobs/{target_id}/dismiss",
            json={"reason": "Host header spoofing"},
            headers={
                "Host": "evil-attacker.com",
                "Origin": "https://evil-attacker.com",
            },
        )
        assert res_host_spoof.status_code in (400, 403)

        # 6. DNS rebinding scenario
        res_rebind = await client.post(
            f"/api/v1/jobs/{target_id}/dismiss",
            json={"reason": "DNS rebinding exploit"},
            headers={
                "Host": "rebind.evil.com:8000",
                "Origin": "http://rebind.evil.com:8000",
            },
        )
        assert res_rebind.status_code in (400, 403)

        # 7. Request with cookies but without Origin or Referer
        client.cookies.set("session_token", "ambient_cookie_val")
        try:
            res_cookie_no_ref = await client.post(
                f"/api/v1/jobs/{target_id}/dismiss",
                json={"reason": "Cookie without origin"},
            )
            assert res_cookie_no_ref.status_code == 403
            assert "CSRF validation failed" in res_cookie_no_ref.json()["detail"]
        finally:
            client.cookies.clear()
