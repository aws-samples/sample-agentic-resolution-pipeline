"""
Idempotency layer — DynamoDB-backed cache of {idempotency_key → response} with 24h TTL.

Every write tool MUST call lookup() first; on miss it must call record() AFTER
the Jira call succeeds. On Jira failure, do NOT record — that lets a retry
re-attempt.

Conditional write (attribute_not_exists) prevents the race where two Lambda
containers receive the same retry simultaneously.
"""

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

IDEMPOTENCY_TABLE = os.getenv("JIRA_IDEMPOTENCY_TABLE", "")
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("JIRA_IDEMPOTENCY_TTL_SECONDS", str(24 * 3600)))
LOCAL_MODE = os.getenv("JIRA_MCP_LOCAL_MODE", "").lower() in ("1", "true", "yes")

_table = None


def _get_table():
    global _table
    if _table is None:
        if LOCAL_MODE:
            import importlib.util
            here = os.path.dirname(os.path.abspath(__file__))
            spec = importlib.util.spec_from_file_location(
                "local_backends", os.path.join(here, "local-dev", "local_backends.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _table = mod.get_table(IDEMPOTENCY_TABLE or "jira-mcp-idempotency-local")
            return _table
        if not IDEMPOTENCY_TABLE:
            raise RuntimeError("JIRA_IDEMPOTENCY_TABLE env var not set")
        import boto3
        _table = boto3.resource("dynamodb").Table(IDEMPOTENCY_TABLE)
    return _table


def lookup(idempotency_key: str) -> dict | None:
    """Return the cached response if this key has been seen, else None."""
    if not idempotency_key:
        return None
    try:
        resp = _get_table().get_item(Key={"idem_key": idempotency_key})
    except Exception:
        logger.exception("idempotency lookup failed for key=%s — proceeding without cache", idempotency_key)
        return None
    item = resp.get("Item")
    if not item:
        return None
    return json.loads(item["response"])


def record(idempotency_key: str, response: Any) -> None:
    """
    Cache the response. Conditional on attribute_not_exists so a concurrent
    write loses to the first one — both retries see the same final state.
    """
    if not idempotency_key:
        return
    expires_at = int(time.time()) + IDEMPOTENCY_TTL_SECONDS
    try:
        _get_table().put_item(
            Item={
                "idem_key": idempotency_key,
                "response": json.dumps(response, default=str),
                "expires_at": expires_at,
            },
            ConditionExpression="attribute_not_exists(idem_key)",
        )
    except Exception as e:
        # ConditionalCheckFailedException is fine — another invocation won the race.
        if type(e).__name__ == "ConditionalCheckFailedException":
            logger.info("idempotency record race for key=%s — retaining first writer", idempotency_key)
            return
        logger.exception("idempotency record failed for key=%s", idempotency_key)
