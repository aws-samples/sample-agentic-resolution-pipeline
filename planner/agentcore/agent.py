"""
Resolution Planner Agent — AgentCore Runtime entrypoint.

Plans the fix strategy by querying the knowledge base, resolving the target
repository, browsing the repo tree to identify affected files, and building
an enriched prompt for the Resolution Agent.

Invoked via InvokeAgentRuntime with a payload containing:
    - issue_key, rca_summary, project, component
"""

from __future__ import annotations

import json
import logging
import os

from strands import Agent
from strands.models.bedrock import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from tools.kb_query import query_knowledge_base
from tools.repo_resolver import resolve_repository
from tools.prompt_builder import build_resolution_prompt
from tools.browse_repo import browse_repo_tree

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Resolution Planning Agent for an automated bug-fixing pipeline. Your job is to plan the optimal fix strategy for engineering tickets.

Given a root cause analysis (RCA) and ticket metadata, you MUST execute these steps IN ORDER:

1. QUERY THE KNOWLEDGE BASE: Search for similar past fixes using the error pattern, service name, or technology from the RCA. This provides patterns and gotchas from past resolutions.

2. RESOLVE THE REPOSITORY: Use the project key and component to find the target repository URL and credentials.

3. BROWSE THE REPO TREE: Use browse_repo_tree to explore the actual repository structure. Start from the root to understand the project layout, then navigate into directories matching the RCA context (class names, module paths, stack traces). Confirm which files actually exist before recommending them as targets.

4. BUILD THE RESOLUTION PROMPT: With confirmed file paths, KB context, and your analysis, assemble the final prompt for the Resolution Agent. Include:
   - Which specific files need modification (validated against the real repo tree)
   - What approach to take (null check, config change, refactor, etc.)
   - What edge cases to consider
   - What tests to add or update

IMPORTANT RULES:
- ALWAYS execute all four steps in order. Do NOT skip the KB query or repo browsing.
- When browsing the repo, start broad (root listing) then drill into relevant directories.
- Cross-reference file paths from KB results and RCA stack traces against the actual repo tree.
- If a file mentioned in the RCA doesn't exist, note this — the code may have been refactored.
- Limit repo browsing to at most 8 calls to stay efficient.
- After building the prompt, output your results as a JSON object with these exact keys:
  enriched_prompt, repo_info, kb_context, strategy, target_files

Your final output MUST be a valid JSON object like:
{
  "enriched_prompt": "<the built prompt>",
  "repo_info": {"repo_url": "...", "default_branch": "...", "provider": "...", "auth_secret_arn": "..."},
  "kb_context": "<summarized KB findings>",
  "strategy": "<your fix strategy narrative>",
  "target_files": ["path/to/file1.py", "path/to/file2.py"]
}"""

app = BedrockAgentCoreApp()


def _create_agent():
    """Create the planning agent."""
    import boto3
    session = boto3.Session(region_name=os.getenv("AWS_REGION", "us-east-1"))
    model = BedrockModel(
        model_id=os.getenv("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
        boto_session=session,
    )

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[query_knowledge_base, resolve_repository, build_resolution_prompt, browse_repo_tree],
    )


@app.entrypoint
def invoke(payload):
    """Process a planning request and return structured results."""
    try:
        return _do_invoke(payload)
    except Exception as e:
        logger.exception(f"Planning invocation failed: {e}")
        return json.dumps({"error": str(e), "type": type(e).__name__})


def _do_invoke(payload):
    issue_key = payload.get("issue_key", "UNKNOWN-0")
    rca_summary = payload.get("rca_summary", "")
    project = payload.get("project", "")
    component = payload.get("component", "")

    logger.info(f"Starting planning for {issue_key} (project={project}, component={component})")

    agent = _create_agent()

    user_message = f"""Plan a resolution for issue {issue_key}.

## Root Cause Analysis
{rca_summary}

## Ticket Metadata
- Project: {project}
- Component: {component or 'not specified'}

Please execute all four steps: query KB, resolve repo, browse repo tree, and build the resolution prompt. Output the final result as a JSON object."""

    result = agent(user_message)
    response_text = str(result)

    # Parse structured output from the agent's response
    parsed = _parse_agent_output(response_text)
    parsed.setdefault("issue_key", issue_key)

    logger.info(f"Planning complete for {issue_key}: repo={parsed.get('repo_info', {}).get('repo_url', 'N/A')}")
    return json.dumps(parsed)


def _parse_agent_output(text: str) -> dict:
    """Extract structured JSON from the agent's response."""
    result = {
        "enriched_prompt": "",
        "repo_info": {},
        "kb_context": "",
        "strategy": "",
        "target_files": [],
        "agent_trace": [],
    }

    # Look for JSON block in the response
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
            result["repo_info"] = data["repo_info"]

        if "kb_context" in data:
            result["kb_context"] = data["kb_context"]

        if "strategy" in data:
            result["strategy"] = data["strategy"]

        if "target_files" in data:
            result["target_files"] = data["target_files"]

        return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: use the full response as strategy
    result["enriched_prompt"] = text
    result["strategy"] = text
    return result


if __name__ == "__main__":
    app.run()
