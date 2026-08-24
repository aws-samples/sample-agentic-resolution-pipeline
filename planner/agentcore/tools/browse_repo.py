"""Strands tool: Browse repository tree via provider REST API."""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import boto3
import requests
from strands import tool

logger = logging.getLogger(__name__)

_credentials_cache: dict[str, str] = {}


@tool
def browse_repo_tree(
    repo_url: str,
    path: str = "",
    branch: str = "main",
    provider: str = "github",
    auth_secret_arn: str = "",
) -> dict:
    """Browse a repository's directory tree or read a file via the provider's REST API.

    Use this to validate file paths before building your strategy. You can:
    - List directory contents (pass a directory path or empty string for root)
    - Read file contents (pass a file path)

    Call this AFTER resolve_repository to explore the actual codebase structure.

    Args:
        repo_url: Full repository URL (e.g., https://github.com/acme/repo or https://bitbucket.org/ws/repo.git).
        path: Path within the repo to list or read. Empty string = root directory.
        branch: Branch or ref to browse (default: main).
        provider: Git provider - 'github' or 'bitbucket'.
        auth_secret_arn: Secrets Manager ARN for credentials. Required for private repos.

    Returns:
        dict with 'type' ('directory' or 'file'), and either 'entries' (for directories)
        or 'content' (for files). Returns 'type': 'error' on failure.
    """
    if not repo_url:
        return {"type": "error", "message": "repo_url is required"}

    if not repo_url.startswith("https://"):
        return {"type": "error", "message": "Only HTTPS URLs are supported"}

    try:
        owner, repo = _parse_repo_url(repo_url, provider)
    except ValueError as e:
        return {"type": "error", "message": str(e)}

    credentials = _get_credentials(auth_secret_arn) if auth_secret_arn else None

    try:
        if provider == "bitbucket":
            return _browse_bitbucket(owner, repo, path, branch, credentials)
        elif provider == "github":
            return _browse_github(owner, repo, path, branch, credentials)
        else:
            return {"type": "error", "message": f"Unsupported provider: {provider}"}
    except requests.HTTPError as e:
        return {"type": "error", "message": f"HTTP {e.response.status_code}: {e.response.reason}", "path": path}
    except Exception as e:
        logger.error(f"browse_repo_tree failed: {e}")
        return {"type": "error", "message": str(e), "path": path}


def _parse_repo_url(repo_url: str, provider: str) -> tuple[str, str]:
    url = repo_url.rstrip("/").removesuffix(".git")

    if provider == "bitbucket":
        prefix = "https://bitbucket.org/"
        if not url.startswith(prefix):
            raise ValueError(f"Expected Bitbucket URL starting with {prefix}")
        parts = url[len(prefix):].split("/")
        if len(parts) < 2:
            raise ValueError(f"Cannot parse workspace/repo from URL: {repo_url}")
        return parts[0], parts[1]

    elif provider == "github":
        prefix = "https://github.com/"
        if not url.startswith(prefix):
            raise ValueError(f"Expected GitHub URL starting with {prefix}")
        parts = url[len(prefix):].split("/")
        if len(parts) < 2:
            raise ValueError(f"Cannot parse owner/repo from URL: {repo_url}")
        return parts[0], parts[1]

    raise ValueError(f"Unsupported provider: {provider}")


def _get_credentials(secret_arn: str) -> str:
    if secret_arn in _credentials_cache:
        return _credentials_cache[secret_arn]

    client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1"))
    response = client.get_secret_value(SecretId=secret_arn)
    secret = response["SecretString"]
    _credentials_cache[secret_arn] = secret
    return secret


def _browse_bitbucket(workspace: str, repo: str, path: str, branch: str, credentials: str | None) -> dict:
    path = path.strip("/")
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/src/{branch}/{path}"

    headers = {}
    if credentials:
        # Bitbucket secret format: email:app_password
        auth_b64 = base64.b64encode(credentials.encode()).decode()
        headers["Authorization"] = f"Basic {auth_b64}"

    data = _http_get(url, headers)

    # Bitbucket returns a paginated response for directories, raw content for files
    if isinstance(data, dict) and "values" in data:
        entries = []
        for item in data["values"][:100]:
            entries.append({
                "name": item.get("path", "").split("/")[-1],
                "path": item.get("path", ""),
                "type": "directory" if item.get("type") == "commit_directory" else "file",
                "size": item.get("size", 0),
            })
        return {"type": "directory", "path": path or "/", "entries": entries}
    elif isinstance(data, str):
        content = data[:102400]
        return {
            "type": "file",
            "path": path,
            "content": content,
            "size": len(data),
            "truncated": len(data) > 102400,
        }
    else:
        return {"type": "error", "message": "Unexpected response format", "path": path}


def _browse_github(owner: str, repo: str, path: str, branch: str, credentials: str | None) -> dict:
    path = path.strip("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"

    headers = {"Accept": "application/vnd.github.v3+json"}
    if credentials:
        headers["Authorization"] = f"Bearer {credentials}"

    data = _http_get(url, headers)

    if isinstance(data, list):
        entries = []
        for item in data[:100]:
            entries.append({
                "name": item.get("name", ""),
                "path": item.get("path", ""),
                "type": "directory" if item.get("type") == "dir" else "file",
                "size": item.get("size", 0),
            })
        return {"type": "directory", "path": path or "/", "entries": entries}
    elif isinstance(data, dict) and "content" in data:
        content_b64 = data["content"]
        content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        content = content[:102400]
        return {
            "type": "file",
            "path": path,
            "content": content,
            "size": data.get("size", len(content)),
            "truncated": data.get("size", 0) > 102400,
        }
    elif isinstance(data, dict) and data.get("type") == "file" and "download_url" in data:
        # Large files use download_url instead of inline content
        content = _http_get_raw(data["download_url"], headers)
        content = content[:102400]
        return {
            "type": "file",
            "path": path,
            "content": content,
            "size": data.get("size", len(content)),
            "truncated": data.get("size", 0) > 102400,
        }
    else:
        return {"type": "error", "message": "Unexpected response format", "path": path}


def _http_get(url: str, headers: dict) -> Any:
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "json" in content_type or resp.text.startswith(("{", "[")):
        return resp.json()
    return resp.text


def _http_get_raw(url: str, headers: dict) -> str:
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text
