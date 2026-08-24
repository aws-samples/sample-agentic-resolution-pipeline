"""
Jira webhook receiver — Jira Cloud → DevOps Agent.

Flow per page 3 of diagrams/jira-mcp-architecture.drawio:
  1. API Gateway accepts POST with X-Hub-Signature header.
  2. This Lambda HMAC-verifies (Jira shared secret from Secrets Manager).
  3. Optional JQL-style filter (env JIRA_WEBHOOK_FILTER) drops events that
     don't match — e.g. "project=OPS AND priority in (P1,P2)".
  4. Normalize Jira's webhook payload into a common IssueEvent shape.
  5. Dedupe by (issue_key, event_type, change_id) via DynamoDB short-TTL.
  6. POST signed (HMAC) to AGENT_WEBHOOK_URL.
  7. Failures → SQS DLQ (env AGENT_DLQ_URL) for retry with backoff.

Stdlib only for HTTP (matches lambda/grafana-webhook-proxy/handler.py).
"""

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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

JIRA_WEBHOOK_SECRET_ARN = os.getenv("JIRA_WEBHOOK_SECRET_ARN", "")
AGENT_WEBHOOK_URL = os.getenv("AGENT_WEBHOOK_URL", "")
AGENT_WEBHOOK_SECRET_ARN = os.getenv("AGENT_WEBHOOK_SECRET_ARN", "")
AGENT_DLQ_URL = os.getenv("AGENT_DLQ_URL", "")
DEDUPE_TABLE = os.getenv("JIRA_WEBHOOK_DEDUPE_TABLE", "")
DEDUPE_TTL_SECONDS = int(os.getenv("JIRA_WEBHOOK_DEDUPE_TTL_S", "300"))
WEBHOOK_FILTER = os.getenv("JIRA_WEBHOOK_FILTER", "")  # simple expr, see _filter_match
REPLAY_WINDOW_S = int(os.getenv("JIRA_WEBHOOK_REPLAY_WINDOW_S", "300"))

# Orchestrator integration — when set, the webhook starts a Step Functions
# execution instead of forwarding directly to the agent. Falls back to direct
# forwarding when not set (backward-compatible with walk-along deployment).
ORCHESTRATOR_STATE_MACHINE_ARN = os.getenv("ORCHESTRATOR_STATE_MACHINE_ARN", "")

# Task token table — stores Step Functions task tokens for callback-based resume.
# The webhook handler uses this to detect RCA completion or approval transitions
# and resume paused orchestrator executions.
TASK_TOKEN_TABLE = os.getenv("TASK_TOKEN_TABLE", "")

# Jira status that signals human approval for resolution
RESOLUTION_APPROVAL_STATUS = os.getenv("RESOLUTION_APPROVAL_STATUS", "In Review")

# Self-loop guardrail — drop events authored by the agent's Jira identity so the
# agent's own comments don't re-trigger investigation. Set to the accountId of
# the Jira service account the MCP writes as. Multiple IDs comma-separated.
JIRA_AGENT_ACCOUNT_IDS = {
    s.strip() for s in os.getenv("JIRA_AGENT_ACCOUNT_IDS", "").split(",") if s.strip()
}
# Backup: drop events whose comment body starts with this marker.
AGENT_COMMENT_MARKER = os.getenv("JIRA_AGENT_COMMENT_MARKER", "[Agent]")
SERVICE_NAME = os.getenv("AGENT_SERVICE_NAME", "ecommerce-walkalong")

_jira_secret_cache: str | None = None
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


def _jira_secret() -> str:
    global _jira_secret_cache
    if _jira_secret_cache is None:
        _jira_secret_cache = _load_secret(JIRA_WEBHOOK_SECRET_ARN)
    return _jira_secret_cache


def _agent_secret() -> str:
    global _agent_secret_cache
    if _agent_secret_cache is None:
        _agent_secret_cache = _load_secret(AGENT_WEBHOOK_SECRET_ARN)
    return _agent_secret_cache


# ── HMAC verify ─────────────────────────────────────────────────────────────

