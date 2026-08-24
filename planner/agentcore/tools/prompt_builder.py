"""Strands tool: Build the enriched resolution prompt for the code-fixing agent."""

from __future__ import annotations

import logging

from strands import tool

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """You are an expert software engineer. Fix the following issue by modifying the code in this repository.

## Issue
{issue_key}

## Root Cause Analysis
{rca_summary}

## Fix Strategy
{strategy}

## Similar Past Fixes (from Knowledge Base)
{kb_context}

## Target Files
{target_files}

## Instructions
1. Analyze the root cause and the suggested strategy
2. Start with the target files identified above
3. Write the fix, ensuring:
   - The fix addresses the root cause, not just the symptoms
   - Add or update tests to cover the fix
   - Follow existing code style and patterns
   - Do not introduce new dependencies unless absolutely necessary
4. Create a new branch named `fix/{issue_key}`
5. Commit with a clear message referencing the issue
6. Push the branch and create a Pull Request with:
   - Title: "fix({component}): {short_description}"
   - Body: Summary of changes, root cause, and test coverage

## Repository
{repo_url}

## Safety Rules
- NEVER force push
- NEVER modify CI/CD pipelines or deployment configs
- NEVER commit secrets, credentials, or API keys
- If tests fail, iterate on the fix before creating the PR

After creating the PR, output the URL in this exact format:
PR_URL=<the full PR URL>
"""


@tool
def build_resolution_prompt(
    rca_summary: str,
    strategy: str,
    kb_context: str = "",
    issue_key: str = "",
    repo_url: str = "",
    target_files: str = "",
) -> dict:
    """Assemble the final enriched prompt for the code-fixing Resolution Agent.

    Call this LAST, after you have queried the KB, resolved the repo, and browsed
    the repo tree to identify target files.

    Args:
        rca_summary: The root cause analysis summary.
        strategy: Your recommended fix strategy (approach, edge cases, testing).
        kb_context: Relevant past fixes from the knowledge base.
        issue_key: Jira issue key (e.g., 'CHECKOUT-42').
        repo_url: Repository URL.
        target_files: Comma-separated list of file paths confirmed via repo browsing.

    Returns:
        dict with 'prompt' (the assembled prompt) and 'token_estimate'.
    """
    if not rca_summary:
        return {"error": "rca_summary is required"}

    component = issue_key.split("-")[0].lower() if "-" in issue_key else "fix"

    prompt = PROMPT_TEMPLATE.format(
        issue_key=issue_key,
        rca_summary=rca_summary,
        strategy=strategy,
        kb_context=kb_context or "No similar past fixes available.",
        repo_url=repo_url,
        component=component,
        short_description="address root cause from RCA",
        target_files=target_files or "Not specified — investigate the repository.",
    )

    token_estimate = len(prompt.split()) * 2

    logger.info(f"Built resolution prompt for {issue_key} (~{token_estimate} tokens)")

    return {
        "prompt": prompt,
        "token_estimate": token_estimate,
    }
