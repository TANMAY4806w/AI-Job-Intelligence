"""Collector Registry.

Boundary: Maps external source platform identifiers to BaseCollector implementations.
For V1:
- Registers GreenhouseCollector for 'GREENHOUSE'.
- Resolves collectors deterministically using Source.source_name.
- Rejects unsupported or unregistered platforms with UnsupportedSourceError.
- Zero guessing, zero heuristics, zero automatic URL inference.
"""

from typing import TYPE_CHECKING, Any

from app.collectors.base.collector import BaseCollector
from app.collectors.base.http import PoliteHttpClient
from app.persistence.models.source import Source

if TYPE_CHECKING:
    from app.orchestration.ingestion_service import JobIngestionService


class CollectorRegistryError(Exception):
    """Base exception for collector registry operations."""


class UnsupportedSourceError(CollectorRegistryError, ValueError):
    """Raised when a requested source platform is not supported or not registered."""

    def __init__(self, source_identifier: str) -> None:
        """Initialize UnsupportedSourceError.

        Args:
            source_identifier: Unrecognized or unsupported source platform identifier.
        """
        self.source_identifier = source_identifier
        super().__init__(
            f"Unsupported or unregistered source platform: '{source_identifier}'. "
            "Supported sources in V1: GREENHOUSE."
        )


class CollectorRegistry:
    """Registry mapping external source platform identifiers to collector implementations."""

    def __init__(self) -> None:
        """Initialize registry with standard V1 supported collectors."""
        self._registry: dict[str, type[BaseCollector]] = {}
        # Pre-register supported V1 collectors
        from app.collectors.greenhouse.collector import GreenhouseCollector

        self.register("GREENHOUSE", GreenhouseCollector)

    @staticmethod
    def _normalize_key(source_identifier: str) -> str:
        """Normalize a source platform identifier string for case-insensitive lookup.

        Args:
            source_identifier: Source platform name or token.

        Returns:
            str: Upper-cased, whitespace-stripped identifier.

        Raises:
            ValueError: If source_identifier is not a non-empty string.
        """
        if not source_identifier or not isinstance(source_identifier, str):
            raise ValueError("Source identifier must be a non-empty string.")
        cleaned = source_identifier.strip().upper()
        if not cleaned:
            raise ValueError("Source identifier cannot be blank or whitespace-only.")
        return cleaned

    def register(
        self,
        source_identifier: str,
        collector_cls: type[BaseCollector],
    ) -> None:
        """Register a collector implementation class for a source platform identifier.

        Args:
            source_identifier: Source platform name (e.g. 'GREENHOUSE', 'LEVER').
            collector_cls: Subclass of BaseCollector implementing the collection lifecycle.

        Raises:
            ValueError: If source_identifier is invalid or collector_cls is not a BaseCollector.
        """
        clean_key = self._normalize_key(source_identifier)
        if not isinstance(collector_cls, type) or not issubclass(collector_cls, BaseCollector):
            raise ValueError(
                f"collector_cls for '{clean_key}' must be a subclass of BaseCollector."
            )
        self._registry[clean_key] = collector_cls

    def unregister(self, source_identifier: str) -> bool:
        """Remove a collector implementation from the registry.

        Args:
            source_identifier: Source platform name to unregister.

        Returns:
            bool: True if collector was registered and removed, False otherwise.
        """
        clean_key = self._normalize_key(source_identifier)
        return self._registry.pop(clean_key, None) is not None

    def get(self, source_identifier: str) -> type[BaseCollector] | None:
        """Retrieve the collector class registered for a given source platform.

        Args:
            source_identifier: Source platform name or token.

        Returns:
            type[BaseCollector] | None: Registered collector class or None if not registered.
        """
        try:
            clean_key = self._normalize_key(source_identifier)
        except ValueError:
            return None
        return self._registry.get(clean_key)

    def is_supported(self, source_identifier: str) -> bool:
        """Check whether a source platform identifier has a registered collector.

        Args:
            source_identifier: Source platform name or token.

        Returns:
            bool: True if supported and registered, False otherwise.
        """
        return self.get(source_identifier) is not None

    def list_supported_sources(self) -> list[str]:
        """List all currently registered source platform identifiers.

        Returns:
            list[str]: Sorted list of uppercase source platform names.
        """
        return sorted(self._registry.keys())

    def resolve(
        self,
        source: Source | str,
        ingestion_service: "JobIngestionService",
        *,
        http_client: PoliteHttpClient | None = None,
    ) -> BaseCollector:
        """Resolve and instantiate the collector for a Source model or identifier string.

        Args:
            source: Source ORM instance or string platform name.
            ingestion_service: JobIngestionService instance required by the collector.
            http_client: Optional PoliteHttpClient instance.

        Returns:
            BaseCollector: Instantiated collector adapter.

        Raises:
            UnsupportedSourceError: If the source platform is not registered.
            ValueError: If source identifier is missing or blank.
        """
        source_identifier = source.source_name if isinstance(source, Source) else source
        collector_cls = self.get(source_identifier)
        if collector_cls is None:
            raise UnsupportedSourceError(source_identifier)
        return collector_cls(ingestion_service, http_client=http_client)


_default_collector_registry: CollectorRegistry | None = None


def get_default_collector_registry() -> CollectorRegistry:
    """Retrieve or lazily initialize the default global CollectorRegistry."""
    global _default_collector_registry
    if _default_collector_registry is None:
        _default_collector_registry = CollectorRegistry()
    return _default_collector_registry


def __getattr__(name: str) -> Any:
    """Provide lazy access to default_collector_registry to prevent circular imports."""
    if name == "default_collector_registry":
        return get_default_collector_registry()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
