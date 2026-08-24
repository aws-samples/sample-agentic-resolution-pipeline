"""
Hybrid event classifier — rules first, LLM fallback for ambiguous tickets.

Takes a normalized IssueEvent and returns a triage category. Fast path uses
structured Jira fields (deterministic, free). When rules return NOISE and the
ticket has meaningful text, falls back to Bedrock InvokeModel (Claude Haiku)
for semantic classification with confidence scores.

Categories:
  PROD_INCIDENT   — incident type or high priority (P1/P2)
  BUG_TICKET      — bug type, normal priority
  FEATURE_REQUEST — story/task/feature type or feature-request label
  DATA_QUALITY    — data-fix/data-quality label
  NOISE           — duplicate, unrecognized, or low-confidence LLM result
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class TriageCategory(str, Enum):
    PROD_INCIDENT = "PROD_INCIDENT"
    BUG_TICKET = "BUG_TICKET"
    FEATURE_REQUEST = "FEATURE_REQUEST"
    DATA_QUALITY = "DATA_QUALITY"
    NOISE = "NOISE"


_INCIDENT_TYPES = {"incident", "problem", "outage"}
_BUG_TYPES = {"bug", "defect", "error"}
_FEATURE_TYPES = {"story", "task", "feature", "epic", "improvement", "new feature"}
_DATA_LABELS = {"data-fix", "data-quality", "data_fix", "data_quality"}
_FEATURE_LABELS = {"feature-request", "feature_request", "enhancement"}

_HIGH_PRIORITIES = {"HIGH"}


def classify(event: dict) -> tuple[TriageCategory, dict]:
    """
    Classify a normalized webhook event into a triage category.

    Returns:
        (category, metadata) where metadata contains:
            source: "rules" | "llm"
            confidence: float (1.0 for rules, 0-1 for LLM)
            reasoning: str (empty for rules, LLM explanation for LLM)
    """
    category = _rule_based_classify(event)

    if category != TriageCategory.NOISE:
        return category, {"source": "rules", "confidence": 1.0, "reasoning": ""}

    # Fallback to LLM for ambiguous tickets
    from llm_classifier import llm_classify

    llm_result = llm_classify(event)
    if llm_result:
        return (
            TriageCategory(llm_result["category"]),
            {
                "source": "llm",
                "confidence": llm_result["confidence"],
                "reasoning": llm_result["reasoning"],
            },
        )

    return TriageCategory.NOISE, {"source": "rules", "confidence": 1.0, "reasoning": ""}


def _rule_based_classify(event: dict) -> TriageCategory:
    """Fast deterministic classification from structured fields."""
    metadata = (event.get("data") or {}).get("metadata") or {}
    labels = {lbl.lower() for lbl in (metadata.get("labels") or [])}
    issue_type = (metadata.get("issue_type") or "").lower().strip()
    priority = (event.get("priority") or "MEDIUM").upper()

    if labels & _DATA_LABELS:
        return TriageCategory.DATA_QUALITY

    if issue_type in _INCIDENT_TYPES or priority in _HIGH_PRIORITIES:
        return TriageCategory.PROD_INCIDENT

    if issue_type in _BUG_TYPES:
        return TriageCategory.BUG_TICKET

    if issue_type in _FEATURE_TYPES or labels & _FEATURE_LABELS:
        return TriageCategory.FEATURE_REQUEST

    return TriageCategory.NOISE
