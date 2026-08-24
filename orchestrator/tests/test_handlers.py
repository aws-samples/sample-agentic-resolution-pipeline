"""Tests for resolve_repo_handler and post_resolution_handler."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import handler


def _write_config(data: dict) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(data, f)
    f.close()
    return f.name


SAMPLE_CONFIG = {
    "mappings": [
        {"project": "CHECKOUT", "component": "order-service",
         "repo": "https://github.com/acme/orders", "branch": "main"},
        {"project": "CHECKOUT",
         "repo": "https://github.com/acme/checkout", "branch": "develop"},
    ],
    "defaults": {"branch": "main", "provider": "github"},
}


class TestResolveRepoHandler:

    def _make_event(self, project="CHECKOUT", component=None, issue_key="CHECKOUT-42"):
        return {
            "classification": {"issue_key": issue_key},
            "event": {
                "data": {
                    "metadata": {
                        "project": project,
                        "component": component,
                        "issue_type": "Bug",
                    }
                }
            },
            "rca_result": {"rca_summary": "NullPointerException in OrderService.process()"},
        }

    def test_resolves_with_component(self):
        config_path = _write_config(SAMPLE_CONFIG)
        with patch("handler.ConfigFileResolver") as mock_cls:
            from resolvers.config_resolver import ConfigFileResolver
            real_resolver = ConfigFileResolver(config_path)
            mock_cls.return_value = real_resolver

            result = handler.resolve_repo_handler(
                self._make_event(component="order-service"), None
            )

        assert result["resolved"] is True
        assert result["repo_url"] == "https://github.com/acme/orders"
        assert result["default_branch"] == "main"
        assert result["issue_key"] == "CHECKOUT-42"

    def test_resolves_project_fallback(self):
        config_path = _write_config(SAMPLE_CONFIG)
        with patch("handler.ConfigFileResolver") as mock_cls:
            from resolvers.config_resolver import ConfigFileResolver
            real_resolver = ConfigFileResolver(config_path)
            mock_cls.return_value = real_resolver

            result = handler.resolve_repo_handler(
                self._make_event(component="unknown"), None
            )

        assert result["resolved"] is True
        assert result["repo_url"] == "https://github.com/acme/checkout"
        assert result["default_branch"] == "develop"

    def test_no_match_returns_resolved_false(self):
        config_path = _write_config({
            "mappings": [
                {"project": "OTHER", "repo": "https://github.com/acme/other", "branch": "main"},
            ],
            "defaults": {"branch": "main", "provider": "github"},
        })
        with patch("handler.ConfigFileResolver") as mock_cls:
            from resolvers.config_resolver import ConfigFileResolver
            real_resolver = ConfigFileResolver(config_path)
            mock_cls.return_value = real_resolver

            result = handler.resolve_repo_handler(
                self._make_event(project="NOMATCH"), None
            )

        assert result["resolved"] is False
        assert "error" in result

    def test_extracts_project_from_issue_key_when_missing(self):
        config_path = _write_config(SAMPLE_CONFIG)
        with patch("handler.ConfigFileResolver") as mock_cls:
            from resolvers.config_resolver import ConfigFileResolver
            real_resolver = ConfigFileResolver(config_path)
            mock_cls.return_value = real_resolver

            event = {
                "classification": {"issue_key": "CHECKOUT-99"},
                "event": {"data": {"metadata": {}}},
                "rca_result": {},
            }
            result = handler.resolve_repo_handler(event, None)

        assert result["resolved"] is True
        assert result["repo_url"] == "https://github.com/acme/checkout"


class TestPostResolutionHandler:

    def test_success_reads_s3_and_notifies(self):
        mock_s3 = MagicMock()
        mock_sns = MagicMock()
        output_json = json.dumps({
            "pr_url": "https://github.com/acme/orders/pull/7",
            "status": "success",
            "issue_key": "CHECKOUT-42",
            "branch": "fix/CHECKOUT-42",
        }).encode()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: output_json)}

        with patch("boto3.client") as mock_client:
            def client_factory(service):
                if service == "s3":
                    return mock_s3
                if service == "sns":
                    return mock_sns
                return MagicMock()

            mock_client.side_effect = client_factory
            handler.SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:123:topic"

            result = handler.post_resolution_handler({
                "issue_key": "CHECKOUT-42",
                "output_bucket": "my-bucket",
                "output_key": "resolutions/CHECKOUT-42/output.json",
                "event": {},
                "classification": {},
            }, None)

        assert result["pr_url"] == "https://github.com/acme/orders/pull/7"
        assert result["status"] == "success"
        assert result["notified"] is True
        mock_s3.get_object.assert_called_once_with(
            Bucket="my-bucket", Key="resolutions/CHECKOUT-42/output.json"
        )
        mock_sns.publish.assert_called_once()

    def test_failed_resolution_sends_failure_notification(self):
        mock_sns = MagicMock()

        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_sns
            handler.SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:123:topic"

            result = handler.post_resolution_handler({
                "issue_key": "CHECKOUT-42",
                "failed": True,
                "error": {"Error": "States.TaskFailed", "Cause": "container exit 1"},
                "event": {},
                "classification": {},
            }, None)

        assert result["status"] == "failed"
        assert result["pr_url"] is None
        assert result["notified"] is True

    def test_no_sns_topic_still_succeeds(self):
        mock_s3 = MagicMock()
        output_json = json.dumps({
            "pr_url": "https://github.com/acme/orders/pull/7",
            "status": "success",
        }).encode()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: output_json)}

        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_s3
            handler.SNS_TOPIC_ARN = ""

            result = handler.post_resolution_handler({
                "issue_key": "CHECKOUT-42",
                "output_bucket": "my-bucket",
                "output_key": "resolutions/CHECKOUT-42/output.json",
                "event": {},
                "classification": {},
            }, None)

        assert result["status"] == "success"
        assert result["notified"] is False

    def test_s3_read_failure_sets_status(self):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")

        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_s3
            handler.SNS_TOPIC_ARN = ""

            result = handler.post_resolution_handler({
                "issue_key": "CHECKOUT-42",
                "output_bucket": "my-bucket",
                "output_key": "resolutions/CHECKOUT-42/output.json",
                "event": {},
                "classification": {},
            }, None)

        assert result["status"] == "output_read_failed"
        assert result["pr_url"] is None
