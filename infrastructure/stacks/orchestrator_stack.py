"""
Orchestrator Stack — Step Functions state machine + classifier/dispatcher Lambdas.

Provisions:
  - ClassifyFn           : Lambda that classifies normalized events into triage categories
  - DispatchFn           : Lambda that routes events to the DevOps Agent
  - NoiseFn              : Lambda that handles NOISE events (log + future auto-close)
  - StoreTokenAndNotifyFn: Lambda that stores task tokens + sends SNS notifications
  - OrchestratorSM       : Step Functions state machine wiring classify → route → dispatch → wait → approve
  - TaskTokenTable       : DynamoDB for storing Step Functions task tokens (callback pattern)
  - NotificationTopic    : SNS topic for human notifications (RCA ready, approval needed)
  - DevOpsAgentSecret    : Secrets Manager — secret for signing events to DevOps Agent

Namespaced with "agentic-pipeline-" prefix to avoid collisions with the
walk-along deployment in the same account.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as lambda_,
    aws_stepfunctions as sfn,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_secretsmanager as sm,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
)
from constructs import Construct


def _build_orchestrator_asset(src_dir: str) -> str:
    """Stage the orchestrator/ directory into a build dir for Lambda packaging."""
    src = Path(src_dir).resolve()
    out = src.parent / "_build" / "orchestrator-lambda"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    skip = {"tests", "_build", "__pycache__"}
    for entry in src.iterdir():
        if entry.name.startswith(("_", ".")):
            continue
        if entry.name in skip:
            continue
        if entry.suffix == ".json" and entry.name == "state_machine.asl.json":
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


class OrchestratorStack(cdk.Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Parameters ──────────────────────────────────────────────────────
        devops_agent_url = CfnParameter(
            self, "DevOpsAgentWebhookUrl", type="String",
            description="DevOps Agent webhook endpoint for investigation dispatch",
            default=self.node.try_get_context("devops_agent_webhook_url") or "",
        )
        notification_email = CfnParameter(
            self, "NotificationEmail", type="String",
            description="Email address for pipeline notifications (RCA ready, approval needed)",
            default="",
        )
        resolution_cluster_arn = CfnParameter(
            self, "ResolutionClusterArn", type="String",
            description="ECS cluster ARN from ResolutionStack",
            default=f"arn:aws:ecs:{self.region}:{self.account}:cluster/agentic-pipeline-resolution",
        )
        resolution_task_def_arn = CfnParameter(
            self, "ResolutionTaskDefinitionArn", type="String",
            description="Fargate task definition ARN from ResolutionStack",
            default="",
        )
        resolution_subnet_ids = CfnParameter(
            self, "ResolutionSubnetIds", type="String",
            description="JSON array of private subnet IDs from ResolutionStack, e.g. [\"subnet-abc\",\"subnet-def\"]",
            default="[]",
        )
        resolution_sg_id = CfnParameter(
            self, "ResolutionSecurityGroupId", type="String",
            description="Security group ID from ResolutionStack",
            default="",
        )
        resolution_output_bucket = CfnParameter(
            self, "ResolutionOutputBucket", type="String",
            description="S3 bucket name for resolution output from ResolutionStack",
            default="",
        )
        kb_id = CfnParameter(
            self, "KnowledgeBaseId", type="String",
            description="Bedrock Knowledge Base ID from KnowledgeBaseStack",
            default="",
        )
        guardrail_id = CfnParameter(
            self, "GuardrailId", type="String",
            description="Bedrock Guardrail ID from GuardrailStack",
            default="",
        )
        guardrail_version = CfnParameter(
            self, "GuardrailVersion", type="String",
            description="Bedrock Guardrail version from GuardrailStack",
            default="",
        )
        planner_runtime_arn = CfnParameter(
            self, "PlannerRuntimeArn", type="String",
            description="AgentCore Runtime ARN for Resolution Planner from PlannerAgentCoreStack",
            default=f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/agentic_pipeline_resolution_planner",
        )
        resolution_dispatcher_arn = CfnParameter(
            self, "ResolutionDispatcherArn", type="String",
            description="ARN of the Resolution Dispatcher Lambda from ResolutionAgentCoreStack",
            default=f"arn:aws:lambda:{self.region}:{self.account}:function:agentic-pipeline-resolution-dispatcher",
        )
        jira_base_url = CfnParameter(
            self, "JiraBaseUrl", type="String",
            description="Jira Cloud base URL (e.g. https://your-org.atlassian.net)",
            default=self.node.try_get_context("jira_base_url") or "",
        )
        jira_api_secret_arn = CfnParameter(
            self, "JiraApiSecretArn", type="String",
            description="Secrets Manager ARN for Jira API credentials (from JiraIntakeStack)",
            default=self.node.try_get_context("jira_api_secret_arn") or "",
        )

        # ── Secrets ─────────────────────────────────────────────────────────
        agent_secret = sm.Secret(
            self, "DevOpsAgentSecret",
            secret_name="agentic-pipeline/devops-agent-secret",
            description="Secret for HMAC-signing events forwarded to the DevOps Agent",
        )

        # ── DynamoDB — Task Token Store ─────────────────────────────────────
        task_token_table = ddb.Table(
            self, "TaskTokenTable",
            table_name="agentic-pipeline-task-tokens",
            partition_key=ddb.Attribute(name="issue_key", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="stage", type=ddb.AttributeType.STRING),
            time_to_live_attribute="expires_at",
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── SNS — Notification Topic ───────────────────────────────────────
        notification_topic = sns.Topic(
            self, "NotificationTopic",
            topic_name="agentic-pipeline-notifications",
            display_name="Agentic Resolution Pipeline Notifications",
        )

        # ── Lambda Functions ────────────────────────────────────────────────
        src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator")
        code_path = _build_orchestrator_asset(src_dir)

        common_env = {
            "DEVOPS_AGENT_WEBHOOK_URL": self.node.try_get_context("devops_agent_webhook_url") or devops_agent_url.value_as_string,
            "DEVOPS_AGENT_SECRET_ARN": agent_secret.secret_arn,
            "TASK_TOKEN_TABLE": task_token_table.table_name,
            "SNS_NOTIFICATION_TOPIC_ARN": notification_topic.topic_arn,
            "GUARDRAIL_ID": guardrail_id.value_as_string,
            "GUARDRAIL_VERSION": guardrail_version.value_as_string,
        }

        classify_fn = lambda_.Function(
            self, "ClassifyFn",
            function_name="agentic-pipeline-classify",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.classify_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                **common_env,
                "LLM_CLASSIFIER_ENABLED": "true",
                "LLM_CLASSIFIER_MODEL_ID": "us.anthropic.claude-opus-4-8",
            },
        )
        classify_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel", "bedrock:ApplyGuardrail"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/us.anthropic.claude-opus-4-8",
                f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/{guardrail_id.value_as_string}",
            ],
        ))

        dispatch_fn = lambda_.Function(
            self, "DispatchFn",
            function_name="agentic-pipeline-dispatch",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.dispatch_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                **common_env,
                "JIRA_BASE_URL": jira_base_url.value_as_string,
                "JIRA_API_SECRET_ARN": self.node.try_get_context("jira_api_secret_arn") or jira_api_secret_arn.value_as_string,
            },
        )
        agent_secret.grant_read(dispatch_fn)
        dispatch_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:JiraApiSecret*"],
        ))

        noise_fn = lambda_.Function(
            self, "NoiseFn",
            function_name="agentic-pipeline-noise",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.noise_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(10),
            memory_size=128,
            environment=common_env,
        )

        store_token_fn = lambda_.Function(
            self, "StoreTokenAndNotifyFn",
            function_name="agentic-pipeline-store-token",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.store_token_and_notify",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(15),
            memory_size=128,
            environment=common_env,
        )
        task_token_table.grant_read_write_data(store_token_fn)
        notification_topic.grant_publish(store_token_fn)

        kb_retrieval_fn = lambda_.Function(
            self, "KBRetrievalFn",
            function_name="agentic-pipeline-kb-retrieval",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="kb_retrieval.kb_retrieval_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                **common_env,
                "KB_ID": kb_id.value_as_string,
                "KB_RESULTS_COUNT": "3",
            },
        )
        kb_retrieval_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:Retrieve", "bedrock:ApplyGuardrail"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb_id.value_as_string}",
                f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/{guardrail_id.value_as_string}",
            ],
        ))

        resolve_repo_fn = lambda_.Function(
            self, "ResolveRepoFn",
            function_name="agentic-pipeline-resolve-repo",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.resolve_repo_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(10),
            memory_size=128,
            environment=common_env,
        )

        post_plan_fn = lambda_.Function(
            self, "PostPlanFn",
            function_name="agentic-pipeline-post-plan",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.post_plan_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                **common_env,
                "JIRA_BASE_URL": jira_base_url.value_as_string,
                "JIRA_API_SECRET_ARN": self.node.try_get_context("jira_api_secret_arn") or jira_api_secret_arn.value_as_string,
            },
        )
        task_token_table.grant_read_write_data(post_plan_fn)
        notification_topic.grant_publish(post_plan_fn)
        post_plan_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:JiraApiSecret*"],
        ))

        resolution_planner_fn = lambda_.Function(
            self, "ResolutionPlannerFn",
            function_name="agentic-pipeline-resolution-planner",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="resolution_planner.resolution_planner_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(600),
            memory_size=512,
            environment={
                **common_env,
                "PLANNER_RUNTIME_ARN": planner_runtime_arn.value_as_string,
                "KB_ID": kb_id.value_as_string,
            },
        )
        resolution_planner_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[
                planner_runtime_arn.value_as_string,
                f"{planner_runtime_arn.value_as_string}/*",
            ],
        ))
        resolution_planner_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:Retrieve", "bedrock:ApplyGuardrail"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb_id.value_as_string}",
                f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/{guardrail_id.value_as_string}",
            ],
        ))

        post_resolution_fn = lambda_.Function(
            self, "PostResolutionFn",
            function_name="agentic-pipeline-post-resolution",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.post_resolution_handler",
            code=lambda_.Code.from_asset(code_path),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                **common_env,
                "JIRA_BASE_URL": jira_base_url.value_as_string,
                "JIRA_API_SECRET_ARN": self.node.try_get_context("jira_api_secret_arn") or jira_api_secret_arn.value_as_string,
            },
        )
        notification_topic.grant_publish(post_resolution_fn)
        # Post-resolution Lambda reads output from S3
        post_resolution_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{resolution_output_bucket.value_as_string}/resolutions/*"],
        ))
        post_resolution_fn.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:JiraApiSecret*"],
        ))

        # ── Step Functions State Machine ────────────────────────────────────
        asl_path = os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator", "state_machine.asl.json")
        with open(asl_path, encoding="utf-8") as f:
            asl_template = f.read()

        asl_definition = (
            asl_template
            .replace("${ClassifyFunctionArn}", classify_fn.function_arn)
            .replace("${DispatchFunctionArn}", dispatch_fn.function_arn)
            .replace("${NoiseFunctionArn}", noise_fn.function_arn)
            .replace("${StoreTokenAndNotifyFunctionArn}", store_token_fn.function_arn)
            .replace("${KBRetrievalFunctionArn}", kb_retrieval_fn.function_arn)
            .replace("${ResolveRepoFunctionArn}", resolve_repo_fn.function_arn)
            .replace("${ResolutionPlannerFunctionArn}", resolution_planner_fn.function_arn)
            .replace("${PostPlanFunctionArn}", post_plan_fn.function_arn)
            .replace("${ResolutionDispatcherFunctionArn}", resolution_dispatcher_arn.value_as_string)
            .replace("${PostResolutionFunctionArn}", post_resolution_fn.function_arn)
            .replace("${ResolutionClusterArn}", resolution_cluster_arn.value_as_string)
            .replace("${ResolutionTaskDefinitionArn}", resolution_task_def_arn.value_as_string)
            .replace("${ResolutionSubnetIds}", resolution_subnet_ids.value_as_string)
            .replace("${ResolutionSecurityGroupId}", resolution_sg_id.value_as_string)
            .replace("${ResolutionOutputBucket}", resolution_output_bucket.value_as_string)
        )

        sm_role = iam.Role(
            self, "OrchestratorSMRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        )
        classify_fn.grant_invoke(sm_role)
        dispatch_fn.grant_invoke(sm_role)
        noise_fn.grant_invoke(sm_role)
        store_token_fn.grant_invoke(sm_role)
        kb_retrieval_fn.grant_invoke(sm_role)
        resolve_repo_fn.grant_invoke(sm_role)
        resolution_planner_fn.grant_invoke(sm_role)
        post_plan_fn.grant_invoke(sm_role)
        post_resolution_fn.grant_invoke(sm_role)
        # AgentCore invoker Lambda (cross-stack, referenced by ARN)
        sm_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["lambda:InvokeFunction"],
            resources=[resolution_dispatcher_arn.value_as_string],
        ))

        # ECS RunTask permissions for Step Functions
        sm_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ecs:RunTask"],
            resources=["*"],
            conditions={
                "ArnEquals": {
                    "ecs:cluster": resolution_cluster_arn.value_as_string,
                },
            },
        ))
        sm_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ecs:StopTask", "ecs:DescribeTasks"],
            resources=["*"],
            conditions={
                "ArnEquals": {
                    "ecs:cluster": resolution_cluster_arn.value_as_string,
                },
            },
        ))
        sm_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["iam:PassRole"],
            resources=[
                f"arn:aws:iam::{self.account}:role/agentic-pipeline-resolution-execution-{self.region}",
                f"arn:aws:iam::{self.account}:role/agentic-pipeline-resolution-task-{self.region}",
            ],
            conditions={
                "StringLike": {
                    "iam:PassedToService": "ecs-tasks.amazonaws.com",
                },
            },
        ))
        sm_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
            resources=[
                f"arn:aws:events:{self.region}:{self.account}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule.*",
            ],
        ))

        state_machine = sfn.CfnStateMachine(
            self, "OrchestratorStateMachine",
            state_machine_name="agentic-pipeline-orchestrator",
            definition_string=asl_definition,
            role_arn=sm_role.role_arn,
            state_machine_type="STANDARD",
        )

        # ── Grant webhook receiver permission to resume executions ─────────
        # The webhook Lambda needs: states:StartExecution (new events) +
        # DynamoDB read/write on task token table + states:SendTaskSuccess (callbacks)
        # StartExecution is granted in the JiraIntakeStack; here we output what
        # the intake stack needs for the callback path.

        # ── Outputs ─────────────────────────────────────────────────────────
        CfnOutput(self, "StateMachineArn",
                  value=f"arn:aws:states:{self.region}:{self.account}:stateMachine:agentic-pipeline-orchestrator",
                  description="Orchestrator state machine ARN — webhook receiver starts executions here")
        CfnOutput(self, "TaskTokenTableName", value=task_token_table.table_name,
                  description="DynamoDB table storing Step Functions task tokens for callback pattern")
        CfnOutput(self, "NotificationTopicArn", value=notification_topic.topic_arn,
                  description="SNS topic for pipeline notifications — subscribe email/Slack here")
        CfnOutput(self, "DevOpsAgentSecretArn", value=agent_secret.secret_arn,
                  description="Populate with the shared secret for signing events to the DevOps Agent")
