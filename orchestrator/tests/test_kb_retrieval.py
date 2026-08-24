"""Tests for kb_retrieval_handler."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import kb_retrieval


class TestKBRetrievalHandler:

    def _make_event(self, rca_summary="NullPointerException in OrderService", issue_key="CHECKOUT-42"):
        return {
            "classification": {"issue_key": issue_key},
            "rca_result": {"rca_summary": rca_summary},
            "event": {},
        }

    def test_no_rca_summary_returns_early(self):
        event = {"classification": {"issue_key": "X-1"}, "rca_result": {}, "event": {}}
        result = kb_retrieval.kb_retrieval_handler(event, None)

        assert "No RCA summary" in result["kb_context"]
        assert result["sources"] == []

    def test_no_kb_id_returns_placeholder(self):
        kb_retrieval.KB_ID = ""
        result = kb_retrieval.kb_retrieval_handler(self._make_event(), None)

        assert "not configured" in result["kb_context"]
        assert result["query"] == "NullPointerException in OrderService"

    def test_successful_kb_query(self):
        kb_retrieval.KB_ID = "kb-12345"
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Fixed by adding null check in OrderService.java line 42"},
                    "score": 0.92,
                    "location": {"s3Location": {"uri": "s3://kb-bucket/fixes/fix-001.md"}},
                },
                {
                    "content": {"text": "Similar NPE resolved by initializing the order list in constructor"},
                    "score": 0.85,
                    "location": {"s3Location": {"uri": "s3://kb-bucket/fixes/fix-002.md"}},
                },
            ]
        }

        with patch("boto3.client", return_value=mock_client):
            result = kb_retrieval.kb_retrieval_handler(self._make_event(), None)

        assert "Similar Fix #1" in result["kb_context"]
        assert "Similar Fix #2" in result["kb_context"]
        assert "null check" in result["kb_context"]
        assert len(result["sources"]) == 2
        assert result["sources"][0]["score"] == 0.92
        mock_client.retrieve.assert_called_once()

    def test_kb_query_failure_returns_error_context(self):
        kb_retrieval.KB_ID = "kb-12345"
        mock_client = MagicMock()
        mock_client.retrieve.side_effect = Exception("AccessDenied")

        with patch("boto3.client", return_value=mock_client):
            result = kb_retrieval.kb_retrieval_handler(self._make_event(), None)

        assert "query failed" in result["kb_context"]
        assert result["sources"] == []

    def test_kb_query_no_results(self):
        kb_retrieval.KB_ID = "kb-12345"
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with patch("boto3.client", return_value=mock_client):
            result = kb_retrieval.kb_retrieval_handler(self._make_event(), None)

        assert "No similar past fixes" in result["kb_context"]
        assert result["sources"] == []
