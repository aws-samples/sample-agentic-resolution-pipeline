#!/usr/bin/env python3
"""
Agentic Resolution Pipeline — CDK Entry Point

Stacks:
  JiraIntakeStack    — Jira MCP server + webhook receiver (Lambda, API Gateway, DynamoDB, Secrets Manager)
  OrchestratorStack  — Step Functions orchestrator + classifier/dispatcher Lambdas

Usage:
  cd infrastructure
  pip install -r requirements.txt
  cdk deploy JiraIntakeStack \
    --parameters JiraBaseUrl=https://your-org.atlassian.net \
    --parameters AgentWebhookUrl=https://your-agent-endpoint/webhook

  cdk deploy OrchestratorStack \
    --parameters DevOpsAgentWebhookUrl=https://your-devops-agent/webhook
"""

import os
import aws_cdk as cdk
from stacks.guardrails_stack import GuardrailStack
from stacks.jira_intake_stack import JiraIntakeStack
from stacks.knowledge_base_stack import KnowledgeBaseStack
from stacks.orchestrator_stack import OrchestratorStack
from stacks.resolution_agentcore_stack import ResolutionAgentCoreStack
from stacks.planner_agentcore_stack import PlannerAgentCoreStack
from stacks.resolution_stack import ResolutionStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

GuardrailStack(app, "GuardrailStack", env=env)
JiraIntakeStack(app, "JiraIntakeStack", env=env)
KnowledgeBaseStack(app, "KnowledgeBaseStack", env=env)
PlannerAgentCoreStack(app, "PlannerAgentCoreStack", env=env)
ResolutionAgentCoreStack(app, "ResolutionAgentCoreStack", env=env)
OrchestratorStack(app, "OrchestratorStack", env=env)
ResolutionStack(app, "ResolutionStack", env=env)

app.synth()
