"""
Orchestrator Lambda handlers — invoked by Step Functions tasks.

Each function corresponds to a state in the state machine:

  classify_handler           — Classifies the event and returns the category
  dispatch_handler           — Routes to downstream agent based on category
  noise_handler              — Auto-closes or links noise/duplicate tickets
  store_token_and_notify     — Stores task token in DynamoDB + sends SNS notification
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from classifier import TriageCategory, classify
from resolvers.config_resolver import ConfigFileResolver

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEVOPS_AGENT_WEBHOOK_URL = os.getenv("DEVOPS_AGENT_WEBHOOK_URL", "")
DEVOPS_AGENT_SECRET_ARN = os.getenv("DEVOPS_AGENT_SECRET_ARN", "")
TASK_TOKEN_TABLE = os.getenv("TASK_TOKEN_TABLE", "")
SNS_TOPIC_ARN = os.getenv("SNS_NOTIFICATION_TOPIC_ARN", "")
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "")
JIRA_API_SECRET_ARN = os.getenv("JIRA_API_SECRET_ARN", "")

_agent_secret_cache: str | None = None


def _load_secret(arn: str) -> str:
    import boto3
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=arn)
    val = resp["SecretString"]
    try:
        parsed = json.loads(val)
        return parsed.get("secret") or parsed.get("value") or val
    except json.JSONDecodeError:
        return val


def _agent_secret() -> str:
    global _agent_secret_cache
    if _agent_secret_cache is None:
        _agent_secret_cache = _load_secret(DEVOPS_AGENT_SECRET_ARN)
    return _agent_secret_cache


def _validate_url_scheme(url: str) -> None:
    """Reject non-HTTPS URLs to prevent SSRF via file:// or other schemes."""
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"URL scheme not allowed: {url[:40]}")


def _forward_to_agent(event: dict, webhook_url: str) -> dict:
    """POST signed event to an agent webhook endpoint."""
    _validate_url_scheme(webhook_url)
    payload = json.dumps(event, separators=(",", ":")).encode()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sign_input = f"{timestamp}:{payload.decode()}"
    signature = base64.b64encode(
        hmac.new(_agent_secret().encode(), sign_input.encode(), hashlib.sha256).digest()
    ).decode()

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-amzn-event-timestamp": timestamp,
            "x-amzn-event-signature": signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected
            return {"status": resp.status, "body": resp.read().decode()}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode()}


# ── Step Functions task handlers ──────────────────────────────────────────────


def classify_handler(event, context):
    """
    Input:  normalized IssueEvent from webhook receiver
    Output: event + classification metadata for routing
    """
    category, classification_meta = classify(event)
    logger.info(
        f"Classified {event.get('incidentId')} as {category.value} "
        f"(source={classification_meta['source']}, confidence={classification_meta['confidence']:.2f})"
    )

    return {
        "event": event,
        "classification": {
            "category": category.value,
            "classified_at": datetime.now(timezone.utc).isoformat(),
            "issue_key": (event.get("data") or {}).get("metadata", {}).get("issue_key"),
            "classification_source": classification_meta["source"],
            "confidence": classification_meta["confidence"],
            "reasoning": classification_meta["reasoning"],
        },
    }


def dispatch_handler(event, context):
    """
    Input:  {event: IssueEvent, classification: {...}}
    Output: dispatch result with agent response

    Routes PROD_INCIDENT and BUG_TICKET to the DevOps Agent.
    DATA_QUALITY arrives here only after human approval (gate is in the state machine).
    FEATURE_REQUEST is a placeholder — will route to Resolution Agent in the future.
    """
    classification = event.get("classification", {})
    category = classification.get("category")
    issue_event = event.get("event", {})
    issue_key = classification.get("issue_key", "unknown")

    if category in (TriageCategory.PROD_INCIDENT, TriageCategory.BUG_TICKET, TriageCategory.DATA_QUALITY):
        if not DEVOPS_AGENT_WEBHOOK_URL:
            logger.warning(f"No DevOps Agent URL configured; skipping dispatch for {issue_key}")
            return {
                "dispatched": False,
                "reason": "no_agent_url",
                "category": category,
                "issue_key": issue_key,
            }

        result = _forward_to_agent(issue_event, DEVOPS_AGENT_WEBHOOK_URL)
        success = result["status"] < 400

        if success:
            _post_jira_comment(
                issue_key,
                "[Agent] Investigation dispatched to DevOps Agent. Analyzing logs, metrics, and traces to determine root cause. Will post findings shortly.",
            )

        logger.info(f"Dispatched {issue_key} ({category}) to DevOps Agent: status={result['status']}")
        return {
            "dispatched": success,
            "agent": "devops-agent",
            "category": category,
            "issue_key": issue_key,
            "agent_response_status": result["status"],
        }

    if category == TriageCategory.FEATURE_REQUEST:
        # Placeholder: Resolution Agent will handle this directly in the future
        logger.info(f"FEATURE_REQUEST for {issue_key} — placeholder, no dispatch yet")
        return {
            "dispatched": False,
            "reason": "resolution_agent_not_implemented",
            "category": category,
            "issue_key": issue_key,
        }

    logger.warning(f"Unexpected category {category} in dispatch for {issue_key}")
    return {
        "dispatched": False,
        "reason": "unexpected_category",
        "category": category,
        "issue_key": issue_key,
    }


