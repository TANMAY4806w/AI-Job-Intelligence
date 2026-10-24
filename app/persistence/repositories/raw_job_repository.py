"""RawJob asynchronous persistence repository.

Enforces strict append-only semantics for raw external evidence.
No update or delete operations are permitted at the repository level.
Preserves exact wire-level HTTP response bytes in raw_body (BYTEA) alongside
parsed semantic JSON in raw_payload (JSONB) with SHA-256 content_hash verification
and cross-payload consistency validation.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.persistence.models.raw_job import RawJob
from app.persistence.repositories.base import BaseRepository


class RawJobRepository(BaseRepository):
    """Repository managing immutable, append-only RawJob evidence.

    Crucial invariants:
    1. Append-only: Update and delete methods are deliberately omitted from the repository interface.
    2. Exact byte preservation: Original source HTTP wire response bytes are preserved in raw_body (BYTEA).
    3. Semantic JSON data: Parsed structured payload is stored in raw_payload (JSONB) for high-performance querying without synthetic property injection.
    4. Cryptographic integrity: content_hash MUST strictly match SHA256(raw_body).
    5. Wire-to-payload consistency: raw_body must be valid UTF-8 JSON whose parsed structure strictly matches raw_payload.
    """

    async def get_by_id(self, raw_job_id: uuid.UUID) -> RawJob | None:
        """Retrieve a RawJob by primary key UUID."""
        return await self.session.get(RawJob, raw_job_id)

    async def get_latest_by_company_source_and_external_id(
        self,
        company_source_id: uuid.UUID,
        source_job_id: str,
    ) -> RawJob | None:
        """Retrieve the most recent RawJob observation for a company source and external job ID.

        Utilizes the idx_raw_jobs_comp_src_ext compound index.
        """
        stmt = (
            select(RawJob)
            .where(
                RawJob.company_source_id == company_source_id,
                RawJob.source_job_id == source_job_id,
            )
            .order_by(RawJob.collected_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_content_hash(self, content_hash: str) -> Sequence[RawJob]:
        """Retrieve RawJobs matching a specific SHA-256 payload content hash.

        Utilizes the idx_raw_jobs_content_hash index for deduplicating identical raw payloads.
        """
        stmt = (
            select(RawJob)
            .where(RawJob.content_hash == content_hash)
            .order_by(RawJob.collected_at.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_by_company_source_id(
        self,
        company_source_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> Sequence[RawJob]:
        """List historical raw job discoveries for a company source endpoint."""
        stmt = (
            select(RawJob)
            .where(RawJob.company_source_id == company_source_id)
            .order_by(RawJob.collected_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, raw_job: RawJob) -> RawJob:
        """Append a new RawJob record to the session and flush to surface constraints.

        Validates that:
        - raw_body is bytes.
        - raw_payload is a dictionary.
        - content_hash matches hashlib.sha256(raw_job.raw_body).hexdigest().
        - payload_format is recognized (currently 'JSON' or 'HTML').
        - For 'JSON' format:
            - raw_body is valid UTF-8 encoded text representing a JSON object.
            - parsed JSON data from raw_body strictly matches raw_payload.
        - For 'HTML' format:
            - raw_body contains the unaltered HTML source bytes.
            - raw_payload contains structured dictionary data extracted by the parser.

        Raises:
            TypeError: If raw_body is not bytes or raw_payload is not a dict.
            ValueError: If content_hash mismatches, payload_format is unsupported,
                or (for JSON) raw_body is invalid JSON / diverges from raw_payload.

        Does not mutate inputs or commit the transaction.
        """
        if not isinstance(raw_job.raw_body, (bytes, bytearray, memoryview)):
            raise TypeError("raw_job.raw_body must be bytes")
        if not isinstance(raw_job.raw_payload, dict):
            raise TypeError("raw_job.raw_payload must be a dict")

        raw_bytes = bytes(raw_job.raw_body)
        expected_hash = hashlib.sha256(raw_bytes).hexdigest()
        if raw_job.content_hash != expected_hash:
            raise ValueError(
                f"content_hash mismatch: provided '{raw_job.content_hash}' does not match "
                f"SHA-256 digest of raw_body '{expected_hash}'"
            )

        fmt = (raw_job.payload_format or "JSON").upper()
        if fmt not in ("JSON", "HTML"):
            raise ValueError(
                f"Unsupported payload_format '{raw_job.payload_format}'; expected 'JSON' or 'HTML'"
            )

        if fmt == "JSON":
            try:
                decoded_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"raw_job.raw_body contains invalid UTF-8 bytes: {exc}") from exc

            try:
                parsed_payload = json.loads(decoded_text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"raw_job.raw_body does not contain valid JSON: {exc}") from exc

            if not isinstance(parsed_payload, dict):
                raise ValueError(
                    f"raw_job.raw_body must decode to a JSON object (dict), got {type(parsed_payload).__name__}"
                )

            if parsed_payload != raw_job.raw_payload:
                raise ValueError("raw_job.raw_payload does not match parsed JSON from raw_body")

        elif fmt == "HTML":
            # For HTML responses, raw_body contains the raw HTML document bytes.
            # raw_payload contains caller/parser-extracted structured data (already validated as dict above).
            pass

        self.session.add(raw_job)
        await self.session.flush()
        return raw_job
