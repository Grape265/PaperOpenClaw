"""Knowledge graph: fishbone spine plus cross-paper structure."""

from __future__ import annotations

from .skeleton import (
    SPINE,
    SPINE_BY_SLUG,
    SPINE_LABELS,
    SPINE_ORDER,
    SPINE_SLUGS,
    assign_sections,
    classify_heading,
    classify_text,
    group_by_section,
    label_of,
    order_index,
    spine_payload,
)

__all__ = [
    "SPINE",
    "SPINE_SLUGS",
    "SPINE_BY_SLUG",
    "SPINE_LABELS",
    "SPINE_ORDER",
    "assign_sections",
    "classify_heading",
    "classify_text",
    "group_by_section",
    "label_of",
    "order_index",
    "spine_payload",
]
