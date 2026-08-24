"""Tests for LLM classifier (Bedrock InvokeModel fallback)."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import llm_classifier


class TestLLMClassify:

    def setup_method(self):
        llm_classifier.LLM_CLASSIFIER_ENABLED = True
        llm_classifier.GUARDRAIL_ID = ""
        llm_classifier.GUARDRAIL_VERSION = ""

    def _make_event(self, title="App crashes", description="NullPointerException on login"):
        return {
            "priority": "MEDIUM",
            "data": {
                "title": title,
                "description": description,
                "metadata": {
                    "issue_type": "Sub-task",
                    "labels": [],
                    "summary": title,
                    "description": description,
                },
            },
        }

    def test_disabled_returns_none(self):
        llm_classifier.LLM_CLASSIFIER_ENABLED = False
        result = llm_classifier.llm_classify(self._make_event())
        assert result is None

    def test_empty_text_returns_none(self):
        event = {"data": {"metadata": {}}}
        result = llm_classifier.llm_classify(event)
        assert result is None

    @patch("boto3.client")
    def test_successful_classification(self, mock_boto):
        mock_client = MagicMock()
        mock_response_body = MagicMock()
        mock_response_body.read.return_value = json.dumps({
            "content": [{"text": '{"category": "BUG_TICKET", "confidence": 0.92, "reasoning": "NPE indicates a code defect"}'}],
        }).encode()
        mock_client.invoke_model.return_value = {"body": mock_response_body}
        mock_boto.return_value = mock_client

        result = llm_classifier.llm_classify(self._make_event())

        assert result["category"] == "BUG_TICKET"
        assert result["confidence"] == 0.92
        assert "NPE" in result["reasoning"]

    @patch("boto3.client")
    def test_low_confidence_returns_none(self, mock_boto):
        mock_client = MagicMock()
        mock_response_body = MagicMock()
        mock_response_body.read.return_value = json.dumps({
            "content": [{"text": '{"category": "BUG_TICKET", "confidence": 0.5, "reasoning": "unclear"}'}],
        }).encode()
        mock_client.invoke_model.return_value = {"body": mock_response_body}
        mock_boto.return_value = mock_client

        result = llm_classifier.llm_classify(self._make_event())
        assert result is None

    @patch("boto3.client")
    def test_invalid_category_returns_none(self, mock_boto):
        mock_client = MagicMock()
        mock_response_body = MagicMock()
        mock_response_body.read.return_value = json.dumps({
            "content": [{"text": '{"category": "UNKNOWN_THING", "confidence": 0.9, "reasoning": "x"}'}],
        }).encode()
        mock_client.invoke_model.return_value = {"body": mock_response_body}
        mock_boto.return_value = mock_client

        result = llm_classifier.llm_classify(self._make_event())
        assert result is None

    @patch("boto3.client")
    def test_malformed_json_returns_none(self, mock_boto):
        mock_client = MagicMock()
        mock_response_body = MagicMock()
        mock_response_body.read.return_value = json.dumps({
            "content": [{"text": "I think this is a bug ticket"}],
        }).encode()
        mock_client.invoke_model.return_value = {"body": mock_response_body}
        mock_boto.return_value = mock_client

        result = llm_classifier.llm_classify(self._make_event())
        assert result is None

    @patch("boto3.client")
    def test_api_error_returns_none(self, mock_boto):
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = Exception("ThrottlingException")
        mock_boto.return_value = mock_client

        result = llm_classifier.llm_classify(self._make_event())
        assert result is None

    @patch("boto3.client")
    def test_guardrail_passed_when_configured(self, mock_boto):
        llm_classifier.GUARDRAIL_ID = "gr-123"
        llm_classifier.GUARDRAIL_VERSION = "1"

        mock_client = MagicMock()
        mock_response_body = MagicMock()
        mock_response_body.read.return_value = json.dumps({
            "content": [{"text": '{"category": "PROD_INCIDENT", "confidence": 0.95, "reasoning": "outage"}'}],
        }).encode()
        mock_client.invoke_model.return_value = {"body": mock_response_body}
        mock_boto.return_value = mock_client

        llm_classifier.llm_classify(self._make_event())

        call_kwargs = mock_client.invoke_model.call_args[1]
        assert call_kwargs["guardrailIdentifier"] == "gr-123"
        assert call_kwargs["guardrailVersion"] == "1"


class TestExtractTicketText:

    def test_full_ticket(self):
        event = {
            "priority": "HIGH",
            "data": {
                "title": "Login broken",
                "description": "Users can't log in since deploy",
                "metadata": {
                    "issue_type": "Bug",
                    "labels": ["urgent", "auth"],
                    "summary": "Login broken",
                    "description": "Users can't log in since deploy",
                },
            },
        }
        text = llm_classifier._extract_ticket_text(event)
        assert "Login broken" in text
        assert "Users can't log in" in text
        assert "HIGH" in text
        assert "urgent" in text

    def test_minimal_ticket(self):
        event = {"data": {"metadata": {}}}
        text = llm_classifier._extract_ticket_text(event)
        assert text == ""
