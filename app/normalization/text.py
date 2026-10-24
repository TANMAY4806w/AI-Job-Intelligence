"""Text and HTML sanitization for job descriptions and bullet points.

Boundary: Transforms raw unstructured text or HTML responses into clean,
human-readable plain text without losing meaningful job requirements.
"""

import html
import re
from typing import Any

# Pattern matching scripts, styles, and noscript blocks with contents
_SCRIPT_STYLE_PATTERN = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)

# Pattern matching HTML comments
_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)

# Block elements that introduce line breaks
_BREAK_PATTERN = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LIST_ITEM_PATTERN = re.compile(r"<li[^>]*>", re.IGNORECASE)
_BLOCK_CLOSE_PATTERN = re.compile(
    r"</(?:p|div|h[1-6]|tr|ul|ol|table|blockquote)>",
    re.IGNORECASE,
)

# Generic tag stripper
_TAG_PATTERN = re.compile(r"<[^>]+>")

# Bullet point markers for structured lists
_BULLET_PREFIX_PATTERN = re.compile(r"^(?:[•\-\*▪▫·•]|(?:\d+[\.\)]))\s*")


def clean_text(raw_text: str | None) -> str:
    """Clean plain-text string, standardizing whitespace and line breaks."""
    if not raw_text:
        return ""

    # Normalize line breaks and tabs
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ").replace("\t", " ")

    # Collapse excessive spaces within lines
    lines = [re.sub(r"[ ]{2,}", " ", line).strip() for line in text.split("\n")]

    # Collapse 3+ consecutive empty lines to 1
    cleaned_lines: list[str] = []
    empty_count = 0
    for line in lines:
        if not line:
            empty_count += 1
            if empty_count <= 1:
                cleaned_lines.append("")
        else:
            empty_count = 0
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def clean_html_to_text(html_text: str | None) -> str:
    """Convert raw HTML job description into formatted, human-readable plain text."""
    if not html_text:
        return ""

    text = html_text

    # 1. Remove scripts, styles, and noscript blocks completely
    text = _SCRIPT_STYLE_PATTERN.sub("", text)

    # 2. Remove HTML comments
    text = _COMMENT_PATTERN.sub("", text)

    # 3. Format breaks and list items
    text = _BREAK_PATTERN.sub("\n", text)
    text = _LIST_ITEM_PATTERN.sub("\n• ", text)
    text = _BLOCK_CLOSE_PATTERN.sub("\n\n", text)

    # 4. Remove all remaining tags
    text = _TAG_PATTERN.sub(" ", text)

    # 5. Decode HTML entities (&amp;, &nbsp;, &lt;, etc.)
    text = html.unescape(text)

    # 6. Normalize whitespace
    return clean_text(text)


def clean_bullet_points(items: Any) -> list[str] | None:
    """Clean structured list of bullet points (responsibilities or requirements).

    Args:
        items: List of strings, single multiline string, or None.

    Returns:
        list[str] | None: Cleaned list of duty/requirement items, or None if empty.
    """
    if items is None:
        return None

    extracted: list[str] = []

    if isinstance(items, (list, tuple, set)):
        for item in items:
            if isinstance(item, str):
                cleaned = clean_html_to_text(item) if "<" in item else clean_text(item)
                cleaned = _BULLET_PREFIX_PATTERN.sub("", cleaned).strip()
                if cleaned:
                    extracted.append(cleaned)
    elif isinstance(items, str):
        cleaned_str = clean_html_to_text(items) if "<" in items else clean_text(items)
        for line in cleaned_str.split("\n"):
            line_cleaned = _BULLET_PREFIX_PATTERN.sub("", line).strip()
            if line_cleaned:
                extracted.append(line_cleaned)

    return extracted if extracted else None
