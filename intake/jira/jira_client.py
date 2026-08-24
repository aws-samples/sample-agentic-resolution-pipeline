"""
Jira Cloud REST v3 client.

Thin wrapper over httpx with auth (email + API token from Secrets Manager),
retry/backoff for transient 5xx, and methods for the 15 MCP tool operations.

Auth: HTTP Basic with email:api_token (per Atlassian Cloud docs).
Token is fetched once per cold start from Secrets Manager and cached in module scope.
"""

import base64
import json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "")
JIRA_SECRET_ARN = os.getenv("JIRA_SECRET_ARN", "")
HTTP_TIMEOUT_S = float(os.getenv("JIRA_HTTP_TIMEOUT_S", "15"))
MAX_RETRIES = int(os.getenv("JIRA_MAX_RETRIES", "3"))
DRY_RUN = os.getenv("JIRA_MCP_DRY_RUN", "").lower() in ("1", "true", "yes")
LOCAL_MODE = os.getenv("JIRA_MCP_LOCAL_MODE", "").lower() in ("1", "true", "yes")

_credentials_cache: dict[str, str] | None = None


def _dry_run_response(operation: str, **detail) -> dict:
    """
    Synthetic response for write operations when DRY_RUN is enabled.
    Always carries `dry_run: true` so the dispatcher can skip idempotency
    recording — a corrected real-run with the same key should still fire.
    """
    return {"dry_run": True, "operation": operation, "would_have_sent": detail,
            "note": "JIRA_MCP_DRY_RUN is enabled — no Jira call was made"}


class JiraError(Exception):
    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"Jira {status_code}: {message}")
        self.status_code = status_code
        self.body = body


def _load_credentials() -> dict[str, str]:
    """Load {email, api_token} from Secrets Manager. Cached per Lambda container."""
    global _credentials_cache
    if _credentials_cache is not None:
        return _credentials_cache

    if not JIRA_SECRET_ARN:
        raise RuntimeError("JIRA_SECRET_ARN env var not set")

    import boto3
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=JIRA_SECRET_ARN)
    secret = json.loads(resp["SecretString"])
    if "email" not in secret or "api_token" not in secret:
        raise RuntimeError("Jira secret must contain 'email' and 'api_token' keys")
    _credentials_cache = {"email": secret["email"], "api_token": secret["api_token"]}
    return _credentials_cache


def _auth_header() -> str:
    creds = _load_credentials()
    raw = f"{creds['email']}:{creds['api_token']}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def _request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
             files: dict | None = None, extra_headers: dict | None = None) -> Any:
    """
    Issue a Jira REST request with retry/backoff on 5xx and 429.
    Returns parsed JSON (or None for 204). Raises JiraError on non-retriable failure.
    """
    if not JIRA_BASE_URL:
        raise RuntimeError("JIRA_BASE_URL env var not set")

    url = f"{JIRA_BASE_URL.rstrip('/')}{path}"
    headers = {"Authorization": _auth_header(), "Accept": "application/json"}
    if json_body is not None and files is None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)

    backoff = 0.5
    last_err: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
                if files is not None:
                    resp = client.request(method, url, params=params, headers=headers, files=files)
                else:
                    resp = client.request(method, url, params=params, headers=headers, json=json_body)

            if resp.status_code in (429, 502, 503, 504) and attempt < MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                sleep_s = float(retry_after) if retry_after else backoff
                logger.warning("Jira %s %s → %s; retrying in %.2fs (attempt %d)",
                               method, path, resp.status_code, sleep_s, attempt + 1)
                time.sleep(sleep_s)
                backoff *= 2
                continue

            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise JiraError(resp.status_code, f"{method} {path}", body)

            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        except httpx.TimeoutException as e:
            last_err = e
            if attempt < MAX_RETRIES:
                logger.warning("Jira timeout on %s %s; retry %d", method, path, attempt + 1)
                time.sleep(backoff)  # nosemgrep: arbitrary-sleep
                backoff *= 2
                continue
            raise JiraError(599, f"timeout calling {method} {path}", str(e)) from e

    raise JiraError(599, f"exhausted retries: {last_err}")