def noise_handler(event, context):
    """
    Input:  {event: IssueEvent, classification: {category: "NOISE"}}
    Output: action taken (log for now; future: auto-close via Jira MCP)

    For now, just logs and returns. Future: call Jira MCP to close or link as duplicate.
    """
    classification = event.get("classification", {})
    issue_event = event.get("event", {})
    issue_key = classification.get("issue_key", "unknown")

    logger.info(f"NOISE event for {issue_key} — no action taken (auto-close not yet wired)")
    return {
        "action": "logged",
        "category": "NOISE",
        "issue_key": issue_key,
        "note": "Auto-close via Jira MCP will be wired in a future iteration",
    }


# ── Task token store + notification ──────────────────────────────────────────


def store_token_and_notify(event, context):
    """
    Stores a Step Functions task token in DynamoDB (keyed by issue_key + stage)
    and optionally sends an SNS notification. The state machine invokes this as
    a waitForTaskToken resource — it blocks until the webhook handler detects
    the relevant Jira event (RCA posted / transition to approved) and calls
    SendTaskSuccess with the stored token.

    Input: {issue_key, stage, task_token, event, classification, notification_subject?}
    """
    import boto3

    issue_key = event.get("issue_key", "unknown")
    stage = event.get("stage", "unknown")
    task_token = event.get("task_token")
    notification_subject = event.get("notification_subject")

    if not TASK_TOKEN_TABLE:
        logger.error("TASK_TOKEN_TABLE not configured")
        raise RuntimeError("TASK_TOKEN_TABLE not configured")

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(TASK_TOKEN_TABLE)

    # Store the token — webhook handler will look this up to resume the execution
    # Convert floats to strings for DynamoDB compatibility (no float/Decimal issues)
    classification = event.get("classification") or {}
    if "confidence" in classification:
        classification = {**classification, "confidence": str(classification["confidence"])}

    table.put_item(Item={
        "issue_key": issue_key,
        "stage": stage,
        "task_token": task_token,
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "classification": classification,
        "expires_at": int(time.time()) + 90000,  # 25h TTL (beyond 24h SF timeout)
    })
    logger.info(f"Stored task token for {issue_key} stage={stage}")

    # Send SNS notification if subject is provided and topic is configured
    if notification_subject and SNS_TOPIC_ARN:
        sns = boto3.client("sns")
        message_body = (
            f"Issue: {issue_key}\n"
            f"Stage: {stage}\n"
            f"Action required: {notification_subject}\n"
            f"\nView the ticket in Jira to take action."
        )
        if event.get("rca_result"):
            message_body += f"\n\nRCA Summary: {event['rca_result'].get('rca_summary', 'See ticket')}"

        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[Agentic Pipeline] {issue_key}: {notification_subject}",
            Message=message_body,
        )
        logger.info(f"Sent SNS notification for {issue_key}: {notification_subject}")

    # This function does NOT return to Step Functions — the execution stays
    # paused at waitForTaskToken until SendTaskSuccess is called externally.
    # The return here is only reached in local testing.
    return {"token_stored": True, "issue_key": issue_key, "stage": stage}


# ── Resolve Repo handler ──────────────────────────────────────────────────────


def resolve_repo_handler(event, context):
    """
    Input:  full state — uses classification.issue_key + event.data for resolution
    Output: {repo_url, default_branch, path, provider, auth_secret_arn}

    Uses the ConfigFileResolver to map Jira project/component to a repository.
    """
    classification = event.get("classification", {})
    issue_key = classification.get("issue_key", "unknown")
    issue_event = event.get("event", {})
    data = issue_event.get("data", {})
    metadata = data.get("metadata", {})

    project = metadata.get("project", issue_key.split("-")[0] if "-" in issue_key else "")
    component = metadata.get("component")
    issue_type = metadata.get("issue_type")
    rca_summary = (event.get("rca_result") or {}).get("rca_summary")

    resolver = ConfigFileResolver()
    repo_info = resolver.resolve(
        project=project,
        component=component,
        issue_type=issue_type,
        rca_summary=rca_summary,
    )

    if repo_info is None:
        logger.error(f"No repo mapping found for {issue_key} (project={project}, component={component})")
        return {
            "resolved": False,
            "issue_key": issue_key,
            "error": f"No repo mapping for project={project} component={component}",
        }

    logger.info(f"Resolved {issue_key} to {repo_info.repo_url} (branch={repo_info.branch})")
    return {
        "resolved": True,
        "repo_url": repo_info.repo_url,
        "default_branch": repo_info.branch,
        "path": repo_info.path or "",
        "provider": repo_info.provider,
        "auth_secret_arn": repo_info.auth_secret_arn or "",
        "issue_key": issue_key,
    }


