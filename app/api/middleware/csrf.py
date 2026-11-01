"""CSRF origin and referer validation middleware for state-changing browser requests."""

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.status import HTTP_403_FORBIDDEN

from app.config.settings import get_settings


class CSRFOriginValidationMiddleware(BaseHTTPMiddleware):
    """Enforces strict Origin and Referer validation on state-changing browser requests (POST, PATCH, PUT, DELETE).

    Directives:
    1. Rejects requests with untrusted or cross-origin Origin headers (e.g. from malicious sites).
    2. Rejects 'Origin: null' explicitly (e.g. sandboxed iframes, file:// contexts).
    3. Validates Referer origin when Origin header is omitted by browser.
    4. Permits same-origin browser requests matching statically configured trusted origins.
       Arbitrary incoming Host headers are NEVER dynamically trusted to eliminate Host-header
       poisoning and DNS-rebinding attack surfaces.
    5. Rejects cookie-bearing requests that lack both Origin and Referer headers to prevent
       no-referrer CSRF bypasses.
    6. Permits non-browser programmatic API clients (curl, automated CLI) when neither
       Origin, Referer, nor ambient browser cookies are present.
    """

    def __init__(self, app, settings=None):
        super().__init__(app)
        self.settings = settings or get_settings()
        self.trusted_origins = self.get_trusted_origins(self.settings)

    async def dispatch(self, request: Request, call_next):
        # Only inspect state-changing HTTP verbs
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")

            # Check 1: If request carries cookies, it is a browser session.
            # Reject if an attacker used Referrer-Policy: no-referrer to strip both Origin and Referer.
            if request.cookies and not origin and not referer:
                return JSONResponse(
                    status_code=HTTP_403_FORBIDDEN,
                    content={
                        "detail": (
                            "CSRF validation failed: authenticated browser requests with cookies "
                            "must include a valid Origin or Referer header."
                        )
                    },
                )

            # Check 2: If either origin or referer is provided, enforce validation against static trusted origins
            if origin or referer:
                trusted_origins = self.trusted_origins

                if origin:
                    cleaned_origin = origin.strip().rstrip("/")
                    if cleaned_origin == "null" or cleaned_origin not in trusted_origins:
                        return JSONResponse(
                            status_code=HTTP_403_FORBIDDEN,
                            content={
                                "detail": f"CSRF origin validation failed: untrusted origin '{origin}'."
                            },
                        )
                elif referer:
                    ref_origin = self._extract_origin(referer)
                    if not ref_origin or ref_origin not in trusted_origins:
                        return JSONResponse(
                            status_code=HTTP_403_FORBIDDEN,
                            content={
                                "detail": f"CSRF referer validation failed: untrusted referer '{referer}'."
                            },
                        )

        return await call_next(request)

    @staticmethod
    def _extract_origin(url: str) -> str | None:
        """Extract scheme://netloc from a full URL."""
        try:
            parsed = urlparse(url)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        except Exception:
            pass
        return None

    @classmethod
    def get_trusted_origins(cls, settings) -> set[str]:
        """Construct the immutable set of trusted origins from application settings.

        Environment Separation:
        - In development and test environments (APP_ENV in ('development', 'testing')):
          Enables local development origins (localhost, 127.0.0.1, test, testserver).
        - In production and staging environments (APP_ENV in ('production', 'staging')):
          Restricts trusted origins strictly to DASHBOARD_BASE_URL and explicitly configured
          additional origins. Localhost, 127.0.0.1, and test runners are completely disabled.
        - API_HOST (the server bind address) is NEVER automatically treated as a browser origin.
        - Arbitrary incoming Host headers are NEVER dynamically trusted.
        """
        trusted: set[str] = set()

        # 1. Configured dashboard origin (required in all environments)
        if settings.DASHBOARD_BASE_URL:
            dash_origin = cls._extract_origin(settings.DASHBOARD_BASE_URL)
            if dash_origin:
                trusted.add(dash_origin)

        # 2. Explicitly configured additional trusted origins
        additional = getattr(settings, "ADDITIONAL_TRUSTED_ORIGINS", None) or []
        for origin in additional:
            extracted = cls._extract_origin(origin) or origin.strip().rstrip("/")
            if extracted and extracted != "null":
                trusted.add(extracted)

        # 3. Development and test origins (strictly excluded in production / staging)
        if (
            settings.APP_ENV in ("development", "testing")
            and getattr(settings, "DEBUG", True) is not False
        ):
            trusted.update(
                {
                    "http://localhost:8000",
                    "http://127.0.0.1:8000",
                    "http://test",
                    "https://test",
                    "http://testserver",
                    "https://testserver",
                }
            )

        return trusted
