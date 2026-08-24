"""
LLM-based ticket classifier — Bedrock InvokeModel fallback.

Called when the rule-based classifier returns NOISE for a ticket that has
meaningful text content. Uses Claude Haiku for fast, cheap classification
with confidence scores and reasoning traces.

Environment variables:
    LLM_CLASSIFIER_ENABLED  — "true" to enable (default: disabled)
    LLM_CLASSIFIER_MODEL_ID — Bedrock model ID (default: Haiku)
    GUARDRAIL_ID            — Bedrock Guardrail ID (optional, for input safety)
    GUARDRAIL_VERSION       — Guardrail version (optional)
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

LLM_CLASSIFIER_ENABLED = os.getenv("LLM_CLASSIFIER_ENABLED", "false").lower() == "true"
LLM_CLASSIFIER_MODEL_ID = os.getenv(
    "LLM_CLASSIFIER_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
)
GUARDRAIL_ID = os.getenv("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION", "")

CONFIDENCE_THRESHOLD = 0.7

SYSTEM_PROMPT = """You are a ticket classification system for an engineering team. Classify the ticket into exactly one category based on its content.

Categories:
- PROD_INCIDENT: Production outages, service degradation, P1/P2 incidents, alerts firing, customer-impacting issues
- BUG_TICKET: Code defects, errors, unexpected behavior in non-production or non-critical paths
- FEATURE_REQUEST: New features, enhancements, improvements, stories, epics
- DATA_QUALITY: Data corruption, data migration issues, data fixes, data inconsistencies
- NOISE: Duplicates, non-actionable items, administrative tasks, unclear/empty tickets

Respond with a JSON object only, no other text:
{"category": "CATEGORY_NAME", "confidence": 0.0-1.0, "reasoning": "one sentence explanation"}"""

VALID_CATEGORIES = {"PROD_INCIDENT", "BUG_TICKET", "FEATURE_REQUEST", "DATA_QUALITY", "NOISE"}


def llm_classify(event: dict) -> dict | None:
    """
    Classify a ticket using Bedrock InvokeModel.

    Args:
        event: Normalized issue event with data.title, data.description, etc.

    Returns:
        {category: str, confidence: float, reasoning: str} or None if disabled/failed.
    """
    if not LLM_CLASSIFIER_ENABLED:
        return None

    ticket_text = _extract_ticket_text(event)
    if not ticket_text or len(ticket_text.strip()) < 10:
        logger.debug("Ticket text too short for LLM classification")
        return None

    return _invoke_model(ticket_text)


def _extract_ticket_text(event: dict) -> str:
    """Build a text representation of the ticket for classification."""
    data = event.get("data", {})
    metadata = data.get("metadata", {})

    parts = []

    title = data.get("title") or metadata.get("summary") or ""
    if title:
        parts.append(f"Title: {title}")

    description = data.get("description") or metadata.get("description") or ""
    if description:
        parts.append(f"Description: {description[:1000]}")

    issue_type = metadata.get("issue_type", "")
    if issue_type:
        parts.append(f"Type: {issue_type}")

    priority = event.get("priority", "")
    if priority:
        parts.append(f"Priority: {priority}")

    labels = metadata.get("labels", [])
    if labels:
        parts.append(f"Labels: {', '.join(labels)}")

    return "\n".join(parts)


def _invoke_model(ticket_text: str) -> dict | None:
    """Call Bedrock InvokeModel with the ticket text."""
    import boto3

    client = boto3.client("bedrock-runtime")

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 150,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": f"Classify this ticket:\n\n{ticket_text}",
            }
        ],
    }

    invoke_params = {
        "modelId": LLM_CLASSIFIER_MODEL_ID,
        "contentType": "application/json",
        "accept": "application/json",
        "body": json.dumps(body),
    }

    if GUARDRAIL_ID and GUARDRAIL_VERSION:
        invoke_params["guardrailIdentifier"] = GUARDRAIL_ID
        invoke_params["guardrailVersion"] = GUARDRAIL_VERSION

    try:
        response = client.invoke_model(**invoke_params)
    except Exception as e:
        logger.error(f"Bedrock InvokeModel failed: {e}")
        return None

    try:
        response_body = json.loads(response["body"].read())
        content = response_body["content"][0]["text"]
        result = json.loads(content)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning(f"Failed to parse LLM response: {e}")
        return None

    category = result.get("category", "").upper()
    confidence = float(result.get("confidence", 0))
    reasoning = result.get("reasoning", "")

    if category not in VALID_CATEGORIES:
        logger.warning(f"LLM returned invalid category: {category}")
        return None

    if confidence < CONFIDENCE_THRESHOLD:
        logger.info(
            f"LLM classification below threshold: {category} ({confidence:.2f}) — {reasoning}"
        )
        return None

    logger.info(f"LLM classified as {category} (confidence={confidence:.2f}): {reasoning}")
    return {
        "category": category,
        "confidence": confidence,
        "reasoning": reasoning,
    }
