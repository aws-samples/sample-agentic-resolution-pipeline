"""
Resolution Agent — AgentCore Runtime entrypoint.

Receives a resolution task from the pipeline orchestrator, reasons about
the fix approach, then executes git operations and code changes.

Invoked via InvokeAgentRuntime with a payload containing:
    - issue_key, rca_summary, kb_context, repo_url, branch,
      git_provider, auth_secret_arn, fix_strategy, output_key
"""

from __future__ import annotations

import json
import logging
import os

from strands import Agent
from strands.models.bedrock import BedrockModel
from strands_tools import shell, file_read, file_write, editor
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert software engineer working as an autonomous resolution agent.
Your job is to fix engineering issues by modifying code in a repository.

You will receive:
- A root cause analysis (RCA) explaining what went wrong
- Context from similar past fixes in the knowledge base
- A fix strategy from the planning agent
- Repository and authentication details

Your workflow:
1. Understand the RCA and fix strategy
2. The repository is already cloned at /workspace/repo with git credentials configured
3. Analyze the codebase to find affected files
4. Write the fix following existing patterns
5. Add or update tests
6. Commit, push to a fix branch, and create a Pull Request
7. Report the PR URL

Safety rules:
- NEVER force push
- NEVER modify CI/CD pipelines or deployment configs
- NEVER commit secrets or API keys
- If tests fail, iterate up to 2 times then create the PR anyway with a note
- Always create a new branch named fix/{issue_key}

Efficiency rules:
- Start with the files identified in the fix strategy. If the fix requires changes to additional files (imports, interfaces, configs), make those too — but don't refactor unrelated code.
- Make the minimal fix that resolves the issue.
- If the branch fix/{issue_key} already exists, delete it and start fresh.
- Limit yourself to at most 20 tool calls total. If you haven't finished by then, commit what you have.

Early exit rules:
- If the bug described in the RCA has already been fixed in the code (the line already has the correct logic), report this immediately with PR_URL=ALREADY_FIXED and stop. Do not keep trying.
- If after 10 tool calls you cannot identify how to fix the issue, commit what you have with a descriptive message and create the PR as a draft/WIP.

## Creating Pull Requests

For Bitbucket: read credentials from /workspace/.api-credentials (sourced as shell vars).
Then create the PR with:

```bash
source /workspace/.api-credentials
curl -s -X POST "https://api.bitbucket.org/2.0/repositories/${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO}/pullrequests" \
  -u "${BITBUCKET_EMAIL}:${BITBUCKET_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"title": "<PR title>", "source": {"branch": {"name": "fix/<issue_key>"}}, "destination": {"branch": {"name": "main"}}}'
```

The response JSON contains the PR URL in the "links.html.href" field.

After creating the PR, include this exact line in your final response:
PR_URL=<the full PR URL>"""

app = BedrockAgentCoreApp()


def _create_agent(session_id: str = None, actor_id: str = "resolution-agent"):
    """Lazy-init the agent with optional Memory session manager."""
    import boto3
    session = boto3.Session(region_name=os.getenv("AWS_REGION", "us-east-1"))
    model = BedrockModel(
        model_id=os.getenv("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
        boto_session=session,
    )

    session_manager = None
    memory_id = os.getenv("MEMORY_ID")
    if memory_id and session_id:
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig

        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=actor_id,
            batch_size=1,
            retrieval_config={
                "/strategies/": RetrievalConfig(top_k=10, relevance_score=0.5),
            },
        )
        session_manager = AgentCoreMemorySessionManager(config, region_name=os.getenv("AWS_REGION", "us-east-1"))
        logger.info(f"Memory enabled: memory_id={memory_id}, session_id={session_id}")

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[shell, file_read, file_write, editor],
        session_manager=session_manager,
    )


@app.entrypoint
def invoke(payload):
    """Process a resolution request and return the result."""
    try:
        return _do_invoke(payload)
    except Exception as e:
        logger.exception(f"Invocation failed: {e}")
        return json.dumps({"error": str(e), "type": type(e).__name__})


def _do_invoke(payload):
    issue_key = payload.get("issue_key", "UNKNOWN-0")
    rca_summary = payload.get("rca_summary", "")
    kb_context = payload.get("kb_context", "No similar past fixes available.")
    repo_url = payload.get("repo_url", "")
    branch = payload.get("branch", "main")
    fix_strategy = payload.get("fix_strategy", "")
    git_provider = payload.get("git_provider", "github")

    logger.info(f"Starting resolution for {issue_key}")
    session_id = f"resolution-{issue_key}"
    agent = _create_agent(session_id=session_id, actor_id="resolution-agent")

    user_message = f"""Fix issue {issue_key}.

## Root Cause Analysis
{rca_summary}

## Fix Strategy (from planning agent)
{fix_strategy}

## Similar Past Fixes
{kb_context}

## Repository Details
- URL: {repo_url}
- Base branch: {branch}
- Provider: {git_provider}
- Target branch: fix/{issue_key}

The repository has already been cloned to /workspace/repo. Git credentials are configured.
Please analyze the code, write the fix, run tests, and create a Pull Request."""

    result = agent(user_message)
    response_text = str(result)

    # Extract PR URL from agent response
    pr_url = ""
    import re
    for line in response_text.split("\n"):
        if line.strip().startswith("PR_URL="):
            pr_url = line.strip().split("=", 1)[1].strip()
            break
    if not pr_url:
        # Fallback: find Bitbucket PR URL pattern in the response
        match = re.search(r'https://bitbucket\.org/[^/]+/[^/]+/pull-requests/\d+', response_text)
        if match:
            pr_url = match.group(0)

    output = {
        "issue_key": issue_key,
        "pr_url": pr_url,
        "status": "success" if pr_url else "no_pr_created",
        "agent_response_length": len(response_text),
    }

    logger.info(f"Resolution complete for {issue_key}: pr_url={pr_url}")
    return json.dumps(output)


if __name__ == "__main__":
    app.run()
