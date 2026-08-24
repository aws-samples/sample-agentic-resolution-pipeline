"""
Bedrock Guardrails utility — applies guardrail checks across the pipeline.

Usage:
    from guardrails import apply_input_guardrail, apply_output_guardrail

    # On classifier input (ticket text)
    result = apply_input_guardrail(ticket_text)
    if result["blocked"]:
        # Handle blocked input

    # On KB output or resolution output
    result = apply_output_guardrail(generated_text)
    clean_text = result["output"]  # PII anonymized, secrets masked

Environment variables:
    GUARDRAIL_ID      — Bedrock Guardrail ID
    GUARDRAIL_VERSION — Guardrail version number
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

GUARDRAIL_ID = os.getenv("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION", "")


def apply_input_guardrail(text: str, source: str = "INPUT") -> dict:
    """
    Apply guardrail to input text (e.g., ticket content before LLM classification).

    Returns:
        {
            "blocked": bool,
            "output": str,         # Original text if not blocked, empty if blocked
            "action": str,         # "NONE", "GUARDRAIL_INTERVENED"
            "violations": list,    # List of policy violations found
        }
    """
    return _apply_guardrail(text, source)


def apply_output_guardrail(text: str, source: str = "OUTPUT") -> dict:
    """
    Apply guardrail to output text (e.g., KB retrieval results, PR descriptions).

    Returns:
        {
            "blocked": bool,
            "output": str,         # Cleaned text (PII anonymized) or empty if blocked
            "action": str,         # "NONE", "GUARDRAIL_INTERVENED"
            "violations": list,    # List of policy violations found
        }
    """
    return _apply_guardrail(text, source)


def _apply_guardrail(text: str, source: str) -> dict:
    """Core guardrail application logic."""
    if not GUARDRAIL_ID or not GUARDRAIL_VERSION:
        logger.debug("Guardrail not configured — passing through")
        return {
            "blocked": False,
            "output": text,
            "action": "NONE",
            "violations": [],
        }

    if not text or not text.strip():
        return {
            "blocked": False,
            "output": text,
            "action": "NONE",
            "violations": [],
        }

    import boto3

    client = boto3.client("bedrock-runtime")

    try:
        response = client.apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source=source,
            content=[{"text": {"text": text}}],
        )
    except Exception as e:
        logger.error(f"Guardrail API call failed: {e}")
        return {
            "blocked": False,
            "output": text,
            "action": "ERROR",
            "violations": [str(e)],
        }

    action = response.get("action", "NONE")
    outputs = response.get("outputs", [])
    assessments = response.get("assessments", [])

    violations = _extract_violations(assessments)

    if action == "GUARDRAIL_INTERVENED":
        output_text = outputs[0]["text"] if outputs else ""
        blocked = not output_text or output_text == text

        if violations:
            logger.warning(
                f"Guardrail intervened (source={source}): "
                f"action={action}, violations={len(violations)}, "
                f"types={[v['type'] for v in violations]}"
            )

        return {
            "blocked": blocked and action == "GUARDRAIL_INTERVENED",
            "output": output_text if output_text else text,
            "action": action,
            "violations": violations,
        }

    return {
        "blocked": False,
        "output": text,
        "action": action,
        "violations": violations,
    }


def _extract_violations(assessments: list) -> list:
    """Extract violation details from guardrail assessments."""
    violations = []

    for assessment in assessments:
        # Content policy violations
        content_policy = assessment.get("contentPolicy", {})
        for filter_result in content_policy.get("filters", []):
            if filter_result.get("action") in ("BLOCKED", "ANONYMIZED"):
                violations.append({
                    "type": "content_filter",
                    "category": filter_result.get("type"),
                    "action": filter_result.get("action"),
                    "confidence": filter_result.get("confidence"),
                })

        # Topic policy violations
        topic_policy = assessment.get("topicPolicy", {})
        for topic in topic_policy.get("topics", []):
            if topic.get("action") == "BLOCKED":
                violations.append({
                    "type": "denied_topic",
                    "name": topic.get("name"),
                    "action": "BLOCKED",
                })

        # Word policy violations
        word_policy = assessment.get("wordPolicy", {})
        for word in word_policy.get("customWords", []):
            if word.get("action") in ("BLOCKED", "ANONYMIZED"):
                violations.append({
                    "type": "word_filter",
                    "match": word.get("match"),
                    "action": word.get("action"),
                })
        for word in word_policy.get("managedWordLists", []):
            if word.get("action") in ("BLOCKED", "ANONYMIZED"):
                violations.append({
                    "type": "managed_word",
                    "match": word.get("match"),
                    "action": word.get("action"),
                })

        # Sensitive information violations
        sensitive_policy = assessment.get("sensitiveInformationPolicy", {})
        for pii in sensitive_policy.get("piiEntities", []):
            if pii.get("action") in ("BLOCKED", "ANONYMIZED"):
                violations.append({
                    "type": "pii",
                    "entity_type": pii.get("type"),
                    "action": pii.get("action"),
                })
        for regex in sensitive_policy.get("regexes", []):
            if regex.get("action") in ("BLOCKED", "ANONYMIZED"):
                violations.append({
                    "type": "regex",
                    "name": regex.get("name"),
                    "action": regex.get("action"),
                })

    return violations
