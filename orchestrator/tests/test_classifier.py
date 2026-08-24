"""Tests for the hybrid event classifier (rules + LLM fallback)."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from classifier import TriageCategory, classify, _rule_based_classify


def _make_event(issue_type="Bug", priority="MEDIUM", labels=None, description=""):
    return {
        "eventType": "incident",
        "incidentId": "jira-TEST-1-jira:issue_created",
        "action": "created",
        "priority": priority,
        "title": "[TEST-1] Test issue",
        "description": description,
        "timestamp": "2026-07-01T00:00:00+00:00",
        "service": "test-service",
        "data": {
            "title": "Test issue",
            "description": description,
            "metadata": {
                "source": "jira",
                "jira_event": "jira:issue_created",
                "issue_key": "TEST-1",
                "project": "TEST",
                "issue_type": issue_type,
                "status": "Open",
                "priority_raw": "Medium",
                "labels": labels or [],
                "description": description,
            }
        },
    }


class TestRuleBasedClassify:
    def test_incident_type(self):
        event = _make_event(issue_type="Incident", priority="HIGH")
        assert _rule_based_classify(event) == TriageCategory.PROD_INCIDENT

    def test_high_priority_bug(self):
        event = _make_event(issue_type="Bug", priority="HIGH")
        assert _rule_based_classify(event) == TriageCategory.PROD_INCIDENT

    def test_problem_type(self):
        event = _make_event(issue_type="Problem")
        assert _rule_based_classify(event) == TriageCategory.PROD_INCIDENT

    def test_bug_type_medium_priority(self):
        event = _make_event(issue_type="Bug", priority="MEDIUM")
        assert _rule_based_classify(event) == TriageCategory.BUG_TICKET

    def test_defect_type(self):
        event = _make_event(issue_type="Defect", priority="LOW")
        assert _rule_based_classify(event) == TriageCategory.BUG_TICKET

    def test_story_type(self):
        event = _make_event(issue_type="Story")
        assert _rule_based_classify(event) == TriageCategory.FEATURE_REQUEST

    def test_task_type(self):
        event = _make_event(issue_type="Task")
        assert _rule_based_classify(event) == TriageCategory.FEATURE_REQUEST

    def test_feature_request_label(self):
        event = _make_event(issue_type="Other", labels=["feature-request"])
        assert _rule_based_classify(event) == TriageCategory.FEATURE_REQUEST

    def test_data_fix_label(self):
        event = _make_event(issue_type="Bug", priority="HIGH", labels=["data-fix"])
        assert _rule_based_classify(event) == TriageCategory.DATA_QUALITY

    def test_data_quality_label_overrides_priority(self):
        event = _make_event(issue_type="Incident", priority="HIGH", labels=["data-quality"])
        assert _rule_based_classify(event) == TriageCategory.DATA_QUALITY

    def test_unknown_type_no_labels(self):
        event = _make_event(issue_type="Sub-task", priority="MEDIUM")
        assert _rule_based_classify(event) == TriageCategory.NOISE

    def test_empty_type(self):
        event = _make_event(issue_type="", priority="LOW")
        assert _rule_based_classify(event) == TriageCategory.NOISE


class TestHybridClassify:
    def test_rules_match_returns_rules_source(self):
        event = _make_event(issue_type="Bug", priority="MEDIUM")
        category, meta = classify(event)
        assert category == TriageCategory.BUG_TICKET
        assert meta["source"] == "rules"
        assert meta["confidence"] == 1.0

    @patch("llm_classifier.llm_classify")
    def test_noise_falls_back_to_llm(self, mock_llm):
        mock_llm.return_value = {
            "category": "BUG_TICKET",
            "confidence": 0.85,
            "reasoning": "Description indicates a crash bug",
        }
        event = _make_event(
            issue_type="Sub-task",
            description="App crashes on login with NullPointerException",
        )
        category, meta = classify(event)
        assert category == TriageCategory.BUG_TICKET
        assert meta["source"] == "llm"
        assert meta["confidence"] == 0.85
        mock_llm.assert_called_once()

    @patch("llm_classifier.llm_classify")
    def test_llm_returns_none_stays_noise(self, mock_llm):
        mock_llm.return_value = None
        event = _make_event(issue_type="Sub-task")
        category, meta = classify(event)
        assert category == TriageCategory.NOISE
        assert meta["source"] == "rules"

    @patch("llm_classifier.llm_classify")
    def test_rules_match_skips_llm(self, mock_llm):
        event = _make_event(issue_type="Incident", priority="HIGH")
        category, meta = classify(event)
        assert category == TriageCategory.PROD_INCIDENT
        mock_llm.assert_not_called()
