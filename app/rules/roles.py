"""Role classification, priority tiering, and hard exclusion evaluation.

Boundary: 100% deterministic keyword and normalized string matching.
Enforces frozen V1 role taxonomy from rules/JOB_FILTERS_V1.md.
"""

import re
from dataclasses import dataclass

from app.rules.schemas import (
    REJECTION_EXCLUDED_ROLE,
    REVIEW_EXCLUSION_AMBIGUOUS,
    RelevanceStatus,
    RolePriority,
)

# 2.1 P1 — Highest Priority (Core Target Roles: 14 roles)
P1_ROLES: frozenset[str] = frozenset(
    {
        "ai engineer",
        "genai engineer",
        "applied ai engineer",
        "ai software engineer",
        "ai developer",
        "genai developer",
        "agentic ai engineer",
        "agentic ai developer",
        "llm engineer",
        "llm application developer",
        "ai application engineer",
        "ai application developer",
        "rag engineer",
        "ai agent engineer",
    }
)

# 2.2 P2 — Strong Targets (Accelerators & AI-Adjacent: 7 roles)
P2_ROLES: frozenset[str] = frozenset(
    {
        "forward deployed engineer (ai/fde)",
        "forward deployed engineer ai fde",
        "forward deployed engineer",
        "ai backend engineer",
        "ai/ml software engineer",
        "ai ml software engineer",
        "python backend engineer",
        "python backend developer",
        "backend engineer",
        "applied machine learning engineer",
    }
)

# 2.3 P3 — Good Backup (Foundational Technical Roles: 9 roles)
P3_ROLES: frozenset[str] = frozenset(
    {
        "ml engineer",
        "machine learning engineer",
        "python developer",
        "software engineer",
        "software developer",
        "sde",
        "software development engineer",
        "junior software engineer",
        "ai automation engineer",
        "ai automation developer",
    }
)

# 3.1 Prohibited disciplines & titles (Hard Exclusions)
_SALESFORCE_PATTERN = re.compile(r"\bsalesforce\b", re.IGNORECASE)

_FRONTEND_ONLY_PATTERN = re.compile(
    r"\b(?:frontend|front[\s-]end|react|angular|vue|css\s+specialist|ui\s+developer|web\s+developer)\b",
    re.IGNORECASE,
)

_IT_SUPPORT_PATTERN = re.compile(
    r"\b(?:it\s+support|desktop\s+support|helpdesk|tech(?:nical)?\s+support|system\s+administrator|sysadmin)\b",
    re.IGNORECASE,
)

_QA_SDET_PATTERN = re.compile(
    r"\b(?:qa|sdet|manual\s+tester|automation\s+tester|test\s+engineer|quality\s+assurance|software\s+tester)\b",
    re.IGNORECASE,
)

_NON_TECHNICAL_PATTERN = re.compile(
    r"\b(?:sales|hr|human\s+resources|recruiter|talent\s+acquisition|marketing|operations|"
    r"account\s+executive|business\s+development|finance|accountant|legal)\b",
    re.IGNORECASE,
)

# Specific architect / management exclusions per Section 3.1
_SENIOR_MANAGEMENT_ARCHITECT_PATTERN = re.compile(
    r"\b(?:ai\s+architect|genai\s+architect|ai\s+product\s+manager|ai\s+pm|"
    r"ai\s+business\s+analyst|ai\s+ba|engineering\s+manager|director\s+of\s+engineering|"
    r"vp\s+of\s+engineering|chief\s+technology\s+officer|\bcto\b)\b",
    re.IGNORECASE,
)

# 3.2 Java-only & .NET-only tech stacks
_JAVA_DOTNET_PATTERN = re.compile(
    r"\b(?:java|core\s+java|dotnet|\.net|c#|csharp|asp\.net)\b",
    re.IGNORECASE,
)

_AI_MODERN_STACK_PATTERN = re.compile(
    r"\b(?:ai|genai|llm|rag|agent|machine\s+learning|ml|python|fastapi|langchain|llamaindex)\b",
    re.IGNORECASE,
)