# ── Local-mode dispatch helper ─────────────────────────────────────────────

_stub_module = None


def _stub():
    """Lazy + memoized import of local-dev/jira_stub.py. Memoization is critical
    — the stub holds in-memory state (issues, comments) and re-importing on every
    call would give each tool its own fresh world."""
    global _stub_module
    if _stub_module is not None:
        return _stub_module
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "jira_stub", os.path.join(here, "local-dev", "jira_stub.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _stub_module = mod
    return mod


# ── Read operations ─────────────────────────────────────────────────────────

def search_issues_jql(jql: str, fields: list[str] | None = None, start_at: int = 0,
                      max_results: int = 50, expand: list[str] | None = None,
                      next_page_token: str | None = None) -> dict:
    """
    Jira Cloud's POST /rest/api/3/search returned 410 Gone after the May 2025
    deprecation. The new endpoint is /search/jql with token-based pagination
    (no startAt — the response carries `nextPageToken` for the next page).
    """
    if LOCAL_MODE:
        return _stub().search_issues_jql(jql, fields, start_at, max_results, expand)
    body: dict[str, Any] = {"jql": jql, "maxResults": max_results}
    if fields:
        body["fields"] = fields
    if expand:
        body["expand"] = ",".join(expand) if isinstance(expand, list) else expand
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return _request("POST", "/rest/api/3/search/jql", json_body=body)


def get_issue(issue_key: str, fields: list[str] | None = None,
              expand: list[str] | None = None) -> dict:
    if LOCAL_MODE:
        return _stub().get_issue(issue_key, fields, expand)
    params = {}
    if fields:
        params["fields"] = ",".join(fields)
    if expand:
        params["expand"] = ",".join(expand)
    return _request("GET", f"/rest/api/3/issue/{issue_key}", params=params)


def get_issue_comments(issue_key: str, start_at: int = 0, max_results: int = 50) -> dict:
    if LOCAL_MODE:
        return _stub().get_issue_comments(issue_key, start_at, max_results)
    return _request("GET", f"/rest/api/3/issue/{issue_key}/comment",
                    params={"startAt": start_at, "maxResults": max_results})


def get_issue_changelog(issue_key: str, start_at: int = 0, max_results: int = 50) -> dict:
    if LOCAL_MODE:
        return _stub().get_issue_changelog(issue_key, start_at, max_results)
    return _request("GET", f"/rest/api/3/issue/{issue_key}/changelog",
                    params={"startAt": start_at, "maxResults": max_results})


def get_transitions(issue_key: str) -> dict:
    if LOCAL_MODE:
        return _stub().get_transitions(issue_key)
    return _request("GET", f"/rest/api/3/issue/{issue_key}/transitions")


def get_project_metadata(project_key: str) -> dict:
    """Returns issue types + create-meta with field schemas."""
    if LOCAL_MODE:
        return _stub().get_project_metadata(project_key)
    return _request("GET", "/rest/api/3/issue/createmeta",
                    params={"projectKeys": project_key, "expand": "projects.issuetypes.fields"})


def get_attachments(issue_key: str) -> list[dict]:
    if LOCAL_MODE:
        return _stub().get_attachments(issue_key)
    issue = get_issue(issue_key, fields=["attachment"])
    return issue.get("fields", {}).get("attachment", [])


def get_user(query: str) -> list[dict]:
    """Resolve users by email or display name. Returns list (Jira may match multiple)."""
    if LOCAL_MODE:
        return _stub().get_user(query)
    return _request("GET", "/rest/api/3/user/search", params={"query": query})


# ── Write operations ────────────────────────────────────────────────────────

def create_issue(project_key: str, issue_type: str, summary: str,
                 description_adf: dict | None = None, fields: dict | None = None) -> dict:
    body_fields = {
        "project": {"key": project_key},
        "issuetype": {"name": issue_type},
        "summary": summary,
    }
    if description_adf is not None:
        body_fields["description"] = description_adf
    if fields:
        body_fields.update(fields)
    if DRY_RUN:
        return _dry_run_response("create_issue", fields=body_fields)
    if LOCAL_MODE:
        return _stub().create_issue(project_key, issue_type, summary, description_adf, fields)
    return _request("POST", "/rest/api/3/issue", json_body={"fields": body_fields})


def update_issue(issue_key: str, fields: dict) -> dict | None:
    if DRY_RUN:
        return _dry_run_response("update_issue", issue_key=issue_key, fields=fields)
    if LOCAL_MODE:
        _stub().update_issue(issue_key, fields)
        return None
    _request("PUT", f"/rest/api/3/issue/{issue_key}", json_body={"fields": fields})
    return None


def transition_issue(issue_key: str, transition_id: str, comment_adf: dict | None = None) -> dict | None:
    body: dict[str, Any] = {"transition": {"id": transition_id}}
    if comment_adf is not None:
        body["update"] = {"comment": [{"add": {"body": comment_adf}}]}
    if DRY_RUN:
        return _dry_run_response("transition_issue", issue_key=issue_key, body=body)
    if LOCAL_MODE:
        _stub().transition_issue(issue_key, transition_id, comment_adf)
        return None
    _request("POST", f"/rest/api/3/issue/{issue_key}/transitions", json_body=body)
    return None


def add_comment(issue_key: str, body_adf: dict) -> dict:
    if DRY_RUN:
        return _dry_run_response("add_comment", issue_key=issue_key, body=body_adf)
    if LOCAL_MODE:
        return _stub().add_comment(issue_key, body_adf)
    return _request("POST", f"/rest/api/3/issue/{issue_key}/comment", json_body={"body": body_adf})


def add_attachment(issue_key: str, filename: str, content: bytes,
                   content_type: str = "application/octet-stream") -> list[dict] | dict:
    if DRY_RUN:
        return _dry_run_response("add_attachment", issue_key=issue_key, filename=filename,
                                  size_bytes=len(content), content_type=content_type)
    if LOCAL_MODE:
        return _stub().add_attachment(issue_key, filename, content, content_type)
    files = {"file": (filename, content, content_type)}
    return _request(
        "POST", f"/rest/api/3/issue/{issue_key}/attachments",
        files=files,
        extra_headers={"X-Atlassian-Token": "no-check"},
    )


def link_issues(inward_key: str, outward_key: str, link_type: str) -> dict | None:
    body = {
        "type": {"name": link_type},
        "inwardIssue": {"key": inward_key},
        "outwardIssue": {"key": outward_key},
    }
    if DRY_RUN:
        return _dry_run_response("link_issues", body=body)
    if LOCAL_MODE:
        _stub().link_issues(inward_key, outward_key, link_type)
        return None
    _request("POST", "/rest/api/3/issueLink", json_body=body)
    return None


def bulk_update(issue_keys: list[str], fields: dict) -> list[dict]:
    """Apply the same field update to multiple issues. Returns per-issue results."""
    results = []
    for key in issue_keys:
        try:
            update_issue(key, fields)  # respects DRY_RUN internally
            results.append({"key": key, "status": "ok"})
        except JiraError as e:
            results.append({"key": key, "status": "error", "code": e.status_code, "message": str(e)})
    return results


def bulk_transition(issue_keys: list[str], transition_id: str) -> list[dict]:
    results = []
    for key in issue_keys:
        try:
            transition_issue(key, transition_id)  # respects DRY_RUN internally
            results.append({"key": key, "status": "ok"})
        except JiraError as e:
            results.append({"key": key, "status": "error", "code": e.status_code, "message": str(e)})
    return results
