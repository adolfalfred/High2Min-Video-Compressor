"""Read and update the ADT page index used by the sign-language runtime."""

from __future__ import annotations

import re
from typing import Final

META_TAG_PATTERN: Final = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
ATTRIBUTE_PATTERN: Final = re.compile(
    r"(?P<name>[A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*(?P<quote>['\"])(?P<value>.*?)\2",
    re.DOTALL,
)


def _attributes(tag: str) -> dict[str, str]:
    return {
        match.group("name").casefold(): match.group("value")
        for match in ATTRIBUTE_PATTERN.finditer(tag)
    }


def page_section_index(source: str) -> int | None:
    """Return a non-negative ``page-section-id`` value when one is declared."""

    values: list[int] = []
    for match in META_TAG_PATTERN.finditer(source):
        attributes = _attributes(match.group(0))
        if attributes.get("name", "").casefold() != "page-section-id":
            continue
        try:
            value = int(attributes.get("content", ""))
        except ValueError:
            return None
        if value < 0:
            return None
        values.append(value)
    return values[0] if len(values) == 1 else None


def replace_page_section_index(source: str, value: int) -> str:
    """Replace one page index while preserving the surrounding HTML formatting."""

    if value < 0:
        raise ValueError("ADT page-section-id cannot be negative.")
    candidates: list[tuple[re.Match[str], re.Match[str]]] = []
    for tag_match in META_TAG_PATTERN.finditer(source):
        tag = tag_match.group(0)
        if _attributes(tag).get("name", "").casefold() != "page-section-id":
            continue
        content_match = next(
            (
                match
                for match in ATTRIBUTE_PATTERN.finditer(tag)
                if match.group("name").casefold() == "content"
            ),
            None,
        )
        if content_match is not None:
            candidates.append((tag_match, content_match))
    if len(candidates) != 1:
        raise ValueError("ADT page must declare exactly one page-section-id meta value.")
    tag_match, content_match = candidates[0]
    start = tag_match.start() + content_match.start("value")
    end = tag_match.start() + content_match.end("value")
    return source[:start] + str(value) + source[end:]
