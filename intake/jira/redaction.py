"""
Secret redaction — regex-based scrub of agent-supplied text before it ships to Jira.

Applied to comment bodies, description text, and attachment filenames. Replaces
matches with [REDACTED:<kind>] so reviewers can see WHAT was stripped without
seeing the value.

Conservative by design — false positives are preferable to leaking a real secret
into Jira's permanent history. Tune via JIRA_REDACTION_DISABLE env var if needed.
"""

import os
import re

DISABLED = os.getenv("JIRA_REDACTION_DISABLE", "").lower() in ("1", "true", "yes")

# (label, pattern) — order matters; more specific patterns first.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b|\bghs_[A-Za-z0-9]{36}\b|\bgho_[A-Za-z0-9]{36}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    ("password_kv", re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\",;]{6,}['\"]?")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]


def redact_text(text: str) -> str:
    """Run all patterns over `text`; replace each match with [REDACTED:<label>]."""
    if DISABLED or not text:
        return text
    for label, pattern in PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def redact_adf(adf: dict | None) -> dict | None:
    """
    Walk an Atlassian Document Format tree and redact every leaf-level text node.
    ADF nodes have shape {type, content?, text?, attrs?, marks?}.
    """
    if DISABLED or adf is None:
        return adf
    return _walk(adf)


def _walk(node):
    if isinstance(node, dict):
        out = {k: _walk(v) for k, v in node.items()}
        if "text" in out and isinstance(out["text"], str):
            out["text"] = redact_text(out["text"])
        return out
    if isinstance(node, list):
        return [_walk(n) for n in node]
    return node


def redact_filename(name: str) -> str:
    """Filenames can also embed secrets (e.g. 'AKIA...env'). Same scrub."""
    return redact_text(name)
