"""
Knowledge Base ingestion — writes resolution documents to S3 and triggers sync.

Called ONLY after a confirmed PR merge (not on PR creation). This prevents
polluting the KB with unmerged or reverted fixes.

Environment variables:
  KB_DATA_BUCKET    — S3 bucket for resolution documents
  KB_ID             — Bedrock Knowledge Base ID
  DATA_SOURCE_ID    — Bedrock KB data source ID (for sync)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

KB_DATA_BUCKET = os.getenv("KB_DATA_BUCKET", "")
KB_ID = os.getenv("KB_ID", "")
DATA_SOURCE_ID = os.getenv("DATA_SOURCE_ID", "")


def ingestion_handler(event, context):
    """
    Input: {
        issue_key: str,
        rca_summary: str,
        resolution_summary: str,
        pr_url: str,
        repo_url: str,
        files_changed: list[str],   (optional)
        category: str,              (e.g. PROD_INCIDENT, BUG_TICKET)
        merged_at: str,             (ISO timestamp)
        metadata: dict              (optional — service, component, etc.)
    }
    Output: {ingested: bool, s3_key: str, sync_job_id: str}
    """
    issue_key = event.get("issue_key", "unknown")
    rca_summary = event.get("rca_summary", "")
    resolution_summary = event.get("resolution_summary", "")
    pr_url = event.get("pr_url", "")

    if not KB_DATA_BUCKET:
        logger.error("KB_DATA_BUCKET not configured")
        return {"ingested": False, "error": "KB_DATA_BUCKET not configured"}

    if not resolution_summary:
        logger.warning(f"No resolution_summary for {issue_key} — skipping ingestion")
        return {"ingested": False, "error": "no resolution summary provided"}

    document = _build_document(event)
    s3_key = _write_to_s3(issue_key, document)
    sync_job_id = _trigger_sync()

    logger.info(f"Ingested resolution for {issue_key}: s3://{KB_DATA_BUCKET}/{s3_key}")
    return {
        "ingested": True,
        "s3_key": s3_key,
        "sync_job_id": sync_job_id,
        "issue_key": issue_key,
    }


def _build_document(event: dict) -> str:
    """Build a structured markdown document optimized for retrieval.

    Captures the full pipeline trace: RCA → strategy → files → resolution.
    This allows future Planner queries to find relevant past fixes by error
    pattern, service, file path, or fix approach.
    """
    issue_key = event.get("issue_key", "unknown")
    rca_summary = event.get("rca_summary", "N/A")
    strategy = event.get("strategy", "")
    kb_context = event.get("kb_context", "")
    resolution_summary = event.get("resolution_summary", "")
    pr_url = event.get("pr_url", "N/A")
    repo_url = event.get("repo_url", "N/A")
    category = event.get("category", "UNKNOWN")
    merged_at = event.get("merged_at", datetime.now(timezone.utc).isoformat())
    files_changed = event.get("files_changed", [])
    target_files = event.get("target_files", [])
    metadata = event.get("metadata", {})

    service = metadata.get("service", "")
    component = metadata.get("component", "")
    error_pattern = metadata.get("error_pattern", "")

    parts = [
        f"# Resolution: {issue_key}",
        "",
        f"**Category:** {category}",
        f"**Service:** {service}" if service else None,
        f"**Component:** {component}" if component else None,
        f"**Merged:** {merged_at}",
        f"**PR:** {pr_url}",
        f"**Repository:** {repo_url}",
        "",
        "## Root Cause",
        "",
        rca_summary,
    ]

    if strategy:
        parts.extend(["", "## Fix Strategy", "", strategy])

    if target_files or files_changed:
        all_files = target_files or files_changed
        parts.extend(["", "## Files Modified", ""])
        parts.extend([f"- `{f}`" for f in all_files[:20]])

    parts.extend(["", "## Resolution", "", resolution_summary])

    if kb_context:
        parts.extend(["", "## Prior Knowledge Used", "", kb_context])

    if error_pattern:
        parts.extend(["", "## Error Pattern", "", f"```\n{error_pattern}\n```"])

    parts.extend([
        "",
        "---",
        f"*Pipeline trace: classify → investigate → plan → resolve → merge*",
        f"*Session: {metadata.get('session_id', 'N/A')}*",
    ])

    return "\n".join(p for p in parts if p is not None)


def _write_to_s3(issue_key: str, document: str) -> str:
    """Write the resolution document to S3."""
    import boto3

    s3_client = boto3.client("s3")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    s3_key = f"resolutions/{issue_key}/{timestamp}.md"

    s3_client.put_object(
        Bucket=KB_DATA_BUCKET,
        Key=s3_key,
        Body=document.encode("utf-8"),
        ContentType="text/markdown",
        Metadata={
            "issue_key": issue_key,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    return s3_key


def _trigger_sync() -> str:
    """Start a Bedrock KB ingestion job to sync S3 → vector store."""
    import boto3

    if not KB_ID or not DATA_SOURCE_ID:
        logger.warning("KB_ID or DATA_SOURCE_ID not configured — skipping sync")
        return ""

    client = boto3.client("bedrock-agent")

    try:
        response = client.start_ingestion_job(
            knowledgeBaseId=KB_ID,
            dataSourceId=DATA_SOURCE_ID,
        )
        job_id = response.get("ingestionJob", {}).get("ingestionJobId", "")
        logger.info(f"Started KB sync job: {job_id}")
        return job_id
    except Exception as e:
        logger.error(f"Failed to start KB sync: {e}")
        return ""
