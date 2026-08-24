"""Strands tool: Resolve repository from ticket metadata."""

from __future__ import annotations

import logging
import os

from strands import tool

from .config_resolver import ConfigFileResolver

logger = logging.getLogger(__name__)

REPO_CONFIG_PATH = os.getenv("REPO_CONFIG_PATH", "/app/repo-config.yaml")


@tool
def resolve_repository(project: str, component: str = "") -> dict:
    """Determine which repository contains the code to fix for a given Jira project and component.

    Args:
        project: Jira project key (e.g., 'CHECKOUT', 'ARP', 'IOT').
        component: Jira component name (optional, narrows the repo match).

    Returns:
        dict with resolved (bool), repo_url, branch, provider, auth_secret_arn, path.
    """
    if not project:
        return {"resolved": False, "error": "project parameter is required"}

    resolver = ConfigFileResolver(config_path=REPO_CONFIG_PATH)
    repo_info = resolver.resolve(project=project, component=component)

    if repo_info is None:
        return {
            "resolved": False,
            "error": f"No repo mapping for project={project} component={component}",
        }

    return {
        "resolved": True,
        "repo_url": repo_info.repo_url,
        "branch": repo_info.branch,
        "provider": repo_info.provider,
        "auth_secret_arn": repo_info.auth_secret_arn or "",
        "path": repo_info.path or "",
    }
