"""
YAML config-based repo resolver — zero-dependency default for workshops.

Loads a repo-config.yaml and matches Jira project + component to a repository.
Supports fallback logic: exact project+component match first, then project-only,
then global default.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .base import RepoInfo, RepoResolver

DEFAULT_CONFIG_PATH = os.getenv(
    "REPO_CONFIG_PATH",
    str(Path(__file__).parent.parent / "repo-config.yaml"),
)


class ConfigFileResolver(RepoResolver):

    def __init__(self, config_path: str | None = None):
        self._config_path = config_path or DEFAULT_CONFIG_PATH
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

        # Pass 1: exact project + component match
        if component:
            for entry in mappings:
                if (entry.get("project", "").upper() == project.upper()
                        and entry.get("component", "").lower() == component.lower()):
                    return self._to_repo_info(entry, defaults)

        # Pass 2: project-only fallback (entries with no component)
        for entry in mappings:
            if (entry.get("project", "").upper() == project.upper()
                    and not entry.get("component")):
                return self._to_repo_info(entry, defaults)

        # Pass 3: global default (if configured)
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
