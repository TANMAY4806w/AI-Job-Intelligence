"""Deterministic Job Normalization Service (V1).

Boundary: Pure, database-independent transformation service that accepts extracted
job data and produces deterministic normalized attributes suitable for canonical
Job persistence. Operates without database access, network I/O, or LLM invocation.
"""

import copy
import re
import uuid
from typing import Any

from app.normalization.compensation import parse_compensation
from app.normalization.dates import parse_publication_date
from app.normalization.experience import parse_experience
from app.normalization.location import normalize_location
from app.normalization.schemas import ExtractedJobPayload, NormalizedJobData
from app.normalization.text import clean_bullet_points, clean_html_to_text, clean_text
from app.normalization.title import normalize_title
from app.normalization.urls import normalize_url

_EMPLOYMENT_TYPE_MAP = {
    "full-time": "FULL_TIME",
    "full_time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
    "part-time": "PART_TIME",
    "part_time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "temporary": "CONTRACT",
    "freelance": "CONTRACT",
    "intern": "INTERNSHIP",
    "internship": "INTERNSHIP",
    "trainee": "INTERNSHIP",
    "unknown": "UNKNOWN",
}

_REMOTE_TYPE_MAP = {
    "remote": "REMOTE",
    "wfh": "REMOTE",
    "work from home": "REMOTE",
    "hybrid": "HYBRID",
    "partially remote": "HYBRID",
    "on-site": "ON_SITE",
    "onsite": "ON_SITE",
    "in-office": "ON_SITE",
    "in office": "ON_SITE",
    "unknown": "UNKNOWN",
}


def _normalize_employment_type(raw_val: str | None) -> str | None:
    if not raw_val or not isinstance(raw_val, str):
        return None
    cleaned = raw_val.strip().lower()
    if not cleaned:
        return None
    return _EMPLOYMENT_TYPE_MAP.get(cleaned, raw_val.strip().upper())


def _normalize_remote_type(
    raw_val: str | None,
    location_remote_hint: str | None = None,
) -> str:
    if raw_val and isinstance(raw_val, str):
        cleaned = raw_val.strip().lower()
        if cleaned in _REMOTE_TYPE_MAP:
            return _REMOTE_TYPE_MAP[cleaned]
    if location_remote_hint in ("REMOTE", "HYBRID", "ON_SITE"):
        return location_remote_hint
    return "UNKNOWN"


def _clean_skills(skills: Any) -> list[str] | None:
    if not skills:
        return None
    cleaned_list: list[str] = []
    seen: set[str] = set()

    raw_items = skills if isinstance(skills, (list, tuple, set)) else str(skills).split(",")
    for item in raw_items:
        if isinstance(item, str):
            clean = re.sub(r"\s+", " ", item).strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                cleaned_list.append(clean)

    return cleaned_list if cleaned_list else None


