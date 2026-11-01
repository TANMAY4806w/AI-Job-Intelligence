"""Company and career endpoint discovery layer.

Identifies hiring companies, discovers career portals, and detects underlying ATS signatures.
Boundary: Produces company and endpoint metadata; does not process job vacancies.
"""

from app.discovery.company_registry import (
    DEFAULT_REGISTRY_PATH,
    CompanyRegistry,
    CompanyRegistryEntry,
)

__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "CompanyRegistry",
    "CompanyRegistryEntry",
]
