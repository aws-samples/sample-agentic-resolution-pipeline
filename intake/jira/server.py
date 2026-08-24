"""
Jira MCP Server — production-grade Jira Cloud integration for AI agents.

Surface: 16 MCP tools (8 read, 8 write) over JSON-RPC. Same wire shape as
integrations/custom-mcp/server.py.

Production layers per write call:
  IAM SigV4 (API Gateway)  →  idempotency check (DDB)  →  policy
  →  redaction  →  circuit breaker  →  Jira REST v3  →  idempotency record
  →  audit log

Read calls skip idempotency + redaction (GETs are idempotent and have no body).
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

import jira_client as jc
import policy_engine as pol
import idempotency as idem
import metadata_cache as mcache
import redaction as red

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ── Circuit breaker ─────────────────────────────────────────────────────────
# Module-scoped; survives within a Lambda container, resets on cold start.

class CircuitBreaker:
    def __init__(self, fail_threshold: int = 5, reset_after_s: int = 60):
        self.fail_threshold = fail_threshold
        self.reset_after_s = reset_after_s
        self.failure_count = 0
        self.opened_at: float | None = None

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at > self.reset_after_s:
            self.failure_count = 0
            self.opened_at = None
            return False
        return True

    def record_success(self):
        self.failure_count = 0
        self.opened_at = None

    def record_failure(self):
        self.failure_count += 1
        if self.failure_count >= self.fail_threshold:
            self.opened_at = time.time()


_breaker = CircuitBreaker(
    fail_threshold=int(os.getenv("JIRA_CB_THRESHOLD", "5")),
    reset_after_s=int(os.getenv("JIRA_CB_RESET_S", "60")),
)


# ── Audit log ───────────────────────────────────────────────────────────────

_LOCAL_AUDIT_FILE = os.getenv("JIRA_MCP_LOCAL_AUDIT_FILE", "")


def _emit_audit(event: dict) -> None:
    """Structured audit event to CloudWatch Logs. One line per write or denial.
    In local mode, the run script also points JIRA_MCP_LOCAL_AUDIT_FILE at a
    file you can `tail -f` so audit lines aren't lost in access-log noise."""
    event = {**event, "ts": datetime.now(timezone.utc).isoformat(), "audit": "jira_mcp"}
    line = json.dumps(event, default=str)
    print(f"[AUDIT] {line}", flush=True)
    if _LOCAL_AUDIT_FILE:
        try:
            with open(_LOCAL_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── ADF helper ──────────────────────────────────────────────────────────────

def _text_to_adf(text: str) -> dict:
    """Wrap a plain string in minimal Atlassian Document Format."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# ── Tool handlers ───────────────────────────────────────────────────────────

# Each handler returns the dict that becomes the MCP `result.content[0].text`.
# Write handlers receive idempotency_key threaded by the dispatcher.

def t_search_issues_jql(args: dict, **_) -> dict:
    return jc.search_issues_jql(
        jql=args["jql"],
        fields=args.get("fields"),
        start_at=args.get("start_at", 0),
        max_results=args.get("max_results", 50),
        expand=args.get("expand"),
        next_page_token=args.get("next_page_token"),
    )


def t_get_issue(args: dict, **_) -> dict:
    return jc.get_issue(args["issue_key"], fields=args.get("fields"), expand=args.get("expand"))


def t_get_issue_comments(args: dict, **_) -> dict:
    return jc.get_issue_comments(args["issue_key"],
                                  start_at=args.get("start_at", 0),
                                  max_results=args.get("max_results", 50))


def t_get_issue_changelog(args: dict, **_) -> dict:
    return jc.get_issue_changelog(args["issue_key"],
                                   start_at=args.get("start_at", 0),
                                   max_results=args.get("max_results", 50))


def t_get_transitions(args: dict, **_) -> dict:
    issue_key = args["issue_key"]
    project_key = issue_key.split("-", 1)[0]
    # Cache by project, not issue key — workflow transitions are project/scheme-wide,
    # not per-issue. Cuts a hot-path Jira call to once per project per TTL window.
    return mcache.get_or_load(
        f"transitions:{project_key}",
        lambda: jc.get_transitions(issue_key),
    )


def t_get_project_metadata(args: dict, **_) -> dict:
    project_key = args["project_key"]
    return mcache.get_or_load(
        f"project_meta:{project_key}",
        lambda: jc.get_project_metadata(project_key),
    )


def t_get_attachments(args: dict, **_) -> dict:
    return {"attachments": jc.get_attachments(args["issue_key"])}


def t_get_user(args: dict, **_) -> dict:
    return {"users": jc.get_user(args["query"])}


def t_create_issue(args: dict, **_) -> dict:
    project_key = args["project_key"]
    fields = dict(args.get("fields") or {})

    pol.check_field_writes(project_key, fields)

    description_adf = None
    if args.get("description"):
        description_adf = red.redact_adf(_text_to_adf(args["description"]))

    summary = red.redact_text(args["summary"])

    return jc.create_issue(
        project_key=project_key,
        issue_type=args["issue_type"],
        summary=summary,
        description_adf=description_adf,
        fields=fields,
    )


def t_update_issue(args: dict, **_) -> dict:
    issue_key = args["issue_key"]
    project_key = issue_key.split("-", 1)[0]
    fields = dict(args["fields"])

    pol.check_field_writes(project_key, fields)

    if "summary" in fields and isinstance(fields["summary"], str):
        fields["summary"] = red.redact_text(fields["summary"])
    if "description" in fields and isinstance(fields["description"], str):
        fields["description"] = red.redact_adf(_text_to_adf(fields["description"]))

    jc.update_issue(issue_key, fields)
    return {"issue_key": issue_key, "status": "updated"}


def t_transition_issue(args: dict, **_) -> dict:
    issue_key = args["issue_key"]
    project_key = issue_key.split("-", 1)[0]

    transition_id = args["transition_id"]
    transition_name = args["transition_name"]
    comment_text = args.get("comment")

    issue = jc.get_issue(issue_key, fields=["priority"])
    current_priority = (issue.get("fields", {}).get("priority") or {}).get("name")

    pol.check_transition(project_key, transition_name, current_priority, bool(comment_text))

    comment_adf = red.redact_adf(_text_to_adf(comment_text)) if comment_text else None
    jc.transition_issue(issue_key, transition_id, comment_adf=comment_adf)
    return {"issue_key": issue_key, "transition": transition_name, "status": "transitioned"}


def t_add_comment(args: dict, **_) -> dict:
    issue_key = args["issue_key"]
    body_adf = red.redact_adf(_text_to_adf(args["body"]))
    resp = jc.add_comment(issue_key, body_adf)
    return {"issue_key": issue_key, "comment_id": resp.get("id"), "status": "added"}


def t_add_attachment(args: dict, **_) -> dict:
    issue_key = args["issue_key"]
    filename = red.redact_filename(args["filename"])
    content = base64.b64decode(args["content_b64"])
    content_type = args.get("content_type", "application/octet-stream")
    resp = jc.add_attachment(issue_key, filename, content, content_type)
    return {"issue_key": issue_key, "attachments": resp}


def t_link_issues(args: dict, **_) -> dict:
    jc.link_issues(args["inward_key"], args["outward_key"], args["link_type"])
    return {
        "inward_key": args["inward_key"],
        "outward_key": args["outward_key"],
        "link_type": args["link_type"],
        "status": "linked",
    }


def t_bulk_transition(args: dict, **_) -> dict:
    project_key = args["project_key"]
    issue_keys = args["issue_keys"]
    pol.check_bulk_limit(project_key, len(issue_keys))
    return {"results": jc.bulk_transition(issue_keys, args["transition_id"])}


def t_bulk_update(args: dict, **_) -> dict:
    project_key = args["project_key"]
    issue_keys = args["issue_keys"]
    fields = dict(args["fields"])
    pol.check_bulk_limit(project_key, len(issue_keys))
    pol.check_field_writes(project_key, fields)
    return {"results": jc.bulk_update(issue_keys, fields)}


# ── Tool registry ───────────────────────────────────────────────────────────

ToolDef = dict[str, Any]

TOOLS: dict[str, ToolDef] = {
    # Reads
    "search_issues_jql": {
        "fn": t_search_issues_jql, "kind": "read",
        "description": "Search Jira issues with JQL. The workhorse for finding prior incidents, duplicates, SLA sweeps. Pagination is token-based: the response carries nextPageToken; pass it as next_page_token to fetch the next page.",
        "inputSchema": {
            "type": "object", "required": ["jql"],
            "properties": {
                "jql": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "max_results": {"type": "integer", "default": 50},
                "expand": {"type": "array", "items": {"type": "string"}},
                "next_page_token": {"type": "string"},
            },
        },
    },
    "get_issue": {
        "fn": t_get_issue, "kind": "read",
        "description": "Get a single issue with optional field/expansion selection.",
        "inputSchema": {
            "type": "object", "required": ["issue_key"],
            "properties": {
                "issue_key": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "expand": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "get_issue_comments": {
        "fn": t_get_issue_comments, "kind": "read",
        "description": "Get paginated comments on an issue.",
        "inputSchema": {
            "type": "object", "required": ["issue_key"],
            "properties": {
                "issue_key": {"type": "string"},
                "start_at": {"type": "integer", "default": 0},
                "max_results": {"type": "integer", "default": 50},
            },
        },
    },
    "get_issue_changelog": {
        "fn": t_get_issue_changelog, "kind": "read",
        "description": "Get the changelog (status/field history) for an issue.",
        "inputSchema": {
            "type": "object", "required": ["issue_key"],
            "properties": {
                "issue_key": {"type": "string"},
                "start_at": {"type": "integer", "default": 0},
                "max_results": {"type": "integer", "default": 50},
            },
        },
    },
    "get_transitions": {
        "fn": t_get_transitions, "kind": "read",
        "description": "List available workflow transitions for an issue.",
        "inputSchema": {
            "type": "object", "required": ["issue_key"],
            "properties": {"issue_key": {"type": "string"}},
        },
    },
    "get_project_metadata": {
        "fn": t_get_project_metadata, "kind": "read",
        "description": "Get project schema: issue types and create-meta with field schemas.",
        "inputSchema": {
            "type": "object", "required": ["project_key"],
            "properties": {"project_key": {"type": "string"}},
        },
    },
    "get_attachments": {
        "fn": t_get_attachments, "kind": "read",
        "description": "Get attachment metadata + signed download URLs for an issue.",
        "inputSchema": {
            "type": "object", "required": ["issue_key"],
            "properties": {"issue_key": {"type": "string"}},
        },
    },
    "get_user": {
        "fn": t_get_user, "kind": "read",
        "description": "Resolve Jira users by email or display name. Returns list (may match multiple).",
        "inputSchema": {
            "type": "object", "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
    },

    # Writes
    "create_issue": {
        "fn": t_create_issue, "kind": "write",
        "description": "Create a new Jira issue. Use for RCA follow-ups, bug tickets, scheduled-monitor findings.",
        "inputSchema": {
            "type": "object", "required": ["project_key", "issue_type", "summary"],
            "properties": {
                "project_key": {"type": "string"},
                "issue_type": {"type": "string"},
                "summary": {"type": "string"},
                "description": {"type": "string"},
                "fields": {"type": "object"},
            },
        },
    },
    "update_issue": {
        "fn": t_update_issue, "kind": "write",
        "description": "Update fields on an existing issue. Subject to per-project field allowlist.",
        "inputSchema": {
            "type": "object", "required": ["issue_key", "fields"],
            "properties": {
                "issue_key": {"type": "string"},
                "fields": {"type": "object"},
            },
        },
    },
    "transition_issue": {
        "fn": t_transition_issue, "kind": "write",
        "description": "Move an issue through the workflow. Subject to transition allowlist + never-auto-close-P1 rule.",
        "inputSchema": {
            "type": "object", "required": ["issue_key", "transition_id", "transition_name"],
            "properties": {
                "issue_key": {"type": "string"},
                "transition_id": {"type": "string"},
                "transition_name": {"type": "string"},
                "comment": {"type": "string"},
            },
        },
    },
    "add_comment": {
        "fn": t_add_comment, "kind": "write",
        "description": "Add a comment to an issue. Body is plain text; secret-redacted before posting.",
        "inputSchema": {
            "type": "object", "required": ["issue_key", "body"],
            "properties": {
                "issue_key": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    },
    "add_attachment": {
        "fn": t_add_attachment, "kind": "write",
        "description": "Upload an attachment to an issue. Pass content as base64.",
        "inputSchema": {
            "type": "object", "required": ["issue_key", "filename", "content_b64"],
            "properties": {
                "issue_key": {"type": "string"},
                "filename": {"type": "string"},
                "content_b64": {"type": "string"},
                "content_type": {"type": "string"},
            },
        },
    },
    "link_issues": {
        "fn": t_link_issues, "kind": "write",
        "description": "Link two issues (Causes, Duplicates, Blocks, Relates).",
        "inputSchema": {
            "type": "object", "required": ["inward_key", "outward_key", "link_type"],
            "properties": {
                "inward_key": {"type": "string"},
                "outward_key": {"type": "string"},
                "link_type": {"type": "string"},
            },
        },
    },
    "bulk_transition": {
        "fn": t_bulk_transition, "kind": "write",
        "description": "Apply the same transition to multiple issues. Capped per project policy.",
        "inputSchema": {
            "type": "object", "required": ["project_key", "issue_keys", "transition_id"],
            "properties": {
                "project_key": {"type": "string"},
                "issue_keys": {"type": "array", "items": {"type": "string"}},
                "transition_id": {"type": "string"},
            },
        },
    },
    "bulk_update": {
        "fn": t_bulk_update, "kind": "write",
        "description": "Apply the same field update to multiple issues. Capped + allowlist enforced per project policy.",
        "inputSchema": {
            "type": "object", "required": ["project_key", "issue_keys", "fields"],
            "properties": {
                "project_key": {"type": "string"},
                "issue_keys": {"type": "array", "items": {"type": "string"}},
                "fields": {"type": "object"},
            },
        },
    },
}


# ── Dispatcher ──────────────────────────────────────────────────────────────

def _dispatch(tool_name: str, args: dict, idempotency_key: str | None, actor: str) -> dict:
    """Run a tool through the per-kind safety pipeline."""
    if tool_name not in TOOLS:
        raise ValueError(f"unknown tool: {tool_name}")

    tool = TOOLS[tool_name]
    is_write = tool["kind"] == "write"

    # Circuit breaker — applies to both reads and writes (Jira backpressure is shared).
    if _breaker.is_open():
        _emit_audit({"event": "circuit_open", "tool": tool_name, "actor": actor})
        return {"error": "DEGRADED_UPSTREAM",
                "message": "Jira upstream is failing; circuit breaker open. Retry later."}

    # Idempotency — write only.
    if is_write and idempotency_key:
        cached = idem.lookup(idempotency_key)
        if cached is not None:
            _emit_audit({"event": "idempotency_hit", "tool": tool_name,
                         "actor": actor, "idem_key": idempotency_key})
            return cached

    try:
        result = tool["fn"](args)
    except pol.PolicyDenied as e:
        _emit_audit({"event": "policy_denied", "tool": tool_name,
                     "actor": actor, "reason": e.reason, "args_keys": list(args.keys())})
        return {"error": "POLICY_DENIED", "message": e.reason}
    except jc.JiraError as e:
        _breaker.record_failure()
        _emit_audit({"event": "jira_error", "tool": tool_name, "actor": actor,
                     "code": e.status_code, "body": "[REDACTED]"})
        return {"error": "JIRA_ERROR", "code": e.status_code, "message": str(e)}
    except Exception as e:
        _breaker.record_failure()
        logger.exception("tool %s failed", tool_name)
        _emit_audit({"event": "internal_error", "tool": tool_name, "actor": actor, "message": type(e).__name__})
        return {"error": "INTERNAL_ERROR", "message": str(e)}

    _breaker.record_success()

    # Persist idempotency only on successful real writes. Dry-run results carry
    # `dry_run: true` and are NOT cached — otherwise a subsequent real-run with
    # the same idempotency key would return the dry-run shape instead of acting.
    is_dry_run_result = isinstance(result, dict) and result.get("dry_run") is True
    if is_write and idempotency_key and not is_dry_run_result:
        idem.record(idempotency_key, result)

    _emit_audit({"event": "tool_ok" if not is_dry_run_result else "dry_run",
                 "tool": tool_name, "actor": actor,
                 "idem_key": idempotency_key, "summary": _summarize(result)})
    return result


def _summarize(result: dict) -> dict:
    """Compact form of the response for the audit log — avoid logging full bodies."""
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    keys = list(result.keys())[:10]
    return {"keys": keys, "issue_key": result.get("issue_key"), "id": result.get("id")}


# ── MCP JSON-RPC ────────────────────────────────────────────────────────────

def handle_mcp_request(body: dict, *, idempotency_key: str | None = None,
                       actor: str = "unknown") -> dict | None:
    method = body.get("method", "")
    req_id = body.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "jira-mcp-server", "version": "1.0.0"},
        }}

    # Per the spec, "notifications/initialized" is a notification (no `id`),
    # and the server responds with HTTP 202 Accepted and an empty body. We
    # signal that to the transport layer by returning None.
    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        tools_list = [
            {"name": name, "description": t["description"], "inputSchema": t["inputSchema"]}
            for name, t in TOOLS.items()
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        result = _dispatch(tool_name, args, idempotency_key, actor)
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
        }}

    # Other notification namespaces — accept silently per spec.
    if method.startswith("notifications/"):
        return None

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"}}


# ── Lambda handler ──────────────────────────────────────────────────────────

def lambda_handler(event, context):
    """API Gateway → Lambda — Streamable HTTP transport (MCP 2025-03-26).

    - POST /mcp: client sends JSON-RPC request; we respond with JSON.
    - GET /mcp: optional SSE channel for server-initiated messages. We don't
      use server-initiated traffic, so return 405 to be explicit.
    - DELETE /mcp: client signals end of session. We accept and 200.
    - Notifications (no `id`) → HTTP 202 Accepted with empty body.
    - All responses include Mcp-Session-Id so clients can pin a session.
    """
    http_method = (event.get("httpMethod")
                   or (event.get("requestContext", {}).get("http", {}) or {}).get("method")
                   or "POST").upper()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # IAM-authed callers populate requestContext.identity / authorizer.
    request_context = event.get("requestContext") or {}
    identity = (request_context.get("identity") or {})
    actor = identity.get("userArn") or identity.get("user") or "unknown"

    # Session id — return the one the client sent, otherwise mint one.
    session_id = headers.get("mcp-session-id") or _new_session_id()

    if http_method == "GET":
        # We don't push server-initiated messages over SSE.
        return _empty_response(405, session_id, body={"error": "GET not supported on this server"})

    if http_method == "DELETE":
        return _empty_response(200, session_id)

    if http_method != "POST":
        return _empty_response(405, session_id)

    idempotency_key = headers.get("x-idempotency-key")

    body_str = event.get("body", "{}")
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode()
    body = json.loads(body_str) if body_str else {}

    # Notifications: no id → must respond 202 with no body.
    is_notification = "id" not in body and isinstance(body.get("method"), str)
    result = handle_mcp_request(body, idempotency_key=idempotency_key, actor=actor)

    if result is None or is_notification:
        return _empty_response(202, session_id)

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
            "Cache-Control": "no-store",
        },
        "body": json.dumps(result, default=str),
    }


def _new_session_id() -> str:
    import secrets
    return secrets.token_urlsafe(16)


def _empty_response(status: int, session_id: str, body: dict | None = None):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body) if body else "",
    }


# ── Local dev server ────────────────────────────────────────────────────────

if __name__ == "__main__":
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class MCPHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            idem_key = self.headers.get("X-Idempotency-Key")
            result = handle_mcp_request(body, idempotency_key=idem_key, actor="local-dev")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, default=str).encode())

    port = int(os.getenv("PORT", "8081"))
    print(f"Jira MCP server listening on http://localhost:{port}/mcp")
    HTTPServer(("", port), MCPHandler).serve_forever()
