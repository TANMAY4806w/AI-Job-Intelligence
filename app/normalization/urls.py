"""Deterministic application URL normalization.

Boundary: Standardizes application and ATS links for Stage 2 exact endpoint
deduplication matching. Strips promotional and tracking query parameters while
preserving meaningful application tokens, job IDs, and path case sensitivity.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking parameters to strip from application URLs
_TRACKING_PARAMS = {
    # UTM tracking
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_reader",
    "utm_name",
    # Ad network and click trackers
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "twclid",
    "igshid",
    "mc_eid",
    # Analytics session tokens
    "_ga",
    "_gl",
    "_hsenc",
    "_hsmi",
    "hsctatracking",
    # Generic referral & campaign tags
    "ref",
    "reference",
    "source",
    "campaign",
    "trk",
    "tracking",
    "spjobid",
    # ATS referral tracking
    "gh_src",
    "lever-source",
    "ashby_source",
}


def normalize_url(url: str | None) -> str | None:
    """Normalize and canonicalize an application or source URL.

    Args:
        url: Raw URL string.

    Returns:
        str | None: Canonical URL, or None if input is empty or None.

    Raises:
        ValueError: If url is malformed (unsupported scheme or missing host).
    """
    if url is None:
        return None

    cleaned = url.strip()
    if not cleaned:
        return None

    split = urlsplit(cleaned)

    # Validate scheme
    scheme = split.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"Invalid URL scheme '{split.scheme}'. Only HTTP and HTTPS are supported: {url!r}"
        )

    # Validate host
    netloc = split.netloc.lower()
    if not netloc:
        raise ValueError(f"Invalid URL without host: {url!r}")

    # Strip default port numbers
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    # Normalize path (preserve case, strip redundant slashes, strip trailing slash if length > 1)
    path = split.path
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Filter and sort query parameters
    query = ""
    if split.query:
        parsed_params = parse_qsl(split.query, keep_blank_values=False)
        filtered = [(k, v) for k, v in parsed_params if k.lower() not in _TRACKING_PARAMS]
        # Sort deterministically by key, then value
        filtered.sort(key=lambda item: (item[0], item[1]))
        query = urlencode(filtered)

    # Reconstruct canonical URL (strip fragment)
    return urlunsplit((scheme, netloc, path, query, ""))
