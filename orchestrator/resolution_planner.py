"""
Resolution Planner — invokes the AgentCore Planner Runtime to plan the fix strategy.

Called by Step Functions after human approval. The agent:
1. Queries the KB for similar past fixes
2. Resolves the target repository
3. Browses the repo tree to identify target files
4. Builds an enriched prompt for the Resolution Agent

Environment variables:
    PLANNER_RUNTIME_ARN — AgentCore Runtime ARN for the Planner
    KB_ID               — Bedrock Knowledge Base ID (used by fallback path)
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AGENTCORE_CONFIG = Config(read_timeout=600, connect_timeout=30, retries={"max_attempts": 2})

PLANNER_RUNTIME_ARN = os.getenv("PLANNER_RUNTIME_ARN", "")


def resolution_planner_handler(event, context):
    """
    Input: full pipeline state (rca_result, classification, event, resolution_approval)
    Output: {enriched_prompt, repo_info, kb_context, strategy, agent_trace}
    """
    classification = event.get("classification", {})
    rca_result = event.get("rca_result", {})
    issue_event = event.get("event", {})
    issue_key = classification.get("issue_key", "unknown")
    rca_summary = rca_result.get("rca_summary", "")

    if not PLANNER_RUNTIME_ARN:
        logger.warning("Planner runtime not configured — falling back to simple planning")
        return _fallback_plan(event)

    metadata = (issue_event.get("data") or {}).get("metadata", {})
    project = metadata.get("project", issue_key.split("-")[0] if "-" in issue_key else "")
    component = metadata.get("component", "")

    payload = {
        "issue_key": issue_key,
        "rca_summary": rca_summary,
        "project": project,
        "component": component,
    }

    result = _invoke_agent(payload, issue_key)

    if result.get("error"):
        logger.error(f"Planner agent failed for {issue_key}: {result['error']}")
        return _fallback_plan(event)

    # Ensure repo_info is always populated
    if not result.get("repo_info") or not result["repo_info"].get("repo_url"):
        from resolvers.config_resolver import ConfigFileResolver
        resolver = ConfigFileResolver()
        repo_info = resolver.resolve(project=project, component=component)
        if repo_info:
            result["repo_info"] = {
                "repo_url": repo_info.repo_url,
                "default_branch": repo_info.branch,
                "provider": repo_info.provider,
                "auth_secret_arn": repo_info.auth_secret_arn or "",
                "path": repo_info.path or "",
            }

    logger.info(f"Planner agent completed for {issue_key}")
    return result


def _invoke_agent(payload: dict, issue_key: str) -> dict:
    """Invoke the AgentCore Planner Runtime and parse the response."""
    client = boto3.client("bedrock-agentcore", config=AGENTCORE_CONFIG)
    session_id = f"planner-{issue_key}-{uuid.uuid4().hex[:8]}".ljust(33, "0")

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=PLANNER_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode(),
            qualifier="DEFAULT",
        )
    except Exception as e:
        return {"error": str(e)}

    # Read the response body
    response_body = response.get("response", b"")
    if isinstance(response_body, bytes):
        response_text = response_body.decode("utf-8")
    elif hasattr(response_body, "read"):
        response_text = response_body.read().decode("utf-8")
    else:
        response_text = str(response_body)

    # Parse structured output from the agent
    parsed = _parse_agent_output(response_text)
    parsed["session_id"] = session_id

    return parsed


def _parse_agent_output(text: str) -> dict:
    """Parse the agent's response to extract structured planning output."""
    result = {
        "enriched_prompt": "",
        "repo_info": {},
        "kb_context": "",
        "strategy": "",
        "agent_trace": [],
    }

    try:
        # Find the outermost JSON object
        start = text.index("{")
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        data = json.loads(text[start:end])

        if "enriched_prompt" in data:
            result["enriched_prompt"] = data["enriched_prompt"]
        elif "prompt" in data:
            result["enriched_prompt"] = data["prompt"]

        if "repo_info" in data:
            repo = data["repo_info"]
            result["repo_info"] = {
                "repo_url": repo.get("repo_url", ""),
                "default_branch": repo.get("default_branch", repo.get("branch", "main")),
                "provider": repo.get("provider", "github"),
                "auth_secret_arn": repo.get("auth_secret_arn", ""),
            }

        if "kb_context" in data:
            result["kb_context"] = data["kb_context"]

        if "strategy" in data:
            result["strategy"] = data["strategy"]

        if "target_files" in data:
            result["target_files"] = data["target_files"]

        return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: use the full text as strategy
    result["enriched_prompt"] = text
    result["strategy"] = text
    return result


def _fallback_plan(event: dict) -> dict:
    """Simple fallback when the planner agent isn't configured or fails."""
    from kb_retrieval import kb_retrieval_handler
    from resolvers.config_resolver import ConfigFileResolver

    classification = event.get("classification", {})
    rca_result = event.get("rca_result", {})
    issue_event = event.get("event", {})
    issue_key = classification.get("issue_key", "unknown")
    rca_summary = rca_result.get("rca_summary", "")
    metadata = (issue_event.get("data") or {}).get("metadata", {})
    project = metadata.get("project", issue_key.split("-")[0] if "-" in issue_key else "")
    component = metadata.get("component")

    # KB retrieval
    kb_result = kb_retrieval_handler(event, None)
    kb_context = kb_result.get("kb_context", "")

    # Repo resolution
    resolver = ConfigFileResolver()
    repo_info = resolver.resolve(project=project, component=component)

    repo_dict = {}
    if repo_info:
        repo_dict = {
            "resolved": True,
            "repo_url": repo_info.repo_url,
            "default_branch": repo_info.branch,
            "provider": repo_info.provider,
            "auth_secret_arn": repo_info.auth_secret_arn or "",
            "path": repo_info.path or "",
        }

    return {
        "enriched_prompt": f"Fix issue {issue_key}. RCA: {rca_summary}",
        "repo_info": repo_dict,
        "kb_context": kb_context,
        "strategy": "Apply targeted fix based on RCA",
        "agent_trace": ["fallback: planner agent not configured or failed"],
        "session_id": "",
    }