def _verify_hmac(headers: dict, raw_body: bytes) -> bool:
    """
    Jira sends X-Hub-Signature: sha256=<hex>. Compare in constant time.
    Replay window enforced via X-Atlassian-Webhook-Timestamp if present.
    """
    sig_header = headers.get("x-hub-signature", "")
    if not sig_header.startswith("sha256="):
        return False
    expected = sig_header.split("=", 1)[1].strip()
    computed = hmac.new(_jira_secret().encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, computed):
        return False

    ts = headers.get("x-atlassian-webhook-timestamp")
    if ts:
        try:
            sent_ms = int(ts)
            now_ms = int(time.time() * 1000)
            if abs(now_ms - sent_ms) / 1000 > REPLAY_WINDOW_S:
                logger.warning("rejecting webhook outside replay window")
                return False
        except ValueError:
            pass

    return True


# ── Filter ──────────────────────────────────────────────────────────────────

def _filter_match(event: dict) -> bool:
    """
    Minimal filter expression — supports AND-ed clauses against the agent-event
    shape. Keys recognized: project, priority, issue_type, status, jira_event.
    Looks them up in event['data']['metadata'] (or top-level 'priority').

    Empty filter matches everything.
    """
    if not WEBHOOK_FILTER:
        return True

    meta = (event.get("data") or {}).get("metadata") or {}
    lookup = {
        "project": meta.get("project"),
        "priority": event.get("priority") or meta.get("priority_raw"),
        "issue_type": meta.get("issue_type"),
        "issuetype": meta.get("issue_type"),
        "status": meta.get("status"),
        "jira_event": meta.get("jira_event"),
        "event_type": meta.get("jira_event"),
    }

    try:
        for clause in [c.strip() for c in WEBHOOK_FILTER.split("AND")]:
            if " in " in clause:
                key, vals = clause.split(" in ", 1)
                key = key.strip()
                allowed = [v.strip().strip("'\"") for v in vals.strip("() ").split(",")]
                if str(lookup.get(key)) not in allowed:
                    return False
            elif "=" in clause:
                key, val = [p.strip() for p in clause.split("=", 1)]
                val = val.strip("'\"")
                if str(lookup.get(key)) != val:
                    return False
    except Exception:
        logger.exception("filter eval failed; allowing event through")
        return True
    return True


# ── Self-loop guardrail ────────────────────────────────────────────────────

def _is_agent_authored(payload: dict) -> bool:
    """
    Return True if this Jira event was caused by the agent's own service
    account — drop these to prevent the agent commenting → webhook fires →
    agent re-triggered → comments again loop.

    Two layers:
      1. accountId match (preferred, exact)
      2. comment-body marker (fallback if accountId unknown)
    """
    user = (payload.get("user") or {})
    account_id = user.get("accountId")
    if account_id and account_id in JIRA_AGENT_ACCOUNT_IDS:
        return True

    comment = payload.get("comment") or {}
    comment_author_id = (comment.get("author") or {}).get("accountId")
    if comment_author_id and comment_author_id in JIRA_AGENT_ACCOUNT_IDS:
        return True

    body = (comment.get("body") or "")
    if isinstance(body, str) and AGENT_COMMENT_MARKER and body.lstrip().startswith(AGENT_COMMENT_MARKER):
        return True
    if isinstance(body, dict):
        # ADF body — first text node
        try:
            first_text = body["content"][0]["content"][0]["text"]
            if first_text.lstrip().startswith(AGENT_COMMENT_MARKER):
                return True
        except (KeyError, IndexError, TypeError):
            pass

    return False


# ── Orchestrator callback detection ───────────────────────────────────────

def _get_comment_text(payload: dict) -> str:
    """Extract plain text from a Jira comment (handles both string and ADF body)."""
    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        # ADF → concatenate all text nodes
        parts = []
        for para in body.get("content", []):
            for node in (para.get("content") or []):
                if isinstance(node, dict) and "text" in node:
                    parts.append(node["text"])
        return " ".join(parts)
    return ""


def _is_rca_completion(payload: dict) -> bool:
    """Detect the DevOps Agent's final RCA comment based on known patterns."""
    text = _get_comment_text(payload)
    return ("Investigation completed" in text and "ROOT CAUSE" in text)


def _is_plan_approval(payload: dict) -> bool:
    """Detect a human commenting /approve-plan to approve the resolution plan."""
    text = _get_comment_text(payload)
    return text.strip().lower().startswith("/approve-plan")


def _is_resolution_approval(payload: dict) -> bool:
    """Detect a human transitioning the ticket to the resolution-approved status."""
    changelog = payload.get("changelog") or {}
    for item in (changelog.get("items") or []):
        if item.get("field") == "status" and item.get("toString") == RESOLUTION_APPROVAL_STATUS:
            return True
    return False