# Seniority modifier words that may precede a role title
_SENIORITY_PREFIX_PATTERN = re.compile(
    r"^(?:senior|sr\.?|lead|principal|staff|junior|jr\.?|associate|intern|entry[\s-]level)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoleEvaluation:
    """Internal evaluation outcome for role priority and relevance."""

    role_priority: RolePriority
    relevance_status: RelevanceStatus
    is_excluded: bool
    is_ambiguous_tech: bool
    rejection_reason: str | None
    review_reason: str | None
    matched_role: str | None
    explanation: str


def _clean_role_title(title: str) -> str:
    """Strip punctuation and boilerplate to normalize title for matching."""
    text = title.lower().strip()
    text = re.sub(r"[()\[\]/\\|\-_,;:+&]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_seniority(cleaned: str) -> str:
    """Strip leading seniority keywords to identify the underlying technical discipline."""
    return _SENIORITY_PREFIX_PATTERN.sub("", cleaned).strip()


def evaluate_role(
    title: str,
    normalized_title: str | None = None,
    description: str = "",
) -> RoleEvaluation:
    """Evaluate role relevance, priority tier, and hard exclusions.

    Args:
        title: Raw advertised job title.
        normalized_title: Normalized title string if available.
        description: Job description text for context checks (e.g. Java/.NET nuance).

    Returns:
        RoleEvaluation containing role priority, relevance status, and exclusion outcomes.
    """
    raw_clean = _clean_role_title(title)
    norm_clean = _clean_role_title(normalized_title) if normalized_title else raw_clean
    core_raw = _strip_seniority(raw_clean)
    core_norm = _strip_seniority(norm_clean)

    # 1. Evaluate Hard Exclusions (Section 3.1)
    # Check Section 3.1 Senior Management / Architecture Prohibitions
    if _SENIOR_MANAGEMENT_ARCHITECT_PATTERN.search(
        title
    ) or _SENIOR_MANAGEMENT_ARCHITECT_PATTERN.search(normalized_title or ""):
        return RoleEvaluation(
            role_priority="NOT_RELEVANT",
            relevance_status="EXCLUDED",
            is_excluded=True,
            is_ambiguous_tech=False,
            rejection_reason=REJECTION_EXCLUDED_ROLE,
            review_reason=None,
            matched_role=None,
            explanation=f"Role matches senior architecture or management exclusion: {title}",
        )

    # Check Salesforce Developer
    if _SALESFORCE_PATTERN.search(title) or _SALESFORCE_PATTERN.search(normalized_title or ""):
        return RoleEvaluation(
            role_priority="NOT_RELEVANT",
            relevance_status="EXCLUDED",
            is_excluded=True,
            is_ambiguous_tech=False,
            rejection_reason=REJECTION_EXCLUDED_ROLE,
            review_reason=None,
            matched_role=None,
            explanation=f"Role matches Salesforce exclusion: {title}",
        )

    # Check QA / SDET / Testing
    if _QA_SDET_PATTERN.search(title) or _QA_SDET_PATTERN.search(normalized_title or ""):
        return RoleEvaluation(
            role_priority="NOT_RELEVANT",
            relevance_status="EXCLUDED",
            is_excluded=True,
            is_ambiguous_tech=False,
            rejection_reason=REJECTION_EXCLUDED_ROLE,
            review_reason=None,
            matched_role=None,
            explanation=f"Role matches QA/SDET exclusion: {title}",
        )

    # Check IT Support / Desktop Support
    if _IT_SUPPORT_PATTERN.search(title) or _IT_SUPPORT_PATTERN.search(normalized_title or ""):
        return RoleEvaluation(
            role_priority="NOT_RELEVANT",
            relevance_status="EXCLUDED",
            is_excluded=True,
            is_ambiguous_tech=False,
            rejection_reason=REJECTION_EXCLUDED_ROLE,
            review_reason=None,
            matched_role=None,
            explanation=f"Role matches IT support exclusion: {title}",
        )

    # Check Non-technical disciplines (Sales, HR, Marketing, Operations)
    if _NON_TECHNICAL_PATTERN.search(title) or _NON_TECHNICAL_PATTERN.search(
        normalized_title or ""
    ):
        return RoleEvaluation(
            role_priority="NOT_RELEVANT",
            relevance_status="EXCLUDED",
            is_excluded=True,
            is_ambiguous_tech=False,
            rejection_reason=REJECTION_EXCLUDED_ROLE,
            review_reason=None,
            matched_role=None,
            explanation=f"Role matches non-technical discipline exclusion: {title}",
        )

    # Check Generic Frontend-Only Roles
    if (
        _FRONTEND_ONLY_PATTERN.search(title)
        or _FRONTEND_ONLY_PATTERN.search(normalized_title or "")
    ) and not re.search(r"\b(?:backend|full[\s-]?stack|ai|python)\b", title, re.IGNORECASE):
        return RoleEvaluation(
            role_priority="NOT_RELEVANT",
            relevance_status="EXCLUDED",
            is_excluded=True,
            is_ambiguous_tech=False,
            rejection_reason=REJECTION_EXCLUDED_ROLE,
            review_reason=None,
            matched_role=None,
            explanation=f"Role matches generic frontend-only exclusion: {title}",
        )

    # 2. Check Java-Only / .NET-Only Nuance (Section 3.2)
    has_java_dotnet = bool(
        _JAVA_DOTNET_PATTERN.search(title) or _JAVA_DOTNET_PATTERN.search(normalized_title or "")
    )
    if has_java_dotnet:
        title_has_modern = bool(
            _AI_MODERN_STACK_PATTERN.search(title)
            or _AI_MODERN_STACK_PATTERN.search(normalized_title or "")
        )
        desc_has_modern = bool(_AI_MODERN_STACK_PATTERN.search(description))

        if title_has_modern:
            # Critical Exception: Role targeting AI/modern backend that mentions Java/.NET
            # Does not get rejected.
            pass
        elif desc_has_modern:
            # Ambiguous: Title says Java/.NET but description mentions AI/modern stack
            return RoleEvaluation(
                role_priority="NOT_RELEVANT",
                relevance_status="EXCLUDED",
                is_excluded=False,
                is_ambiguous_tech=True,
                rejection_reason=None,
                review_reason=REVIEW_EXCLUSION_AMBIGUOUS,
                matched_role=None,
                explanation=f"Role mentions Java/.NET but description mentions AI/modern stack: {title}",
            )
        else:
            # Pure Java/.NET enterprise development
            return RoleEvaluation(
                role_priority="NOT_RELEVANT",
                relevance_status="EXCLUDED",
                is_excluded=True,
                is_ambiguous_tech=False,
                rejection_reason=REJECTION_EXCLUDED_ROLE,
                review_reason=None,
                matched_role=None,
                explanation=f"Role is pure Java/.NET enterprise development: {title}",
            )

    # 3. Match Target Roles: P1, P2, P3
    # Check exact match or core role match in P1
    for p1 in P1_ROLES:
        clean_p1 = _clean_role_title(p1)
        if clean_p1 in (raw_clean, norm_clean, core_raw, core_norm):
            return RoleEvaluation(
                role_priority="P1",
                relevance_status="TARGET",
                is_excluded=False,
                is_ambiguous_tech=False,
                rejection_reason=None,
                review_reason=None,
                matched_role=p1,
                explanation=f"Matched P1 target role: {p1}",
            )

    # Check exact match or core role match in P2
    for p2 in P2_ROLES:
        clean_p2 = _clean_role_title(p2)
        if clean_p2 in (raw_clean, norm_clean, core_raw, core_norm):
            return RoleEvaluation(
                role_priority="P2",
                relevance_status="ADJACENT",
                is_excluded=False,
                is_ambiguous_tech=False,
                rejection_reason=None,
                review_reason=None,
                matched_role=p2,
                explanation=f"Matched P2 target role: {p2}",
            )

    # Check exact match or core role match in P3
    for p3 in P3_ROLES:
        clean_p3 = _clean_role_title(p3)
        if clean_p3 in (raw_clean, norm_clean, core_raw, core_norm):
            return RoleEvaluation(
                role_priority="P3",
                relevance_status="ADJACENT",
                is_excluded=False,
                is_ambiguous_tech=False,
                rejection_reason=None,
                review_reason=None,
                matched_role=p3,
                explanation=f"Matched P3 target role: {p3}",
            )

    # Also check substring / word boundary match for target roles (e.g. "Junior Software Engineer", "Python Developer - AI")
    for p1 in P1_ROLES:
        pattern = r"\b" + re.escape(_clean_role_title(p1)) + r"\b"
        if re.search(pattern, raw_clean) or re.search(pattern, norm_clean):
            return RoleEvaluation(
                role_priority="P1",
                relevance_status="TARGET",
                is_excluded=False,
                is_ambiguous_tech=False,
                rejection_reason=None,
                review_reason=None,
                matched_role=p1,
                explanation=f"Matched P1 target role: {p1}",
            )

    for p2 in P2_ROLES:
        pattern = r"\b" + re.escape(_clean_role_title(p2)) + r"\b"
        if re.search(pattern, raw_clean) or re.search(pattern, norm_clean):
            return RoleEvaluation(
                role_priority="P2",
                relevance_status="ADJACENT",
                is_excluded=False,
                is_ambiguous_tech=False,
                rejection_reason=None,
                review_reason=None,
                matched_role=p2,
                explanation=f"Matched P2 target role: {p2}",
            )

    for p3 in P3_ROLES:
        pattern = r"\b" + re.escape(_clean_role_title(p3)) + r"\b"
        if re.search(pattern, raw_clean) or re.search(pattern, norm_clean):
            return RoleEvaluation(
                role_priority="P3",
                relevance_status="ADJACENT",
                is_excluded=False,
                is_ambiguous_tech=False,
                rejection_reason=None,
                review_reason=None,
                matched_role=p3,
                explanation=f"Matched P3 target role: {p3}",
            )

    # 4. Non-matching role outside P1, P2, P3
    return RoleEvaluation(
        role_priority="NOT_RELEVANT",
        relevance_status="EXCLUDED",
        is_excluded=True,
        is_ambiguous_tech=False,
        rejection_reason=REJECTION_EXCLUDED_ROLE,
        review_reason=None,
        matched_role=None,
        explanation=f"Role '{title}' does not match any approved P1, P2, or P3 target taxonomy",
    )
