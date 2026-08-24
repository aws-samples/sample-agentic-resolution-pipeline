"""
Jira Intake Stack — MCP server + webhook receiver.

Provisions:
  - JiraMCPFn        : Lambda handling MCP JSON-RPC; SigV4-authed via API Gateway IAM auth.
  - JiraWebhookFn    : Lambda receiving Jira webhooks; HMAC-verified, forwards to agent.
  - REST API         : two routes — POST /mcp (IAM auth), POST /webhook (open, HMAC-verified in code).
  - IdempotencyTable : DynamoDB, PK=idem_key, TTL=expires_at.
  - MetadataCache    : DynamoDB, PK=cache_key, TTL=expires_at.
  - DedupeTable      : DynamoDB for webhook dedupe, PK=dedupe_key, TTL=expires_at.
  - AgentDLQ         : SQS queue for failed agent webhook deliveries.
  - JiraSecret       : Secrets Manager — populated out-of-band with {email, api_token}.
  - WebhookSecret    : Secrets Manager — Jira shared secret for HMAC verify.
  - AgentSecret      : Secrets Manager — secret for signing forwarded events to agent.
"""

import os
import subprocess
import shutil
from pathlib import Path
import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as lambda_,
    aws_apigateway as apigw,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_secretsmanager as sm,
    aws_sqs as sqs,
    CfnParameter,
    CfnOutput,
    Duration,
    RemovalPolicy,
)
from constructs import Construct