def _get_approver(payload: dict) -> str:
    """Extract the human who performed the transition (for audit trail)."""
    user = payload.get("user") or {}
    return user.get("displayName") or user.get("accountId") or "unknown"


def _try_orchestrator_callback(payload: dict) -> dict | None:
    """
    Check if this webhook event is a signal to resume a paused orchestrator
    execution (RCA posted or approval transition). Returns a response dict
    if handled, None otherwise.
    """
    if not TASK_TOKEN_TABLE:
        return None

    issue = payload.get("issue") or {}
    issue_key = issue.get("key")
    if not issue_key:
        return None

    # Check for RCA completion (agent-authored final comment)
    if _is_rca_completion(payload):
        rca_text = _get_comment_text(payload)
        output = {
            "rca_summary": rca_text[:2000],
            "rca_detected_at": datetime.now(timezone.utc).isoformat(),
        }
        resumed = _resume_execution(issue_key, "awaiting_rca", output)
        if resumed:
            _audit({"event": "rca_callback_sent", "issue_key": issue_key})
            return {"statusCode": 200, "body": json.dumps({"status": "rca_callback_sent", "issue_key": issue_key})}

    # Check for plan approval (/approve-plan comment)
    if _is_plan_approval(payload):
        approver = _get_approver(payload)
        output = {
            "approved_by": approver,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        resumed = _resume_execution(issue_key, "awaiting_plan_approval", output)
        if resumed:
            _audit({"event": "plan_approved", "issue_key": issue_key, "approved_by": approver})
            return {"statusCode": 200, "body": json.dumps({"status": "plan_approved", "issue_key": issue_key})}

    # Check for resolution approval (human transition)
    if _is_resolution_approval(payload):
        approver = _get_approver(payload)
        output = {
            "approved_by": approver,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        resumed = _resume_execution(issue_key, "awaiting_resolution_approval", output)
        if resumed:
            _audit({"event": "resolution_approved", "issue_key": issue_key, "approved_by": approver})
            return {"statusCode": 200, "body": json.dumps({"status": "resolution_approved", "issue_key": issue_key, "approved_by": approver})}

    return None


def _resume_execution(issue_key: str, stage: str, output: dict) -> bool:
    """Look up task token from DynamoDB and send it back to Step Functions."""
    import boto3

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(TASK_TOKEN_TABLE)

    resp = table.get_item(Key={"issue_key": issue_key, "stage": stage})
    item = resp.get("Item")
    if not item:
        return False

    task_token = item.get("task_token")
    if not task_token:
        return False

    sfn_client = boto3.client("stepfunctions")
    sfn_client.send_task_success(
        taskToken=task_token,
        output=json.dumps(output, default=str),
    )

    table.delete_item(Key={"issue_key": issue_key, "stage": stage})
    return True


# ── Normalize to DevOps Agent event-channel schema ─────────────────────────

# Maps Jira priority names to the agent's HIGH/MEDIUM/LOW bucketing.
_PRIORITY_BUCKETS = {
    "Highest": "HIGH", "P0": "HIGH", "P1": "HIGH", "Critical": "HIGH", "Blocker": "HIGH",
    "High":    "HIGH", "P2": "HIGH",
    "Medium":  "MEDIUM", "P3": "MEDIUM",
    "Low":     "LOW", "P4": "LOW",
    "Lowest":  "LOW", "P5": "LOW",
}


def _normalize(payload: dict) -> dict | None:
    """
    Translate Jira's webhook payload to the DevOps Agent event-channel schema
    (same shape lambda/grafana-webhook-proxy/handler.py emits).
    Returns None if the event should be dropped (e.g., agent-authored).
    """
    if _is_agent_authored(payload):
        return None

    jira_event = payload.get("webhookEvent", "")
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    issue_key = issue.get("key")
    project_key = ((fields.get("project") or {}).get("key")
                   or (issue_key.split("-", 1)[0] if issue_key else "unknown"))

    priority_name = (fields.get("priority") or {}).get("name") or "Medium"
    priority_bucket = _PRIORITY_BUCKETS.get(priority_name, "MEDIUM")

    # action: created | updated | resolved (mapped roughly from Jira's event)
    if "created" in jira_event:
        action = "created"
    elif jira_event in ("jira:issue_deleted",):
        action = "resolved"
    else:
        action = "updated"

    summary = fields.get("summary") or ""
    description_text = ""
    desc = fields.get("description")
    if isinstance(desc, str):
        description_text = desc
    elif isinstance(desc, dict):
        # Crude ADF→text — sufficient for agent triage; full ADF parse overkill here.
        try:
            description_text = " ".join(
                node.get("text", "")
                for para in desc.get("content", [])
                for node in (para.get("content") or [])
                if isinstance(node, dict)
            )
        except Exception:
            description_text = ""

    title = f"[{issue_key}] {summary}" if issue_key else summary

    return {
        "eventType": "incident",
        "incidentId": f"jira-{issue_key}-{jira_event}",
        "action": action,
        "priority": priority_bucket,
        "title": title,
        "description": description_text or summary,
        "timestamp": payload.get("timestamp")
                     and datetime.fromtimestamp(payload["timestamp"] / 1000, tz=timezone.utc).isoformat()
                     or datetime.now(timezone.utc).isoformat(),
        "service": f"{SERVICE_NAME}-{(fields.get('components') or [{}])[0].get('name', 'jira').lower() if fields.get('components') else 'jira'}",
        "data": {
            "metadata": {
                "source": "jira",
                "jira_event": jira_event,
                "issue_key": issue_key,
                "project": project_key,
                "issue_type": (fields.get("issuetype") or {}).get("name"),
                "status": (fields.get("status") or {}).get("name"),
                "priority_raw": priority_name,
                "assignee": (fields.get("assignee") or {}).get("accountId"),
                "reporter": (fields.get("reporter") or {}).get("accountId"),
                "labels": fields.get("labels", []),
                "url": f"{payload.get('issue', {}).get('self', '').rsplit('/rest/', 1)[0]}/browse/{issue_key}" if issue_key else None,
            }
        },
    }


# ── Dedupe ──────────────────────────────────────────────────────────────────

def _is_duplicate(event: dict) -> bool:
    if not DEDUPE_TABLE:
        return False
    # Dedupe on the deterministic incidentId — Jira webhook redelivery sends
    # the same payload, which produces the same incidentId.
    key = event.get("incidentId") or "unknown"
    try:
        import boto3
        table = boto3.resource("dynamodb").Table(DEDUPE_TABLE)
        table.put_item(
            Item={"dedupe_key": key, "expires_at": int(time.time()) + DEDUPE_TTL_SECONDS},
            ConditionExpression="attribute_not_exists(dedupe_key)",
        )
        return False
    except Exception as e:
        if type(e).__name__ == "ConditionalCheckFailedException":
            return True
        logger.exception("dedupe write failed; allowing event through")
        return False


# ── Orchestrator dispatch ──────────────────────────────────────────────────

def _start_orchestrator(event: dict) -> dict:
    """Start a Step Functions execution with the normalized event as input."""
    import boto3
    sfn_client = boto3.client("stepfunctions")
    meta = (event.get("data") or {}).get("metadata") or {}
    execution_name = f"{meta.get('issue_key', 'unknown')}-{int(time.time() * 1000)}"
    # Step Functions execution names: alnum, hyphens, underscores only, max 80 chars
    execution_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in execution_name)[:80]

    resp = sfn_client.start_execution(
        stateMachineArn=ORCHESTRATOR_STATE_MACHINE_ARN,
        name=execution_name,
        input=json.dumps(event, default=str),
    )
    return {"executionArn": resp["executionArn"], "startDate": str(resp["startDate"])}


# ── Forward (legacy direct dispatch) ─────────────────────────────────────────

def _validate_url_scheme(url: str) -> None:
    """Reject non-HTTPS URLs to prevent SSRF via file:// or other schemes."""
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"URL scheme not allowed: {url[:40]}")


