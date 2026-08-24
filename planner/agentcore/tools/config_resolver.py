"""
YAML config-based repo resolver — bundled into the Planner container.

Adapted from orchestrator/resolvers/ for standalone use inside AgentCore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RepoInfo:
    """Resolved repository details."""
    repo_url: str
    branch: str = "main"
    path: str | None = None
    provider: str = "github"
    auth_secret_arn: str | None = None
    metadata: dict = field(default_factory=dict)


class ConfigFileResolver:

    def __init__(self, config_path: str | None = None):
        self._config_path = config_path or str(Path(__file__).parent.parent / "repo-config.yaml")
        self._config: dict[str, Any] | None = None

    @property
    def config(self) -> dict[str, Any]:
        if self._config is None:
            with open(self._config_path, encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
        return self._config

    def resolve(
        self,
        project: str,
        component: str | None = None,
        issue_type: str | None = None,
        rca_summary: str | None = None,
    ) -> RepoInfo | None:
        mappings = self.config.get("mappings", [])
        defaults = self.config.get("defaults", {})

        if component:
            for entry in mappings:
                if (entry.get("project", "").upper() == project.upper()
                        and entry.get("component", "").lower() == component.lower()):
                    return self._to_repo_info(entry, defaults)

        for entry in mappings:
            if (entry.get("project", "").upper() == project.upper()
                    and not entry.get("component")):
                return self._to_repo_info(entry, defaults)

        default_repo = defaults.get("repo")
        if default_repo:
            return RepoInfo(
                repo_url=default_repo,
                branch=defaults.get("branch", "main"),
                provider=defaults.get("provider", "github"),
            )

        return None

    def _to_repo_info(self, entry: dict, defaults: dict) -> RepoInfo:
        return RepoInfo(
            repo_url=entry["repo"],
            branch=entry.get("branch", defaults.get("branch", "main")),
            path=entry.get("path"),
            provider=entry.get("provider", defaults.get("provider", "github")),
            auth_secret_arn=entry.get("auth_secret_arn"),
            metadata={k: v for k, v in entry.items()
                      if k not in ("project", "component", "repo", "branch", "path", "provider", "auth_secret_arn")},
        )