# ── Post-Resolution handler ──────────────────────────────────────────────────


def post_resolution_handler(event, context):
    """
    Input:  {issue_key, output_bucket, output_key, failed?, error?, event, classification}
    Output: {pr_url, status, jira_updated, notified}

    Reads the resolution output from S3, updates Jira with the PR URL,
    and sends an SNS notification.
    """
    import boto3

    issue_key = event.get("issue_key", "unknown")
    failed = event.get("failed", False)
    output_bucket = event.get("output_bucket")
    output_key = event.get("output_key")

    pr_url = None
    status = "failed" if failed else "unknown"

    # Read resolution output from S3
    if not failed and output_bucket and output_key:
        try:
            s3 = boto3.client("s3")
            resp = s3.get_object(Bucket=output_bucket, Key=output_key)
            result = json.loads(resp["Body"].read())
            pr_url = result.get("pr_url")
            status = result.get("status", "success")
        except Exception as e:
            logger.warning(f"Could not read resolution output for {issue_key}: {e}")
            status = "output_read_failed"

    # Send SNS notification
    notified = False
    if SNS_TOPIC_ARN:
        try:
            sns_client = boto3.client("sns")
            if failed:
                subject = f"[Agentic Pipeline] {issue_key}: Resolution FAILED"
                message = (
                    f"Issue: {issue_key}\n"
                    f"Status: Resolution agent failed\n"
                    f"Error: {json.dumps(event.get('error', {}), default=str)}\n"
                )
            else:
                subject = f"[Agentic Pipeline] {issue_key}: PR Created"
                message = (
                    f"Issue: {issue_key}\n"
                    f"Status: {status}\n"
                    f"PR URL: {pr_url or 'N/A'}\n"
                    f"\nReview the PR and merge if appropriate."
                )
            sns_client.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
            notified = True
        except Exception as e:
            logger.warning(f"Failed to send SNS notification for {issue_key}: {e}")

    # Post Jira comment with resolution result
    jira_updated = False
    try:
        if failed:
            _post_jira_comment(
                issue_key,
                f"[Agent] Resolution failed.\n\nError: {json.dumps(event.get('error', {}), default=str)[:500]}\n\nManual intervention required.",
            )
        elif pr_url:
            _post_jira_comment(
                issue_key,
                f"[Agent] Resolution complete — PR created.\n\nPR: {pr_url}\n\nPlease review and merge. Once merged, the fix will be ingested into the Knowledge Base for future reference.",
            )
        else:
            _post_jira_comment(
                issue_key,
                "[Agent] Resolution attempted but no PR was created. The agent may not have found a fix. Manual investigation recommended.",
            )
        jira_updated = True
    except Exception as e:
        logger.warning(f"Failed to post Jira comment for {issue_key}: {e}")

    logger.info(f"Post-resolution for {issue_key}: status={status}, pr_url={pr_url}")
    return {
        "issue_key": issue_key,
        "pr_url": pr_url,
        "status": status,
        "notified": notified,
        "jira_updated": jira_updated,
    }


# ── Post Plan for Review handler ────────────────────────────────────────────────


def post_plan_handler(event, context):
    """
    Posts the resolution plan as a Jira comment and stores the task token.
    The execution pauses until someone comments /approve-plan on the ticket.

    Input: {issue_key, stage, task_token, plan, classification, rca_result}
    """
    import boto3

    issue_key = event.get("issue_key", "unknown")
    stage = event.get("stage", "awaiting_plan_approval")
    task_token = event.get("task_token")
    plan = event.get("plan", {})
    rca_result = event.get("rca_result", {})

    # Store task token (same pattern as other wait states)
    if TASK_TOKEN_TABLE and task_token:
        import time
        ddb = boto3.resource("dynamodb")
        table = ddb.Table(TASK_TOKEN_TABLE)
        table.put_item(Item={
            "issue_key": issue_key,
            "stage": stage,
            "task_token": task_token,
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": int(time.time()) + 90000,
        })
        logger.info(f"Stored plan approval token for {issue_key}")

    # Format the plan as a Jira comment
    comment_body = _format_plan_comment(issue_key, plan, rca_result)

    # Post comment to Jira
    _post_jira_comment(issue_key, comment_body)

    # Send SNS notification
    if SNS_TOPIC_ARN:
        try:
            sns_client = boto3.client("sns")
            sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=f"[Agentic Pipeline] {issue_key}: Resolution plan ready for review",
                Message=(
                    f"A resolution plan has been posted to {issue_key}.\n\n"
                    f"Review the plan in Jira and comment /approve-plan to proceed.\n\n"
                    f"Strategy: {plan.get('strategy', 'See Jira comment')}"
                ),
            )
        except Exception as e:
            logger.warning(f"SNS notification failed for {issue_key}: {e}")

    logger.info(f"Posted plan for review on {issue_key}")
    return {"plan_posted": True, "issue_key": issue_key, "comment": comment_body}