class JobNormalizationService:
    """Pure deterministic normalization service for job vacancies."""

    def normalize(
        self,
        extracted: ExtractedJobPayload | dict[str, Any],
        *,
        company_id: uuid.UUID | None = None,
        company_name: str | None = None,
        payload_format: str | None = None,
    ) -> NormalizedJobData:
        """Transform raw/extracted job data into deterministic NormalizedJobData.

        Guarantees:
            1. Pure function: produces identical results for identical inputs.
            2. Never mutates input data or raw evidence payloads.
            3. Zero database dependency or network I/O.
            4. Validates required fields without rejecting valid optional data.
            5. Decouples pure job content normalization from company identity resolution.

        Args:
            extracted: Pydantic ExtractedJobPayload model or generic dictionary.
            company_id: Optional canonical company UUID reference.
            company_name: Optional canonical company display name.
            payload_format: Optional payload format ('JSON' or 'HTML').

        Returns:
            NormalizedJobData: Standardized canonical attributes.

        Raises:
            ValueError: If required job content (title, description) is missing
                        or if URLs/dates are syntactically malformed.
            TypeError: If input types are unsupported.
        """
        # Convert dictionary to shallow copy to guarantee caller payload immutability
        if isinstance(extracted, dict):
            data = copy.deepcopy(extracted)
        elif isinstance(extracted, ExtractedJobPayload):
            data = extracted.model_dump()
        else:
            raise TypeError(
                f"Unsupported extracted data type: {type(extracted).__name__}. "
                "Expected ExtractedJobPayload or dict."
            )

        # Determine payload format
        fmt = payload_format or data.get("payload_format", "JSON")
        if fmt not in ("JSON", "HTML"):
            raise ValueError(f"Unsupported payload format: {fmt!r}. Expected 'JSON' or 'HTML'")

        # 1. Company Name & Reference Handling (Decoupled from pure content validation)
        raw_company = company_name or data.get("company_name")
        cleaned_company_name: str | None = None
        if raw_company and isinstance(raw_company, str) and raw_company.strip():
            cleaned_company_name = re.sub(r"\s+", " ", raw_company).strip()

        resolved_company_id = company_id or data.get("company_id")
        if resolved_company_id is not None and not isinstance(resolved_company_id, uuid.UUID):
            if isinstance(resolved_company_id, str):
                try:
                    resolved_company_id = uuid.UUID(resolved_company_id)
                except ValueError as err:
                    raise ValueError(
                        f"Invalid company UUID string: {resolved_company_id!r}"
                    ) from err
            else:
                raise TypeError(f"Invalid company_id type: {type(resolved_company_id).__name__}")

        # 2. Title Normalization
        raw_title = data.get("title")
        if not raw_title or not isinstance(raw_title, str) or not raw_title.strip():
            raise ValueError("Job title is required and cannot be empty")
        cleaned_title, normalized_title = normalize_title(raw_title)

        # 3. Description Cleaning (Format-Aware)
        raw_description = data.get("description")
        if (
            not raw_description
            or not isinstance(raw_description, str)
            or not raw_description.strip()
        ):
            raise ValueError("Job description is required and cannot be empty")

        if (
            fmt == "HTML"
            or "<p>" in raw_description
            or "<br" in raw_description
            or "<div>" in raw_description
        ):
            description = clean_html_to_text(raw_description)
        else:
            description = clean_text(raw_description)

        if not description:
            raise ValueError("Job description cannot be empty after text sanitation")

        # 4. Location Normalization (Preserves NULL for unknown/ambiguous)
        raw_location = data.get("location")
        cleaned_location, normalized_location, remote_hint = normalize_location(raw_location)

        # 5. Remote and Employment Type
        remote_type = _normalize_remote_type(data.get("remote_type"), remote_hint)
        employment_type = _normalize_employment_type(data.get("employment_type"))

        # 6. Application URL Normalization
        raw_app_url = data.get("application_url")
        application_url = normalize_url(raw_app_url)

        # 7. Publication Date Parsing (Preserves NULL for missing; rejects malformed)
        raw_posted_at = data.get("posted_at")
        posted_at = parse_publication_date(raw_posted_at)

        # 8. Structured Text Fields
        responsibilities = clean_bullet_points(data.get("responsibilities"))
        requirements = clean_bullet_points(data.get("requirements"))
        skills = _clean_skills(data.get("skills"))

        # 9. Experience Parsing
        experience_raw, exp_min, exp_max = parse_experience(
            raw_exp=data.get("experience_raw"),
            min_years=data.get("experience_min_years"),
            max_years=data.get("experience_max_years"),
        )

        # 10. Compensation Parsing
        sal_min, sal_max, sal_currency, sal_period = parse_compensation(
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            currency=data.get("salary_currency"),
            period=data.get("salary_period"),
            raw_text=data.get("salary_raw") or data.get("compensation_raw"),
        )

        return NormalizedJobData(
            title=cleaned_title,
            normalized_title=normalized_title,
            company_name=cleaned_company_name,
            company_id=resolved_company_id,
            location=cleaned_location,
            normalized_location=normalized_location,
            employment_type=employment_type,
            remote_type=remote_type,
            description=description,
            responsibilities=responsibilities,
            requirements=requirements,
            skills=skills,
            experience_raw=experience_raw,
            experience_min_years=exp_min,
            experience_max_years=exp_max,
            salary_min=sal_min,
            salary_max=sal_max,
            salary_currency=sal_currency,
            salary_period=sal_period,
            posted_at=posted_at,
            application_url=application_url,
        )


# Module-level convenience function
def normalize_job_payload(
    extracted: ExtractedJobPayload | dict[str, Any],
    *,
    company_id: uuid.UUID | None = None,
    company_name: str | None = None,
    payload_format: str | None = None,
) -> NormalizedJobData:
    """Normalize extracted job payload using singleton service."""
    service = JobNormalizationService()
    return service.normalize(
        extracted,
        company_id=company_id,
        company_name=company_name,
        payload_format=payload_format,
    )
