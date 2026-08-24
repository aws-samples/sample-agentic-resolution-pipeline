"""Tests for the repo resolver module."""

import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from resolvers import ConfigFileResolver, RepoInfo


def _write_config(data: dict) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(data, f)
    f.close()
    return f.name


class TestConfigFileResolver:

    def test_exact_project_component_match(self):
        config = _write_config({
            "mappings": [
                {"project": "CHECKOUT", "component": "order-service",
                 "repo": "https://github.com/acme/orders", "branch": "main"},
                {"project": "CHECKOUT",
                 "repo": "https://github.com/acme/checkout", "branch": "main"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="CHECKOUT", component="order-service")

        assert result is not None
        assert result.repo_url == "https://github.com/acme/orders"

    def test_project_only_fallback(self):
        config = _write_config({
            "mappings": [
                {"project": "CHECKOUT", "component": "order-service",
                 "repo": "https://github.com/acme/orders", "branch": "main"},
                {"project": "CHECKOUT",
                 "repo": "https://github.com/acme/checkout", "branch": "main"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="CHECKOUT", component="unknown-thing")

        assert result is not None
        assert result.repo_url == "https://github.com/acme/checkout"

    def test_project_fallback_when_no_component_provided(self):
        config = _write_config({
            "mappings": [
                {"project": "CHECKOUT", "component": "order-service",
                 "repo": "https://github.com/acme/orders", "branch": "main"},
                {"project": "CHECKOUT",
                 "repo": "https://github.com/acme/checkout", "branch": "main"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="CHECKOUT")

        assert result is not None
        assert result.repo_url == "https://github.com/acme/checkout"

    def test_global_default_fallback(self):
        config = _write_config({
            "mappings": [
                {"project": "CHECKOUT",
                 "repo": "https://github.com/acme/checkout", "branch": "main"},
            ],
            "defaults": {"branch": "develop", "provider": "gitlab",
                         "repo": "https://gitlab.com/acme/fallback"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="UNKNOWN")

        assert result is not None
        assert result.repo_url == "https://gitlab.com/acme/fallback"
        assert result.branch == "develop"
        assert result.provider == "gitlab"

    def test_no_match_returns_none(self):
        config = _write_config({
            "mappings": [
                {"project": "CHECKOUT",
                 "repo": "https://github.com/acme/checkout", "branch": "main"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="UNKNOWN")

        assert result is None

    def test_path_and_provider_preserved(self):
        config = _write_config({
            "mappings": [
                {"project": "PLATFORM", "component": "auth",
                 "repo": "https://github.com/acme/monorepo",
                 "branch": "main", "path": "services/auth",
                 "provider": "github"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="PLATFORM", component="auth")

        assert result is not None
        assert result.path == "services/auth"
        assert result.provider == "github"

    def test_auth_secret_arn_preserved(self):
        config = _write_config({
            "mappings": [
                {"project": "INFRA",
                 "repo": "https://gitlab.com/acme/infra",
                 "branch": "main", "provider": "gitlab",
                 "auth_secret_arn": "arn:aws:secretsmanager:us-east-1:123:secret:token"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="INFRA")

        assert result is not None
        assert result.auth_secret_arn == "arn:aws:secretsmanager:us-east-1:123:secret:token"

    def test_case_insensitive_matching(self):
        config = _write_config({
            "mappings": [
                {"project": "checkout", "component": "Order-Service",
                 "repo": "https://github.com/acme/orders", "branch": "main"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        resolver = ConfigFileResolver(config)
        result = resolver.resolve(project="CHECKOUT", component="order-service")

        assert result is not None
        assert result.repo_url == "https://github.com/acme/orders"
