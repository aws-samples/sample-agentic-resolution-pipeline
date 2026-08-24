"""
In-memory Jira stub. Speaks the same return shapes as the real REST v3 API
for the operations our MCP touches — enough for end-to-end local play.

Seeded with a couple of OPS issues + transitions on import. Not durable;
state is reset every time the process restarts.
"""

import threading
import time
from typing import Any

_lock = threading.Lock()
_next_issue_num: dict[str, int] = {"OPS": 100, "SEC": 100}
_issues: dict[str, dict] = {}
_comments: dict[str, list[dict]] = {}
_changelogs: dict[str, list[dict]] = {}
_attachments: dict[str, list[dict]] = {}
_links: list[dict] = []
_next_comment_id = [10000]


PROJECT_TRANSITIONS: dict[str, list[dict]] = {
    "OPS": [
        {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
        {"id": "21", "name": "In Review",      "to": {"name": "In Review"}},
        {"id": "31", "name": "Ready for QA",   "to": {"name": "Ready for QA"}},
        {"id": "41", "name": "Resolve",        "to": {"name": "Done"}},
        {"id": "51", "name": "Close",          "to": {"name": "Closed"}},
        {"id": "61", "name": "Reopen",         "to": {"name": "Open"}},
    ],
    "SEC": [
        {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
    ],
}


def _seed():
    _create_internal("OPS", "Bug", "Checkout 5xx spike at 14:00 UTC", priority="P1")
    _create_internal("OPS", "Task", "Investigate cart-svc latency", priority="P3")
    _create_internal("OPS", "Bug", "Order webhook redelivery loop", priority="P2", status="In Progress")
    _create_internal("SEC", "Task", "Audit S3 bucket policy",       priority="High")


def _create_internal(project: str, issue_type: str, summary: str,
                     priority: str | None = None, status: str = "Open") -> dict:
    with _lock:
        n = _next_issue_num.get(project, 100)
        _next_issue_num[project] = n + 1
    key = f"{project}-{n}"
    fields = {
        "summary": summary,
        "issuetype": {"name": issue_type},
        "project": {"key": project},
        "priority": {"name": priority} if priority else None,
        "status": {"name": status},
        "labels": [],
        "assignee": None,
        "reporter": {"accountId": "agent-svc"},
        "attachment": [],
    }
    issue = {"id": str(60000 + n), "key": key, "fields": fields,
             "self": f"https://local.test/rest/api/3/issue/{key}"}
    _issues[key] = issue
    _comments[key] = []
    _changelogs[key] = []
    _attachments[key] = []
    return issue


# ── Operations called by jira_client when LOCAL_MODE=true ───────────────────

def search_issues_jql(jql: str, fields=None, start_at=0, max_results=50, expand=None) -> dict:
    """Tiny JQL — supports clauses like 'project = OPS', 'priority in (P1,P2)', AND-joined."""
    matches = []
    clauses = [c.strip() for c in jql.split("AND")]
    for issue in _issues.values():
        if all(_match_clause(issue, c) for c in clauses):
            matches.append(issue)
    sliced = matches[start_at:start_at + max_results]
    return {"issues": sliced, "total": len(matches), "startAt": start_at, "maxResults": max_results}


def _match_clause(issue: dict, clause: str) -> bool:
    if not clause:
        return True
    f = issue["fields"]
    try:
        if " in " in clause:
            key, vals = clause.split(" in ", 1)
            key = key.strip()
            allowed = [v.strip().strip("'\"") for v in vals.strip("()").split(",")]
            return _field_value(f, key, issue) in allowed
        for op in (" = ", " != "):
            if op in clause:
                key, val = clause.split(op, 1)
                key, val = key.strip(), val.strip().strip("'\"")
                actual = _field_value(f, key, issue)
                return (actual == val) if op == " = " else (actual != val)
    except Exception:
        return False
    return True


def _field_value(fields: dict, key: str, issue: dict) -> str:
    if key == "project":
        return (fields.get("project") or {}).get("key", "")
    if key == "priority":
        return (fields.get("priority") or {}).get("name", "")
    if key == "status":
        return (fields.get("status") or {}).get("name", "")
    if key == "issuetype" or key == "type":
        return (fields.get("issuetype") or {}).get("name", "")
    if key == "key":
        return issue.get("key", "")
    return ""


def get_issue(issue_key: str, fields=None, expand=None) -> dict:
    if issue_key not in _issues:
        from jira_client import JiraError
        raise JiraError(404, f"GET /rest/api/3/issue/{issue_key}", {"errorMessages": ["Issue not found"]})
    return _issues[issue_key]


def get_issue_comments(issue_key: str, start_at=0, max_results=50) -> dict:
    comments = _comments.get(issue_key, [])
    sliced = comments[start_at:start_at + max_results]
    return {"comments": sliced, "total": len(comments), "startAt": start_at, "maxResults": max_results}


def get_issue_changelog(issue_key: str, start_at=0, max_results=50) -> dict:
    changes = _changelogs.get(issue_key, [])
    sliced = changes[start_at:start_at + max_results]
    return {"values": sliced, "total": len(changes), "startAt": start_at, "maxResults": max_results}


def get_transitions(issue_key: str) -> dict:
    project = issue_key.split("-", 1)[0]
    return {"transitions": PROJECT_TRANSITIONS.get(project, [])}


def get_project_metadata(project_key: str) -> dict:
    return {"projects": [{
        "key": project_key,
        "issuetypes": [
            {"name": "Bug", "fields": {}},
            {"name": "Task", "fields": {}},
            {"name": "Story", "fields": {}},
        ],
    }]}


def get_attachments(issue_key: str) -> list[dict]:
    return list(_attachments.get(issue_key, []))


def get_user(query: str) -> list[dict]:
    return [{"accountId": "local-fake-user", "displayName": query, "emailAddress": query}]


def create_issue(project_key: str, issue_type: str, summary: str,
                 description_adf=None, fields=None) -> dict:
    extra = dict(fields or {})
    priority = None
    if "priority" in extra and isinstance(extra["priority"], dict):
        priority = extra["priority"].get("name")
    issue = _create_internal(project_key, issue_type, summary, priority=priority)
    if description_adf is not None:
        issue["fields"]["description"] = description_adf
    return {"id": issue["id"], "key": issue["key"], "self": issue["self"]}


def update_issue(issue_key: str, fields: dict) -> None:
    issue = _issues.get(issue_key)
    if not issue:
        from jira_client import JiraError
        raise JiraError(404, f"PUT /rest/api/3/issue/{issue_key}", {"errorMessages": ["Issue not found"]})
    issue["fields"].update(fields)


def transition_issue(issue_key: str, transition_id: str, comment_adf=None) -> None:
    project = issue_key.split("-", 1)[0]
    transitions = PROJECT_TRANSITIONS.get(project, [])
    target = next((t for t in transitions if t["id"] == transition_id), None)
    if not target:
        from jira_client import JiraError
        raise JiraError(400, f"transition {transition_id} not found", {"errorMessages": ["Bad transition"]})
    issue = _issues[issue_key]
    prev = issue["fields"]["status"]["name"]
    issue["fields"]["status"] = {"name": target["to"]["name"]}
    _changelogs[issue_key].append({
        "id": str(int(time.time() * 1000)),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "items": [{"field": "status", "fromString": prev, "toString": target["to"]["name"]}],
    })
    if comment_adf is not None:
        add_comment(issue_key, comment_adf)


def add_comment(issue_key: str, body_adf: dict) -> dict:
    if issue_key not in _issues:
        from jira_client import JiraError
        raise JiraError(404, f"POST comment on {issue_key}", {"errorMessages": ["Issue not found"]})
    cid = str(_next_comment_id[0]); _next_comment_id[0] += 1
    comment = {"id": cid, "body": body_adf,
               "created": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    _comments[issue_key].append(comment)
    return comment


def add_attachment(issue_key: str, filename: str, content: bytes,
                   content_type: str = "application/octet-stream") -> list[dict]:
    if issue_key not in _issues:
        from jira_client import JiraError
        raise JiraError(404, f"attachment on {issue_key}", {"errorMessages": ["Issue not found"]})
    att = {"id": str(int(time.time() * 1000)), "filename": filename,
           "size": len(content), "mimeType": content_type}
    _attachments[issue_key].append(att)
    _issues[issue_key]["fields"]["attachment"].append(att)
    return [att]


def link_issues(inward_key: str, outward_key: str, link_type: str) -> None:
    if inward_key not in _issues or outward_key not in _issues:
        from jira_client import JiraError
        raise JiraError(404, "link missing issue", {"errorMessages": ["Issue not found"]})
    _links.append({"inward": inward_key, "outward": outward_key, "type": link_type})


_seed()
