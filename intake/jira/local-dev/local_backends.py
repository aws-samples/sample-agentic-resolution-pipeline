"""
In-memory replacements for AWS services so you can run the MCP server locally
without DynamoDB, Secrets Manager, or any real cloud dep.

Activated by JIRA_MCP_LOCAL_MODE=true. The production modules (idempotency.py,
metadata_cache.py, jira_client.py) check this flag and substitute these shims.

State is module-scoped — survives within a single process, gone on restart.
"""

import json
import os
import threading
import time
from typing import Any


# ── In-memory DynamoDB ──────────────────────────────────────────────────────

class _InMemoryTable:
    """Subset of boto3 dynamodb Table that supports get_item / put_item /
    delete_item with conditional writes and TTL semantics."""

    def __init__(self, name: str, pk_attr: str):
        self.name = name
        self.pk_attr = pk_attr
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _evict_expired(self):
        now = int(time.time())
        with self._lock:
            for k in list(self._items.keys()):
                expires = self._items[k].get("expires_at")
                if expires and expires < now:
                    del self._items[k]

    def get_item(self, Key: dict, **_) -> dict:
        self._evict_expired()
        pk = Key[self.pk_attr]
        with self._lock:
            item = self._items.get(pk)
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item: dict, ConditionExpression: str | None = None, **_) -> dict:
        self._evict_expired()
        pk = Item[self.pk_attr]
        with self._lock:
            if ConditionExpression and "attribute_not_exists" in ConditionExpression and pk in self._items:
                # Mimic boto3's exception class name so callers' isinstance checks via type name work.
                raise _conditional_check_failed()
            self._items[pk] = dict(Item)
        return {}

    def delete_item(self, Key: dict, **_) -> dict:
        with self._lock:
            self._items.pop(Key[self.pk_attr], None)
        return {}


def _conditional_check_failed():
    cls = type("ConditionalCheckFailedException", (Exception,), {})
    return cls("ConditionalCheckFailed")


_TABLES: dict[str, _InMemoryTable] = {}
_TABLE_PK_HINTS: dict[str, str] = {
    # Match the PKs declared in jira_stack.py.
    "idem_key": "idem_key",
    "cache_key": "cache_key",
    "dedupe_key": "dedupe_key",
}


def get_table(name: str) -> _InMemoryTable:
    """Return (and lazily create) an in-memory table. PK is inferred from the
    table NAME by convention — local-mode table names follow the pattern
    'jira-mcp-<pk>'. If the name ends with idempotency / metadata / dedupe,
    the appropriate PK is selected."""
    if name in _TABLES:
        return _TABLES[name]

    name_lower = name.lower()
    if "idem" in name_lower:
        pk = "idem_key"
    elif "dedupe" in name_lower or "webhook" in name_lower:
        pk = "dedupe_key"
    else:
        pk = "cache_key"

    _TABLES[name] = _InMemoryTable(name, pk)
    return _TABLES[name]


# ── In-memory Secrets Manager ───────────────────────────────────────────────

# Read from env vars in local mode — keeps secrets out of files on disk.
# Format: JIRA_LOCAL_SECRET__<env-key> = <value>
def get_secret(secret_id: str) -> str:
    """
    Resolve a secret by ARN-or-name. Looks up env var JIRA_LOCAL_SECRET_<NAME>,
    falling back to a sensible default for the demo.
    """
    norm = secret_id.upper().replace("-", "_").replace("/", "_").replace(":", "_")
    env_key = f"JIRA_LOCAL_SECRET_{norm}"
    val = os.getenv(env_key)
    if val is not None:
        return val

    # Defaults for local testing — DO NOT use in production.
    if "jira" in secret_id.lower() and "webhook" not in secret_id.lower():
        return json.dumps({"email": "agent@local.test", "api_token": "local-fake-token"})
    if "webhook" in secret_id.lower():
        return json.dumps({"secret": "local-webhook-secret"})
    if "agent" in secret_id.lower():
        return json.dumps({"secret": "local-agent-forward-secret"})
    return json.dumps({"value": "local-default"})


# ── Helpers used by patched modules ─────────────────────────────────────────

def is_local_mode() -> bool:
    return os.getenv("JIRA_MCP_LOCAL_MODE", "").lower() in ("1", "true", "yes")