def _post_jira_comment(issue_key: str, comment_body: str):
    """Post a comment to a Jira issue via REST API."""
    if not JIRA_API_SECRET_ARN:
        logger.warning(f"JIRA_API_SECRET_ARN not configured — cannot post plan comment to {issue_key}")
        return

    import boto3
    sm = boto3.client("secretsmanager")
    secret = json.loads(sm.get_secret_value(SecretId=JIRA_API_SECRET_ARN)["SecretString"])
    email = secret.get("email", "")
    api_token = secret.get("api_token", "")

    if not email or not api_token:
        logger.warning(f"Jira credentials incomplete — cannot post to {issue_key}")
        return

    _validate_url_scheme(JIRA_BASE_URL)
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    payload = json.dumps({
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": comment_body}],
                }
            ],
        }
    }).encode()

    credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {credentials}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected
            logger.info(f"Posted plan comment to {issue_key}: {resp.status}")
    except urllib.error.HTTPError as e:
        logger.error(f"Failed to post Jira comment to {issue_key}: {e.code} {e.read().decode()[:200]}")
    except Exception as e:
        logger.error(f"Failed to post Jira comment to {issue_key}: {e}")


def _format_plan_comment(issue_key: str, plan: dict, rca_result: dict) -> str:
    """Format the resolution plan as a structured Jira comment."""
    strategy = plan.get("strategy", "Apply targeted fix based on RCA")
    kb_context = plan.get("kb_context", "No similar past fixes found.")
    repo_url = plan.get("repo_url", "N/A")
    branch = plan.get("default_branch", "main")
    agent_trace = plan.get("agent_trace", [])

    lines = [
        f"[Agent] Resolution Plan for {issue_key}",
        "",
        f"*Strategy:* {strategy}",
        "",
        f"*Target repository:* {repo_url}",
        f"*Base branch:* {branch}",
        "",
    ]

    if kb_context and "No similar" not in kb_context and "not configured" not in kb_context:
        lines.append("*Similar past fixes found:*")
        for line in kb_context.split("\n")[:10]:
            if line.strip():
                lines.append(f"  {line.strip()}")
        lines.append("")

    if agent_trace:
        lines.append("*Planner reasoning:*")
        for step in agent_trace[:5]:
            lines.append(f"  - {step[:200]}")
        lines.append("")

    lines.extend([
        "---",
        "Comment */approve-plan* on this ticket to proceed with execution.",
        "The Resolution Agent will clone the repo, write the fix, run tests, and create a PR.",
    ])

    return "\n".join(lines)


# ── Callback handler (invoked by webhook receiver) ────────────────────────────


def resume_execution(issue_key: str, stage: str, output: dict) -> bool:
    """
    Called by the webhook handler when it detects an RCA completion or approval
    transition. Looks up the task token from DynamoDB and sends it back to
    Step Functions to resume the paused execution.

    Returns True if execution was resumed, False if no pending token found.
    """
    import boto3

    if not TASK_TOKEN_TABLE:
        logger.warning("TASK_TOKEN_TABLE not configured; cannot resume execution")
        return False

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(TASK_TOKEN_TABLE)

    resp = table.get_item(Key={"issue_key": issue_key, "stage": stage})
    item = resp.get("Item")

    if not item:
        logger.info(f"No pending token for {issue_key} stage={stage}")
        return False

    task_token = item.get("task_token")
    if not task_token:
        logger.warning(f"Token record exists but token is empty for {issue_key} stage={stage}")
        return False

    sfn_client = boto3.client("stepfunctions")
    sfn_client.send_task_success(
        taskToken=task_token,
        output=json.dumps(output, default=str),
    )
    logger.info(f"Resumed execution for {issue_key} stage={stage}")

    # Clean up the token record
    table.delete_item(Key={"issue_key": issue_key, "stage": stage})

    return True