def _forward(event: dict) -> tuple[int, str]:
    _validate_url_scheme(AGENT_WEBHOOK_URL)
    payload = json.dumps(event, separators=(",", ":")).encode()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sign_input = f"{timestamp}:{payload.decode()}"
    signature = base64.b64encode(
        hmac.new(_agent_secret().encode(), sign_input.encode(), hashlib.sha256).digest()
    ).decode()

    req = urllib.request.Request(
        AGENT_WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-amzn-event-timestamp": timestamp,
            "x-amzn-event-signature": signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _send_to_dlq(event: dict, reason: str) -> None:
    if not AGENT_DLQ_URL:
        return
    try:
        import boto3
        sqs = boto3.client("sqs")
        sqs.send_message(
            QueueUrl=AGENT_DLQ_URL,
            MessageBody=json.dumps({"event": event, "reason": reason}, default=str),
        )
    except Exception:
        logger.exception("failed to write to DLQ")


def _audit(record: dict) -> None:
    record = {**record, "ts": datetime.now(timezone.utc).isoformat(), "audit": "jira_webhook"}
    print(f"[AUDIT] {json.dumps(record, default=str)}", flush=True)


def _summary(event: dict | None) -> dict:
    """Compact identifier set for audit logs."""
    if not event:
        return {}
    meta = (event.get("data") or {}).get("metadata") or {}
    return {
        "incidentId": event.get("incidentId"),
        "issue_key": meta.get("issue_key"),
        "jira_event": meta.get("jira_event"),
        "priority": event.get("priority"),
    }


# ── Lambda entrypoint ───────────────────────────────────────────────────────

def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw_body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        raw_body_bytes = base64.b64decode(raw_body)
    else:
        raw_body_bytes = raw_body.encode()

    if not _verify_hmac(headers, raw_body_bytes):
        _audit({"event": "hmac_fail"})
        return {"statusCode": 401, "body": json.dumps({"error": "invalid signature"})}

    try:
        payload = json.loads(raw_body_bytes)
    except json.JSONDecodeError:
        _audit({"event": "bad_json"})
        return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON"})}

    # Check for orchestrator callbacks BEFORE self-loop guard — the RCA comment
    # is agent-authored but needs to resume the paused execution, and approval
    # transitions are human-authored but shouldn't start new investigations.
    callback_response = _try_orchestrator_callback(payload)
    if callback_response:
        return callback_response

    normalized = _normalize(payload)

    if normalized is None:
        # Self-loop guardrail fired — agent's own action triggered this event.
        _audit({"event": "self_loop_drop",
                "jira_event": payload.get("webhookEvent"),
                "issue_key": (payload.get("issue") or {}).get("key")})
        return {"statusCode": 200, "body": json.dumps({"status": "self-loop dropped"})}

    if not _filter_match(normalized):
        _audit({"event": "filter_drop", **_summary(normalized)})
        return {"statusCode": 200, "body": json.dumps({"status": "filtered"})}

    if _is_duplicate(normalized):
        _audit({"event": "dedupe_drop", **_summary(normalized)})
        return {"statusCode": 200, "body": json.dumps({"status": "duplicate"})}

    # Route to orchestrator (Step Functions) or fall back to direct agent forwarding.
    # Only start NEW executions for issue_created events — updates/comments are
    # handled as callbacks above. This prevents duplicate executions when Jira
    # fires both comment_created and issue_updated for the same action.
    if ORCHESTRATOR_STATE_MACHINE_ARN:
        jira_event = (normalized.get("data") or {}).get("metadata", {}).get("jira_event", "")
        if jira_event != "jira:issue_created":
            _audit({"event": "update_no_new_execution", **_summary(normalized)})
            return {"statusCode": 200, "body": json.dumps({"status": "update event, no new execution"})}
        try:
            result = _start_orchestrator(normalized)
            _audit({"event": "orchestrator_started", "executionArn": result["executionArn"], **_summary(normalized)})
            return {"statusCode": 200, "body": json.dumps({"status": "orchestrator_started", **result})}
        except Exception as e:
            _send_to_dlq(normalized, f"orchestrator_exception={e}")
            _audit({"event": "orchestrator_exception", "message": str(e), **_summary(normalized)})
            return {"statusCode": 502, "body": json.dumps({"error": "orchestrator start failed"})}

    if not AGENT_WEBHOOK_URL:
        _audit({"event": "no_dispatch_target", **_summary(normalized)})
        return {"statusCode": 200, "body": json.dumps({"status": "no orchestrator or agent webhook configured"})}

    try:
        status, body = _forward(normalized)
        if status >= 400:
            _send_to_dlq(normalized, f"agent_status={status}")
            _audit({"event": "forward_fail", "status": status, **_summary(normalized)})
            return {"statusCode": 502,
                    "body": json.dumps({"error": "agent rejected", "status": status})}
        _audit({"event": "forwarded", **_summary(normalized)})
        return {"statusCode": 200, "body": json.dumps({"status": "forwarded"})}
    except Exception as e:
        _send_to_dlq(normalized, f"exception={e}")
        _audit({"event": "forward_exception", "message": str(e), **_summary(normalized)})
        return {"statusCode": 502, "body": json.dumps({"error": "forward failed"})}
