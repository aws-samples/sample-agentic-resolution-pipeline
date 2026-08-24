"""
Policy engine — enforces policy.yaml against every write call BEFORE it reaches Jira.

A denial returns PolicyDenied with a reason. The MCP server translates that to an
MCP error and skips the idempotency write, so a corrected retry can succeed.

Loaded once per cold start; reload by recycling the Lambda or bumping a config version.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

POLICY_PATH = os.getenv("JIRA_POLICY_PATH", str(Path(__file__).parent / "policy.yaml"))
HIGH_PRIORITIES = {"P0", "P1", "Highest", "High", "Critical", "Blocker"}
TERMINAL_TRANSITION_NAMES = {"Done", "Closed", "Resolved", "Resolve", "Close"}

_policy_cache: dict | None = None


class PolicyDenied(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ProjectPolicy:
    allowed_fields: set[str]
    forbidden_fields: set[str]
    allowed_transitions: set[str]
    forbidden_transitions: set[str]
    rules: dict[str, Any]


def _load() -> dict:
    global _policy_cache
    if _policy_cache is not None:
        return _policy_cache
    with open(POLICY_PATH, encoding="utf-8") as f:
        _policy_cache = yaml.safe_load(f)
    return _policy_cache


def _resolve(project_key: str) -> ProjectPolicy:
    cfg = _load()
    defaults = cfg.get("defaults", {})
    project_cfg = cfg.get("projects", {}).get(project_key, {})

    def merged(field: str, default_value: list | dict) -> Any:
        # Project block, if present, fully overrides the defaults for that field.
        return project_cfg.get(field, defaults.get(field, default_value))

    return ProjectPolicy(
        allowed_fields=set(merged("allowed_fields", [])),
        forbidden_fields=set(merged("forbidden_fields", [])),
        allowed_transitions=set(merged("allowed_transitions", [])),
        forbidden_transitions=set(merged("forbidden_transitions", [])),
        rules={**defaults.get("rules", {}), **project_cfg.get("rules", {})},
    )


# ── Public checks ───────────────────────────────────────────────────────────

def check_field_writes(project_key: str, fields: dict) -> None:
    """Validate that every field key in `fields` is permitted for this project."""
    policy = _resolve(project_key)
    for name in fields.keys():
        if name in policy.forbidden_fields:
            raise PolicyDenied(f"field '{name}' is forbidden in project {project_key}")
        if policy.allowed_fields and name not in policy.allowed_fields:
            raise PolicyDenied(f"field '{name}' is not in allowed_fields for project {project_key}")


def check_transition(project_key: str, transition_name: str, current_priority: str | None,
                     comment_provided: bool) -> None:
    """Validate that the named transition is permitted for this project."""
    policy = _resolve(project_key)
    if transition_name in policy.forbidden_transitions:
        raise PolicyDenied(f"transition '{transition_name}' is forbidden in project {project_key}")
    if policy.allowed_transitions and transition_name not in policy.allowed_transitions:
        raise PolicyDenied(f"transition '{transition_name}' is not in allowed_transitions for project {project_key}")

    is_terminal = transition_name in TERMINAL_TRANSITION_NAMES

    if is_terminal and policy.rules.get("never_auto_close_high_priority", False):
        if current_priority and current_priority in HIGH_PRIORITIES:
            raise PolicyDenied(
                f"never_auto_close_high_priority: refusing terminal transition '{transition_name}' "
                f"on {project_key} issue with priority={current_priority}")

    if is_terminal and policy.rules.get("require_comment_on_terminal_transition", False):
        if not comment_provided:
            raise PolicyDenied(
                f"require_comment_on_terminal_transition: '{transition_name}' requires a comment")


def check_bulk_limit(project_key: str, count: int) -> None:
    policy = _resolve(project_key)
    cap = int(policy.rules.get("max_bulk_operations", 0))
    if count > cap:
        raise PolicyDenied(
            f"bulk operation count {count} exceeds max_bulk_operations={cap} for project {project_key}")
