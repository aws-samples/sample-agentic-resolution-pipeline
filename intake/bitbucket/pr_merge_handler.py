"""
Bitbucket PR Merge Webhook Handler — triggers KB ingestion on merged fix PRs.

Receives Bitbucket Cloud `pullrequest:fulfilled` webhooks and:
  1. Validates the PR was merged (not declined)
  2. Checks the repo is registered in our pipeline (repo-config.yaml allowlist)
  3. Extracts the issue key from the branch name (fix/{ISSUE_KEY})
  4. Pulls the resolution output from S3
  5. Invokes the KB ingestion Lambda with full resolution context

This closes the autonomous feedback loop:
  Bug → Investigate → Plan → Fix → PR → Merge → Ingest → Smarter future fixes

Environment variables:
  RESOLUTION_OUTPUT_BUCKET — S3 bucket with resolution outputs
  KB_INGESTION_FUNCTION    — Lambda function name for KB ingestion
  REPO_ALLOWLIST           — Comma-separated repo URLs (from repo-config.yaml)
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RESOLUTION_OUTPUT_BUCKET = os.getenv("RESOLUTION_OUTPUT_BUCKET", "")
KB_INGESTION_FUNCTION = os.getenv("KB_INGESTION_FUNCTION", "agentic-pipeline-kb-ingestion")
REPO_ALLOWLIST = set(
    r.strip().rstrip("/").rstrip(".git")
    for r in os.getenv("REPO_ALLOWLIST", "").split(",")
    if r.strip()
)

BRANCH_PATTERN = re.compile(r"^fix/([\w]+-\d+)")


def lambda_handler(event, context):
    """Handle Bitbucket pullrequest:fulfilled webhook."""
    try:
        body = _parse_body(event)
        return _handle_pr_merge(body)
    except Exception as e:
        logger.exception(f"PR merge handler failed: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def _parse_body(event: dict) -> dict:
    body_str = event.get("body", "{}")
    if event.get("isBase64Encoded"):
        import base64
        body_str = base64.b64decode(body_str).decode()
    return json.loads(body_str)


def _handle_pr_merge(body: dict) -> dict:
    pr = body.get("pullrequest", {})
    repo = body.get("repository", {})

    pr_state = pr.get("state", "")
    if pr_state != "MERGED":
        logger.info(f"Ignoring PR state={pr_state} (not MERGED)")
        return _response(200, {"skipped": True, "reason": f"state={pr_state}"})

    # Extract repo URL and check allowlist
    repo_url = _normalize_repo_url(repo)
    if not _is_allowed_repo(repo_url):
        logger.info(f"Repo not in allowlist: {repo_url}")
        return _response(200, {"skipped": True, "reason": "repo_not_in_allowlist"})

    # Extract issue key from branch name
    source_branch = pr.get("source", {}).get("branch", {}).get("name", "")
    match = BRANCH_PATTERN.match(source_branch)
    if not match:
        logger.info(f"Branch '{source_branch}' doesn't match fix/{{ISSUE_KEY}} pattern")
        return _response(200, {"skipped": True, "reason": "not_a_fix_branch"})

    issue_key = match.group(1)
    pr_url = pr.get("links", {}).get("html", {}).get("href", "")
    merged_by = pr.get("closed_by", {}).get("display_name", "unknown")
    merged_at = pr.get("updated_on", "")

    logger.info(f"PR merged: {issue_key} | repo={repo_url} | branch={source_branch} | by={merged_by}")

    # Fetch resolution output from S3
    resolution_data = _get_resolution_output(issue_key)

    # Build ingestion payload — include full pipeline context for KB enrichment
    ingestion_payload = {
        "issue_key": issue_key,
        "rca_summary": resolution_data.get("rca_summary", ""),
        "strategy": resolution_data.get("strategy", ""),
        "kb_context": resolution_data.get("kb_context", ""),
        "target_files": resolution_data.get("target_files", []),
        "resolution_summary": _build_resolution_summary(pr, resolution_data),
        "pr_url": pr_url,
        "repo_url": repo_url,
        "category": resolution_data.get("category", "BUG_TICKET"),
        "merged_at": merged_at,
        "files_changed": resolution_data.get("target_files", []) or _get_changed_files(pr),
        "metadata": {
            "merged_by": merged_by,
            "branch": source_branch,
            "session_id": resolution_data.get("session_id", ""),
        },
    }

    # Invoke KB ingestion Lambda
    result = _invoke_ingestion(ingestion_payload)
    logger.info(f"Ingestion triggered for {issue_key}: {result}")

    return _response(200, {
        "ingested": True,
        "issue_key": issue_key,
        "pr_url": pr_url,
        "ingestion_result": result,
    })


def _normalize_repo_url(repo: dict) -> str:
    """Extract and normalize the repo URL from Bitbucket webhook payload."""
    links = repo.get("links", {})
    html_href = links.get("html", {}).get("href", "")
    if html_href:
        return html_href.rstrip("/")

    full_name = repo.get("full_name", "")
    if full_name:
        return f"https://bitbucket.org/{full_name}"

    return ""


def _is_allowed_repo(repo_url: str) -> bool:
    """Check if the repo is registered in our pipeline."""
    if not REPO_ALLOWLIST:
        logger.warning("REPO_ALLOWLIST is empty — allowing all repos")
        return True

    normalized = repo_url.rstrip("/").rstrip(".git")
    return normalized in REPO_ALLOWLIST


def _get_resolution_output(issue_key: str) -> dict:
    """Fetch the resolution output.json from S3 (written by the ECS worker)."""
    if not RESOLUTION_OUTPUT_BUCKET:
        logger.warning("RESOLUTION_OUTPUT_BUCKET not set — returning empty")
        return {}

    import boto3
    s3 = boto3.client("s3")
    key = f"resolutions/{issue_key}/output.json"

    try:
        resp = s3.get_object(Bucket=RESOLUTION_OUTPUT_BUCKET, Key=key)
        return json.loads(resp["Body"].read().decode())
    except s3.exceptions.NoSuchKey:
        logger.warning(f"No resolution output found: s3://{RESOLUTION_OUTPUT_BUCKET}/{key}")
        return {}
    except Exception as e:
        logger.warning(f"Failed to read resolution output: {e}")
        return {}


def _build_resolution_summary(pr: dict, resolution_data: dict) -> str:
    """Build a human-readable resolution summary for the KB document."""
    title = pr.get("title", "")
    description = pr.get("description", "")
    agent_output = resolution_data.get("agent_output_tail", "")

    parts = []
    if title:
        parts.append(f"**PR Title:** {title}")
    if description:
        parts.append(f"**Description:** {description[:500]}")
    if agent_output:
        parts.append(f"**Agent Notes:** {agent_output[:500]}")

    return "\n\n".join(parts) if parts else "Resolution applied via automated fix agent."


def _get_changed_files(pr: dict) -> list:
    """Extract changed file paths from PR if available."""
    # Bitbucket webhook payload doesn't include file list by default
    # We could call the API, but for now return empty
    return []


def _invoke_ingestion(payload: dict) -> dict:
    """Invoke the KB ingestion Lambda."""
    import boto3
    client = boto3.client("lambda")

    response = client.invoke(
        FunctionName=KB_INGESTION_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )

    return {
        "status_code": response.get("StatusCode"),
        "invocation": "async",
    }


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
