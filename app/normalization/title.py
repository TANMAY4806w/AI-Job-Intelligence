"""Deterministic job title normalization.

Boundary: Standardizes job titles for classification and Stage 3 composite
deduplication matching without destroying the original advertised title.
"""

import re

# Common recruitment tags and boilerplate in brackets/parentheses
_TAG_PATTERN = re.compile(
    r"[\[(]\s*(?:urgent(?:ly)?|immediate(?:ly)?|hiring|openings?|joiners?|remote|wfh|hybrid|full[\s-]?time|part[\s-]?time|contract|req(?:uisition)?|job\s*id)[\w\s\-!#:]*[\])]",
    re.IGNORECASE,
)

# Promotional lead-in prefixes (e.g., "Urgent Hiring: Python Developer", "Immediate Opening - AI Engineer")
_PREFIX_PATTERN = re.compile(
    r"^(?:(?:urgently|urgent)\s+)?(?:hiring|opening|immediate opening|we are hiring|walk-in)\s*[:\-–|]\s*",
    re.IGNORECASE,
)

# Common title abbreviations to canonical representations
_ABBREVIATIONS = {
    "dev": "developer",
    "devs": "developers",
    "eng": "engineer",
    "engr": "engineer",
    "swe": "software engineer",
    "sde": "software development engineer",
    "fde": "forward deployed engineer",
    "ml": "machine learning",
    "ai": "ai",
    "genai": "genai",
    "sr": "senior",
    "sr.": "senior",
    "jr": "junior",
    "jr.": "junior",
}


def normalize_title(raw_title: str) -> tuple[str, str]:
    """Clean advertised title and generate canonical normalized title.

    Guarantees:
        - Preserves reported advertised title in cleaned_title.
        - Generates deterministic lowercase normalized_title for Stage 3 deduplication.
        - Expands abbreviations (e.g., 'Sr.' -> 'senior', 'Dev' -> 'developer').
        - Retains meaningful role distinctions including seniority levels (Senior, Junior)
          to prevent false duplicate merging of different positions in Stage 3.

    Args:
        raw_title: Reported job title string.

    Returns:
        tuple[str, str]: (cleaned_title, normalized_title)

    Raises:
        ValueError: If raw_title is empty or contains only whitespace.
    """
    if not isinstance(raw_title, str):
        raise TypeError(f"Job title must be a string, got {type(raw_title).__name__}")

    cleaned = re.sub(r"\s+", " ", raw_title).strip()
    if not cleaned:
        raise ValueError("Job title cannot be empty or whitespace only")

    # 1. Strip recruitment lead-in prefixes
    text = _PREFIX_PATTERN.sub("", cleaned).strip()

    # 2. Strip bracketed / parenthetical boilerplate tags
    text = _TAG_PATTERN.sub(" ", text).strip()

    # 3. Lowercase
    text = text.lower()

    # 4. Protect specific technical terms before punctuation split (e.g., c++, c#)
    text = re.sub(r"\bc\+\+(?=[^\w+]|$)", "cpp", text)
    text = re.sub(r"\bc#\b", "csharp", text)

    # 5. Replace punctuation / separators with whitespace
    text = re.sub(r"[/\\|\-_,;:+&]", " ", text)

    # 6. Tokenize words
    tokens = [t for t in re.split(r"\s+", text) if t]

    if not tokens:
        # Fallback to cleaned lowercase if everything was stripped
        fallback = re.sub(r"\s+", " ", cleaned.lower()).strip()
        return cleaned, fallback

    # 7. Expand abbreviations (e.g., sr. -> senior, dev -> developer)
    expanded = [_ABBREVIATIONS.get(t, t) for t in tokens]

    normalized = " ".join(expanded).strip()
    if not normalized:
        normalized = cleaned.lower()

    return cleaned, normalized
