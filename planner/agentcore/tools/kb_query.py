"""Strands tool: Query Bedrock Knowledge Base for similar past fixes."""

from __future__ import annotations

import logging
import os

import boto3
from strands import tool

logger = logging.getLogger(__name__)

KB_ID = os.getenv("KB_ID", "")
GUARDRAIL_ID = os.getenv("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION", "1")


@tool
def query_knowledge_base(query: str, num_results: int = 3) -> dict:
    """Search the knowledge base for similar past fixes and resolution patterns.

    Use this to find how similar issues were resolved before. Focus your query on
    the error pattern, service name, or technology from the RCA.

    Args:
        query: Search query derived from the RCA. Include error patterns, service names, or technologies.
        num_results: Number of results to retrieve (1-5).

    Returns:
        dict with 'results' (list of past fixes with text, score, source) and 'result_count'.
    """
    if not KB_ID:
        return {"results": [], "result_count": 0, "note": "Knowledge base not configured"}

    client = boto3.client("bedrock-agent-runtime")
    num_results = max(1, min(num_results, 5))

    try:
        response = client.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": num_results,
                }
            },
        )
    except Exception as e:
        logger.error(f"KB query failed: {e}")
        return {"results": [], "result_count": 0, "error": str(e)}

    retrieval_results = response.get("retrievalResults", [])
    results = []

    for r in retrieval_results:
        text = r.get("content", {}).get("text", "")
        score = r.get("score", 0)
        uri = r.get("location", {}).get("s3Location", {}).get("uri", "")

        if GUARDRAIL_ID:
            text = _apply_guardrail(text)

        results.append({
            "text": text[:2000],
            "score": round(score, 3),
            "source_uri": uri,
        })

    return {"results": results, "result_count": len(results)}


def _apply_guardrail(text: str) -> str:
    """Apply output guardrail to KB retrieved content."""
    try:
        client = boto3.client("bedrock-runtime")
        response = client.apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source="OUTPUT",
            content=[{"text": {"text": text}}],
        )
        if response.get("action") == "GUARDRAIL_INTERVENED":
            outputs = response.get("outputs", [])
            return outputs[0]["text"] if outputs else text
    except Exception as e:
        logger.warning(f"Guardrail application failed: {e}")
    return text
