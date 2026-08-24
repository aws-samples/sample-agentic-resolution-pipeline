"""Tests for kb_ingestion handler."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import kb_ingestion


class TestIngestionHandler:

    def _make_event(self, **overrides):
        base = {
            "issue_key": "CHECKOUT-42",
            "rca_summary": "NullPointerException in OrderService.processOrder() due to uninitialized cart list",
            "resolution_summary": "Added null check and initialized empty list in constructor. Added unit test.",
            "pr_url": "https://bitbucket.org/acme/order-service/pull-requests/123",
            "repo_url": "https://bitbucket.org/acme/order-service.git",
            "files_changed": ["src/main/java/OrderService.java", "src/test/java/OrderServiceTest.java"],
            "category": "PROD_INCIDENT",
            "merged_at": "2026-07-20T14:30:00Z",
            "metadata": {
                "service": "order-service",
                "component": "checkout",
                "error_pattern": "java.lang.NullPointerException at OrderService.processOrder(OrderService.java:42)",
            },
        }
        base.update(overrides)
        return base

    def test_no_bucket_configured(self):
        kb_ingestion.KB_DATA_BUCKET = ""
        result = kb_ingestion.ingestion_handler(self._make_event(), None)
        assert result["ingested"] is False
        assert "KB_DATA_BUCKET" in result["error"]

    def test_no_resolution_summary_skips(self):
        kb_ingestion.KB_DATA_BUCKET = "test-bucket"
        result = kb_ingestion.ingestion_handler(
            self._make_event(resolution_summary=""), None
        )
        assert result["ingested"] is False
        assert "no resolution summary" in result["error"]

    @patch("boto3.client")
    def test_successful_ingestion(self, mock_boto_client):
        kb_ingestion.KB_DATA_BUCKET = "test-bucket"
        kb_ingestion.KB_ID = "kb-12345"
        kb_ingestion.DATA_SOURCE_ID = "ds-67890"

        mock_s3 = MagicMock()
        mock_bedrock = MagicMock()
        mock_bedrock.start_ingestion_job.return_value = {
            "ingestionJob": {"ingestionJobId": "job-abc"}
        }

        def client_factory(service):
            if service == "s3":
                return mock_s3
            if service == "bedrock-agent":
                return mock_bedrock
            return MagicMock()

        mock_boto_client.side_effect = client_factory

        result = kb_ingestion.ingestion_handler(self._make_event(), None)

        assert result["ingested"] is True
        assert result["issue_key"] == "CHECKOUT-42"
        assert result["sync_job_id"] == "job-abc"
        assert "resolutions/CHECKOUT-42/" in result["s3_key"]

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert "CHECKOUT-42" in call_kwargs["Key"]
        assert "NullPointerException" in call_kwargs["Body"].decode()

        mock_bedrock.start_ingestion_job.assert_called_once_with(
            knowledgeBaseId="kb-12345",
            dataSourceId="ds-67890",
        )

    @patch("boto3.client")
    def test_sync_failure_still_ingests(self, mock_boto_client):
        kb_ingestion.KB_DATA_BUCKET = "test-bucket"
        kb_ingestion.KB_ID = "kb-12345"
        kb_ingestion.DATA_SOURCE_ID = "ds-67890"

        mock_s3 = MagicMock()
        mock_bedrock = MagicMock()
        mock_bedrock.start_ingestion_job.side_effect = Exception("ThrottlingException")

        def client_factory(service):
            if service == "s3":
                return mock_s3
            if service == "bedrock-agent":
                return mock_bedrock
            return MagicMock()

        mock_boto_client.side_effect = client_factory

        result = kb_ingestion.ingestion_handler(self._make_event(), None)

        assert result["ingested"] is True
        assert result["sync_job_id"] == ""
        mock_s3.put_object.assert_called_once()

    @patch("boto3.client")
    def test_no_sync_when_ids_missing(self, mock_boto_client):
        kb_ingestion.KB_DATA_BUCKET = "test-bucket"
        kb_ingestion.KB_ID = ""
        kb_ingestion.DATA_SOURCE_ID = ""

        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        result = kb_ingestion.ingestion_handler(self._make_event(), None)

        assert result["ingested"] is True
        assert result["sync_job_id"] == ""


class TestBuildDocument:

    def test_document_structure(self):
        event = {
            "issue_key": "API-99",
            "rca_summary": "Timeout due to missing connection pool limit",
            "resolution_summary": "Added maxPoolSize=50 to connection config",
            "pr_url": "https://github.com/acme/api/pull/99",
            "repo_url": "https://github.com/acme/api.git",
            "category": "BUG_TICKET",
            "merged_at": "2026-07-20T10:00:00Z",
            "files_changed": ["config/database.yaml"],
            "metadata": {
                "service": "api-gateway",
                "component": "connection-pool",
                "error_pattern": "ConnectionTimeoutException after 30s",
            },
        }

        doc = kb_ingestion._build_document(event)

        assert "# Resolution: API-99" in doc
        assert "**Category:** BUG_TICKET" in doc
        assert "**Service:** api-gateway" in doc
        assert "Timeout due to missing connection pool limit" in doc
        assert "Added maxPoolSize=50" in doc
        assert "ConnectionTimeoutException" in doc
        assert "`config/database.yaml`" in doc

    def test_minimal_document(self):
        event = {
            "issue_key": "X-1",
            "resolution_summary": "Fixed the bug",
            "metadata": {},
        }

        doc = kb_ingestion._build_document(event)

        assert "# Resolution: X-1" in doc
        assert "Fixed the bug" in doc
        assert "**Service:**" not in doc