def _build_lambda_asset(src_dir: str) -> str:
    """
    Stage the intake/jira/ directory into a build dir with deps installed.
    Runs at synth time. Excludes local-dev/ and tests/ from the package.
    Returns the staged directory path for lambda.Code.from_asset().
    """
    src = Path(src_dir).resolve()
    out = src.parent / "_build" / "jira-lambda"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    skip = {"local-dev", "tests", ".venv", "_build", "diagrams", "__pycache__"}
    for entry in src.iterdir():
        if entry.name in skip:
            continue
        if entry.is_dir():
            shutil.copytree(entry, out / entry.name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(entry, out / entry.name)

    req = out / "requirements.txt"
    if req.exists():
        subprocess.check_call([
            "python3", "-m", "pip", "install",
            "-r", str(req),
            "-t", str(out),
            "--quiet", "--upgrade",
            "--platform", "manylinux2014_x86_64",
            "--only-binary=:all:",
            "--python-version", "3.12",
        ])

    return str(out)


class JiraIntakeStack(cdk.Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Parameters ──────────────────────────────────────────────────────
        jira_base_url = CfnParameter(
            self, "JiraBaseUrl", type="String",
            description="Jira Cloud base URL, e.g. https://your-org.atlassian.net",
            default=self.node.try_get_context("jira_base_url") or "",
        )
        agent_webhook_url = CfnParameter(
            self, "AgentWebhookUrl", type="String",
            description="Agent webhook endpoint that should receive Jira events",
            default=self.node.try_get_context("devops_agent_webhook_url") or "",
        )
        agent_service_name = CfnParameter(
            self, "AgentServiceName", type="String",
            description="Service name tag for forwarded events (identifies the pipeline)",
            default="agentic-resolution-pipeline",
        )
        webhook_filter = CfnParameter(
            self, "WebhookFilter", type="String", default="",
            description="Optional filter, e.g. project=OPS AND priority in (P1,P2). Keys: project, priority, issue_type, status, jira_event.",
        )
        agent_account_ids = CfnParameter(
            self, "JiraAgentAccountIds", type="String", default="",
            description="Comma-separated Jira accountIds the agent writes as. Events authored by these IDs are dropped (self-loop guardrail).",
        )
        dry_run = CfnParameter(
            self, "DryRun", type="String", default="false",
            allowed_values=["true", "false"],
            description="If true, write tools log what they WOULD send to Jira but do not call Jira.",
        )
        orchestrator_sm_arn = CfnParameter(
            self, "OrchestratorStateMachineArn", type="String",
            default=f"arn:aws:states:{self.region}:{self.account}:stateMachine:agentic-pipeline-orchestrator",
            description="Orchestrator Step Functions ARN. When set, webhook starts executions here instead of forwarding directly to agent.",
        )
        task_token_table_name = CfnParameter(
            self, "TaskTokenTableName", type="String", default="agentic-pipeline-task-tokens",
            description="DynamoDB table name for Step Functions task tokens (callback pattern).",
        )
        repo_allowlist = CfnParameter(
            self, "RepoAllowlist", type="String",
            default="",
            description="Comma-separated repo URLs that are allowed to trigger KB ingestion on PR merge.",
        )

        # ── Secrets ─────────────────────────────────────────────────────────
        jira_secret = sm.Secret(
            self, "JiraApiSecret",
            description="Jira service account creds: {email, api_token}",
        )
        webhook_secret = sm.Secret(
            self, "JiraWebhookSecret",
            description="Shared secret Jira uses to HMAC-sign webhooks",
        )
        agent_secret = sm.Secret(
            self, "AgentWebhookSecret",
            description="Secret used to sign events forwarded to the agent",
        )

        # ── DynamoDB ────────────────────────────────────────────────────────
        idem_table = ddb.Table(
            self, "IdempotencyTable",
            partition_key=ddb.Attribute(name="idem_key", type=ddb.AttributeType.STRING),
            time_to_live_attribute="expires_at",
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        cache_table = ddb.Table(
            self, "MetadataCacheTable",
            partition_key=ddb.Attribute(name="cache_key", type=ddb.AttributeType.STRING),
            time_to_live_attribute="expires_at",
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        dedupe_table = ddb.Table(
            self, "WebhookDedupeTable",
            partition_key=ddb.Attribute(name="dedupe_key", type=ddb.AttributeType.STRING),
            time_to_live_attribute="expires_at",
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── DLQ ─────────────────────────────────────────────────────────────
        dlq = sqs.Queue(
            self, "AgentForwardDLQ",
            retention_period=Duration.days(14),
        )

        # ── Lambdas ─────────────────────────────────────────────────────────
        src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "intake", "jira")
        code_path = _build_lambda_asset(src_dir)

        mcp_fn = lambda_.Function(
            self, "JiraMCPFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="server.lambda_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                "JIRA_BASE_URL": jira_base_url.value_as_string,
                "JIRA_SECRET_ARN": jira_secret.secret_arn,
                "JIRA_IDEMPOTENCY_TABLE": idem_table.table_name,
                "JIRA_METADATA_CACHE_TABLE": cache_table.table_name,
                "JIRA_POLICY_PATH": "/var/task/policy.yaml",
                "JIRA_MCP_DRY_RUN": dry_run.value_as_string,
            },
        )
        jira_secret.grant_read(mcp_fn)
        idem_table.grant_read_write_data(mcp_fn)
        cache_table.grant_read_write_data(mcp_fn)

        webhook_fn = lambda_.Function(
            self, "JiraWebhookFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="webhook_receiver.lambda_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                "JIRA_WEBHOOK_SECRET_ARN": webhook_secret.secret_arn,
                "AGENT_WEBHOOK_URL": self.node.try_get_context("devops_agent_webhook_url") or agent_webhook_url.value_as_string,
                "AGENT_WEBHOOK_SECRET_ARN": agent_secret.secret_arn,
                "AGENT_DLQ_URL": dlq.queue_url,
                "JIRA_WEBHOOK_DEDUPE_TABLE": dedupe_table.table_name,
                "JIRA_WEBHOOK_FILTER": webhook_filter.value_as_string,
                "JIRA_AGENT_ACCOUNT_IDS": agent_account_ids.value_as_string,
                "JIRA_AGENT_COMMENT_MARKER": "[Agent]",
                "AGENT_SERVICE_NAME": agent_service_name.value_as_string,
                "ORCHESTRATOR_STATE_MACHINE_ARN": f"arn:aws:states:{self.region}:{self.account}:stateMachine:agentic-pipeline-orchestrator",
                "TASK_TOKEN_TABLE": task_token_table_name.value_as_string,
            },
        )
        webhook_secret.grant_read(webhook_fn)
        agent_secret.grant_read(webhook_fn)
        dedupe_table.grant_read_write_data(webhook_fn)
        dlq.grant_send_messages(webhook_fn)

        # Grant webhook Lambda permission to start orchestrator executions and
        # resume paused executions via SendTaskSuccess (callback pattern for RCA/approval).
        webhook_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:StartExecution", "states:SendTaskSuccess"],
            resources=[f"arn:aws:states:{self.region}:{self.account}:stateMachine:agentic-pipeline-*"],
        ))

        # Grant webhook Lambda access to task token table for callback lookups.
        webhook_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:DeleteItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/agentic-pipeline-task-tokens"],
        ))

        # ── API Gateway ─────────────────────────────────────────────────────
        api = apigw.RestApi(
            self, "JiraIntakeApi",
            rest_api_name="jira-intake",
            description="Jira intake — MCP endpoint (IAM-authed) and webhook receiver (HMAC-verified)",
        )

        mcp_resource = api.root.add_resource("mcp")
        for verb in ("POST", "GET", "DELETE"):
            mcp_resource.add_method(
                verb,
                apigw.LambdaIntegration(mcp_fn),
                authorization_type=apigw.AuthorizationType.IAM,
            )

        webhook_resource = api.root.add_resource("webhook")
        webhook_resource.add_method(
            "POST",
            apigw.LambdaIntegration(webhook_fn),
            authorization_type=apigw.AuthorizationType.NONE,
        )

        # ── PR Merge Webhook (Bitbucket pullrequest:fulfilled) ─────────────
        pr_merge_fn = lambda_.Function(
            self, "PRMergeHandlerFn",
            function_name="agentic-pipeline-pr-merge-handler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="pr_merge_handler.lambda_handler",
            code=lambda_.Code.from_asset(
                str(Path(os.path.dirname(__file__)).parent.parent / "intake" / "bitbucket")
            ),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "RESOLUTION_OUTPUT_BUCKET": f"agentic-pipeline-resolution-output-{self.account}-{self.region}",
                "KB_INGESTION_FUNCTION": "agentic-pipeline-kb-ingestion",
                "REPO_ALLOWLIST": repo_allowlist.value_as_string,
            },
        )
        pr_merge_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::agentic-pipeline-resolution-output-{self.account}-{self.region}/resolutions/*"],
        ))
        pr_merge_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:agentic-pipeline-kb-ingestion"],
        ))

        pr_merge_resource = api.root.add_resource("pr-merge")
        pr_merge_resource.add_method(
            "POST",
            apigw.LambdaIntegration(pr_merge_fn),
            authorization_type=apigw.AuthorizationType.NONE,
        )

        # ── Outputs ─────────────────────────────────────────────────────────
        CfnOutput(self, "MCPEndpointUrl", value=f"{api.url}mcp",
                  description="Jira MCP endpoint — caller signs requests with SigV4 (IAM)")
        CfnOutput(self, "WebhookEndpointUrl", value=f"{api.url}webhook",
                  description="Configure as the URL on Jira's webhook setup page")
        CfnOutput(self, "PRMergeEndpointUrl", value=f"{api.url}pr-merge",
                  description="Configure as Bitbucket webhook URL for pullrequest:fulfilled events")
        CfnOutput(self, "JiraSecretArn", value=jira_secret.secret_arn,
                  description="Populate with: {\"email\": \"...\", \"api_token\": \"...\"}")
        CfnOutput(self, "JiraWebhookSecretArn", value=webhook_secret.secret_arn,
                  description="Set this value as Jira's webhook 'Secret' field for HMAC")
        CfnOutput(self, "AgentWebhookSecretArn", value=agent_secret.secret_arn,
                  description="Shared with the agent webhook; used to verify forwarded events")
        CfnOutput(self, "AgentDLQUrl", value=dlq.queue_url,
                  description="Dead-letter queue for failed agent forwards")
        CfnOutput(self, "InvokePolicyHint",
                  value=f"To allow the agent role to call /mcp: execute-api:Invoke on arn:aws:execute-api:{self.region}:{self.account}:{api.rest_api_id}/*/POST/mcp",
                  description="IAM permission the agent role needs")
