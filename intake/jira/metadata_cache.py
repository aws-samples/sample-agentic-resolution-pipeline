"""
Read-through cache for slow-changing Jira metadata.

Backs the `get_project_metadata` and `get_transitions` tools. Same DynamoDB
shape as idempotency.py but a different table and a much shorter TTL (1h by
default). Cache misses fall through to Jira and the result is written back.

Cache failure is non-fatal — on any DDB error we just call Jira directly. The
goal is rate-limit relief, not correctness.
"""

import json
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

CACHE_TABLE = os.getenv("JIRA_METADATA_CACHE_TABLE", "")
CACHE_TTL_SECONDS = int(os.getenv("JIRA_METADATA_CACHE_TTL_SECONDS", "3600"))
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
            _table = mod.get_table(CACHE_TABLE or "jira-mcp-metadata-local")
            return _table
        if not CACHE_TABLE:
            return None
        import boto3
        _table = boto3.resource("dynamodb").Table(CACHE_TABLE)
    return _table


def get_or_load(cache_key: str, loader: Callable[[], Any]) -> Any:
    """
    Return cached value for `cache_key`. On miss, call `loader()`, cache its
    result, and return it. On any DDB error, fall back to calling `loader()`
    directly without caching.
    """
    table = _get_table()
    if table is None:
        return loader()

    try:
        resp = table.get_item(Key={"cache_key": cache_key})
        item = resp.get("Item")
        if item:
            return json.loads(item["value"])
    except Exception:
        logger.exception("metadata cache lookup failed for key=%s — falling through", cache_key)
        return loader()

    value = loader()

    try:
        table.put_item(Item={
            "cache_key": cache_key,
            "value": json.dumps(value, default=str),
            "expires_at": int(time.time()) + CACHE_TTL_SECONDS,
        })
    except Exception:
        logger.exception("metadata cache write failed for key=%s — value still returned", cache_key)

    return value


def invalidate(cache_key: str) -> None:
    """Force a refresh on next read (e.g. after policy change or manual override)."""
    table = _get_table()
    if table is None:
        return
    try:
        table.delete_item(Key={"cache_key": cache_key})
    except Exception:
        logger.exception("metadata cache invalidate failed for key=%s", cache_key)
