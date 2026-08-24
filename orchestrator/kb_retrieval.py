"""
Knowledge Base retrieval — queries Bedrock KB for similar past fixes.

Called by Step Functions before the Resolution Agent to provide context
about how similar issues were resolved in the past.

Environment variables:
  KB_ID               — Bedrock Knowledge Base ID (required for real queries)
  KB_RESULTS_COUNT    — Number of results to retrieve (default: 3)
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

KB_ID = os.getenv("KB_ID", "")
KB_RESULTS_COUNT = int(os.getenv("KB_RESULTS_COUNT", "3"))


def kb_retrieval_handler(event, context):
    """
    Input:  full pipeline state (uses rca_result.rca_summary as the query)
    Output: {kb_context: str, sources: list, query: str}

    If KB_ID is not configured, returns a placeholder indicating no KB is set up.
    """
    rca_result = event.get("rca_result") or {}
    rca_summary = rca_result.get("rca_summary", "")
    issue_key = (event.get("classification") or {}).get("issue_key", "unknown")

    if not rca_summary:
        logger.info(f"No RCA summary for {issue_key} — skipping KB lookup")
        return {
            "kb_context": "No RCA summary available for knowledge base query.",
            "sources": [],
            "query": "",
        }

    if not KB_ID:
        logger.info(f"KB_ID not configured — returning empty context for {issue_key}")
        return {
            "kb_context": "Knowledge base not configured. No similar past fixes available.",
            "sources": [],
            "query": rca_summary,
        }

    return _query_bedrock_kb(rca_summary, issue_key)


def _query_bedrock_kb(query: str, issue_key: str) -> dict:
    """Query Bedrock Knowledge Base and format results as context for the agent."""
    import boto3

    client = boto3.client("bedrock-agent-runtime")

    try:
        response = client.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": KB_RESULTS_COUNT,
                }
            },
        )
    except Exception as e:
        logger.error(f"KB query failed for {issue_key}: {e}")
        return {
            "kb_context": f"Knowledge base query failed: {str(e)}",
            "sources": [],
            "query": query,
        }

    results = response.get("retrievalResults", [])

    if not results:
        logger.info(f"No KB results for {issue_key}")
        return {
            "kb_context": "No similar past fixes found in knowledge base.",
            "sources": [],
            "query": query,
        }

    # Format results as readable context for Claude Code
    context_parts = []
    sources = []

    for i, result in enumerate(results, 1):
        content = result.get("content", {}).get("text", "")
        score = result.get("score", 0)
        location = result.get("location", {})
        source_uri = location.get("s3Location", {}).get("uri", "unknown")

        context_parts.append(f"### Similar Fix #{i} (relevance: {score:.2f})\n\n{content}")
        sources.append({
            "uri": source_uri,
            "score": score,
            "excerpt_length": len(content),
        })

    kb_context = (
        "The following similar past fixes were found in the knowledge base:\n\n"
        + "\n\n---\n\n".join(context_parts)
    )

    logger.info(f"KB returned {len(results)} results for {issue_key} (top score: {sources[0]['score']:.2f})")

    return {
        "kb_context": kb_context,
        "sources": sources,
        "query": query,
    }
