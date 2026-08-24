"""Tests for guardrails utility."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import guardrails


class TestApplyGuardrail:

    def setup_method(self):
        guardrails.GUARDRAIL_ID = "test-guardrail-id"
        guardrails.GUARDRAIL_VERSION = "1"

    def test_no_guardrail_configured_passes_through(self):
        guardrails.GUARDRAIL_ID = ""
        guardrails.GUARDRAIL_VERSION = ""

        result = guardrails.apply_input_guardrail("some text")

        assert result["blocked"] is False
        assert result["output"] == "some text"
        assert result["action"] == "NONE"

    def test_empty_text_passes_through(self):
        result = guardrails.apply_input_guardrail("")
        assert result["blocked"] is False
        assert result["output"] == ""

    @patch("boto3.client")
    def test_clean_input_passes(self, mock_boto):
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            "action": "NONE",
            "outputs": [{"text": "normal ticket description"}],
            "assessments": [],
        }
        mock_boto.return_value = mock_client

        result = guardrails.apply_input_guardrail("normal ticket description")

        assert result["blocked"] is False
        assert result["output"] == "normal ticket description"
        assert result["violations"] == []
        mock_client.apply_guardrail.assert_called_once()

    @patch("boto3.client")
    def test_pii_anonymized_in_output(self, mock_boto):
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": "Contact {EMAIL} for help"}],
            "assessments": [
                {
                    "sensitiveInformationPolicy": {
                        "piiEntities": [
                            {"type": "EMAIL", "action": "ANONYMIZED"},
                        ],
                        "regexes": [],
                    },
                }
            ],
        }
        mock_boto.return_value = mock_client

        result = guardrails.apply_output_guardrail("Contact user@example.com for help")

        assert result["blocked"] is False
        assert result["output"] == "Contact {EMAIL} for help"
        assert len(result["violations"]) == 1
        assert result["violations"][0]["type"] == "pii"
        assert result["violations"][0]["entity_type"] == "EMAIL"

    @patch("boto3.client")
    def test_secret_blocked(self, mock_boto):
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [],
            "assessments": [
                {
                    "sensitiveInformationPolicy": {
                        "piiEntities": [
                            {"type": "AWS_ACCESS_KEY", "action": "BLOCKED"},
                        ],
                        "regexes": [],
                    },
                }
            ],
        }
        mock_boto.return_value = mock_client

        result = guardrails.apply_output_guardrail("key=AKIAIOSFODNN7EXAMPLE")

        assert result["blocked"] is True
        assert result["action"] == "GUARDRAIL_INTERVENED"
        assert result["violations"][0]["entity_type"] == "AWS_ACCESS_KEY"

    @patch("boto3.client")
    def test_denied_topic_blocked(self, mock_boto):
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [],
            "assessments": [
                {
                    "topicPolicy": {
                        "topics": [
                            {"name": "malware-generation", "action": "BLOCKED"},
                        ],
                    },
                }
            ],
        }
        mock_boto.return_value = mock_client

        result = guardrails.apply_input_guardrail("generate a ransomware payload")

        assert result["blocked"] is True
        assert result["violations"][0]["type"] == "denied_topic"
        assert result["violations"][0]["name"] == "malware-generation"

    @patch("boto3.client")
    def test_api_error_passes_through(self, mock_boto):
        mock_client = MagicMock()
        mock_client.apply_guardrail.side_effect = Exception("ServiceUnavailable")
        mock_boto.return_value = mock_client

        result = guardrails.apply_input_guardrail("some text")

        assert result["blocked"] is False
        assert result["output"] == "some text"
        assert result["action"] == "ERROR"

    @patch("boto3.client")
    def test_word_filter_violation(self, mock_boto):
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [],
            "assessments": [
                {
                    "wordPolicy": {
                        "customWords": [
                            {"match": "AKIA", "action": "BLOCKED"},
                        ],
                        "managedWordLists": [],
                    },
                }
            ],
        }
        mock_boto.return_value = mock_client

        result = guardrails.apply_output_guardrail("AKIAIOSFODNN7EXAMPLE")

        assert result["blocked"] is True
        assert result["violations"][0]["type"] == "word_filter"
