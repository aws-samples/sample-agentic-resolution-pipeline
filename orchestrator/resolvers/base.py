"""
Base interface for repo resolution.

Customers implement RepoResolver to plug in their own service catalog,
CMDB, or registry. The pipeline ships with ConfigFileResolver (YAML-based)
as the zero-dependency default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RepoInfo:
    """Resolved repository details passed to the Resolution Agent."""
    repo_url: str
    branch: str = "main"
    path: str | None = None # Subdirectory to scope the agent's focus
    provider: str = "github"  # github | gitlab | codecommit | bitbucket
    auth_secret_arn: str | None = None  # Secrets Manager ARN for repo credentials
    metadata: dict = field(default_factory=dict)  # Extra context for the agent


class RepoResolver(ABC):
    """
    Interface for resolving a Jira ticket to the repository that needs fixing.

    Implementations:
      - ConfigFileResolver (default)  — static YAML mapping
      - DynamoDBResolver              — DynamoDB lookup table
      - BackstageResolver             — Backstage catalog API
      - ServiceNowResolver            — ServiceNow CMDB
      - AppRegistryResolver           — AWS Service Catalog AppRegistry
    """

    @abstractmethod
    def resolve(
        self,
        project: str,
        component: str | None = None,
        issue_type: str | None = None,
        rca_summary: str | None = None,
    ) -> RepoInfo | None:
        """
        Resolve a Jira ticket context to a repository.

        Args:
            project: Jira project key (e.g., "CHECKOUT")
            component: Jira component name (e.g., "order-service"), may be None
            issue_type: Ticket type (e.g., "Bug", "Incident")
            rca_summary: RCA text from the DevOps Agent — can help narrow to
                         specific services/modules in monorepo setups

        Returns:
            RepoInfo if a matching repo is found, None otherwise.
        """
        ...
