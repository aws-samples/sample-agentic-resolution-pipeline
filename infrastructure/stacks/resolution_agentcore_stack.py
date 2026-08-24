"""
Resolution AgentCore Stack — AgentCore Runtime + ECS Worker for the Resolution Agent.

Provisions:
  - Docker Image Asset (builds and pushes the agent container to AgentCore)
  - AgentCore Runtime (managed container with Strands agent)
  - IAM Role for AgentCore (Bedrock invoke, Secrets Manager, S3)
  - ECS Worker Task Definition (drives AgentCore, no timeout limit)
  - Dispatcher Lambda (stores task token, starts ECS worker)
  - CloudWatch Log Groups (worker + AgentCore application logs)

Architecture:
  Step Functions → Dispatcher Lambda (stores token, starts ECS task, returns)
                 → ECS Worker (drives AgentCore via invoke_agent_runtime, sends callback)
                 → AgentCore Runtime (Strands agent: clone, fix, test, PR)
"""

import os
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as bedrockagentcore,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    CfnOutput,
    CfnParameter,
    Duration,
)
from constructs import Construct


class ResolutionAgentCoreStack(cdk.Stack):

    def __init__(self, scope: "Construct", construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Parameters ─────────────────────────────────────────────────────────
        guardrail_id = CfnParameter(
            self, "GuardrailId", type="String",
            description="Bedrock Guardrail ID",
            default="",
        )

        # ── IAM Role for AgentCore Runtime ─────────────────────────────────────
        runtime_role = iam.Role(
            self, "AgentCoreRuntimeRole",
            role_name=f"agentic-pipeline-resolution-agentcore-{self.region}",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"},
                },
            ),
            inline_policies={
                "bedrock-invoke": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                                "bedrock:ApplyGuardrail",
                            ],
                            resources=[
                                "arn:aws:bedrock:*::foundation-model/*",
                                f"arn:aws:bedrock:{self.region}:{self.account}:*",
                            ],
                        ),
                    ]
                ),
                "cloudwatch-logs": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                            resources=[
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                            ],
                        ),
                        iam.PolicyStatement(
                            actions=["logs:PutResourcePolicy"],
                            resources=[
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/agentic_pipeline*",
                            ],
                        ),
                        iam.PolicyStatement(
                            actions=["logs:DescribeLogGroups"],
                            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:*"],
                        ),
                        iam.PolicyStatement(
                            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                            resources=[
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                            ],
                        ),
                    ]
                ),
                "xray-tracing": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "xray:PutTraceSegments",
                                "xray:PutTelemetryRecords",
                                "xray:GetSamplingRules",
                                "xray:GetSamplingTargets",
                            ],
                            resources=["*"],
                        ),
                    ]
                ),
                "cloudwatch-metrics": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["cloudwatch:PutMetricData"],
                            resources=["*"],
                            conditions={
                                "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"},
                            },
                        ),
                    ]
                ),
                "secrets-access": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["secretsmanager:GetSecretValue"],
                            resources=[
                                f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentic-pipeline/*",
                            ],
                        ),
                    ]
                ),
                "s3-output": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["s3:PutObject", "s3:GetObject"],
                            resources=[
                                f"arn:aws:s3:::agentic-pipeline-resolution-output-{self.account}-{self.region}/*",
                            ],
                        ),
                    ]
                ),
                "ecr-pull": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "ecr:GetDownloadUrlForLayer",
                                "ecr:BatchGetImage",
                                "ecr:GetAuthorizationToken",
                            ],
                            resources=["*"],
                        ),
                    ]
                ),
            },
        )

        # ── Docker Image Asset (AgentCore container) ───────────────────────────
        agent_dir = str(Path(os.path.dirname(__file__)).parent.parent / "resolution" / "agentcore")
        docker_image = ecr_assets.DockerImageAsset(
            self, "ResolutionAgentImage",
            directory=agent_dir,
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        # ── Observability — AgentCore logs ─────────────────────────────────────
        agent_log_group = logs.LogGroup(
            self, "AgentCoreLogGroup",
            log_group_name="/agentic-pipeline/agentcore-resolution",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── Observability — ECS Worker logs ────────────────────────────────────
        worker_log_group = logs.LogGroup(
            self, "WorkerLogGroup",
            log_group_name="/agentic-pipeline/resolution-worker",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── AgentCore Memory (cross-session learning) ─────────────────────────
        memory = bedrockagentcore.Memory(
            self, "ResolutionMemory",
            memory_name="resolution_agent_memory",
            description="Learns from past resolutions: repo patterns, fix strategies, common pitfalls",
            expiration_duration=cdk.Duration.days(90),
            memory_strategies=[
                bedrockagentcore.MemoryStrategy.using_built_in_semantic(),
                bedrockagentcore.MemoryStrategy.using_built_in_user_preference(),
                bedrockagentcore.MemoryStrategy.using_built_in_summarization(),
            ],
        )

        # ── AgentCore Runtime (L2 construct with observability) ───────────────
        runtime = bedrockagentcore.Runtime(
            self, "ResolutionAgentRuntimeL2",
            runtime_name="agentic_pipeline_resolution_agent",
            description="Resolution Agent — clones repos, writes fixes, creates PRs using Claude with Strands",
            agent_runtime_artifact=bedrockagentcore.AgentRuntimeArtifact.from_image_uri(
                docker_image.image_uri,
            ),
            execution_role=runtime_role,
            environment_variables={
                "AWS_REGION": self.region,
                "MODEL_ID": "us.anthropic.claude-sonnet-4-6",
                "GUARDRAIL_ID": guardrail_id.value_as_string,
                "BYPASS_TOOL_CONSENT": "true",
                "STRANDS_NON_INTERACTIVE": "true",
                "MEMORY_ID": memory.memory_id,
            },
            tracing_enabled=False,
            logging_configs=[
                bedrockagentcore.LoggingConfig(
                    log_type=bedrockagentcore.LogType.APPLICATION_LOGS,
                    destination=bedrockagentcore.LoggingDestination.cloud_watch_logs(agent_log_group),
                ),
                bedrockagentcore.LoggingConfig(
                    log_type=bedrockagentcore.LogType.USAGE_LOGS,
                    destination=bedrockagentcore.LoggingDestination.cloud_watch_logs(agent_log_group),
                ),
            ],
        )
        runtime.node.default_child.override_logical_id("ResolutionAgentRuntime")

        # Grant runtime role access to memory
        memory.grant_full_access(runtime_role)

        # ── ECS Worker — Task Definition ───────────────────────────────────────
        vpc_id = self.node.try_get_context("vpc_id")
        if not vpc_id:
            raise ValueError("vpc_id context required: cdk deploy -c vpc_id=vpc-xxx")
        vpc = ec2.Vpc.from_lookup(self, "WorkerVpc", vpc_id=vpc_id)

        worker_dir = str(Path(os.path.dirname(__file__)).parent.parent / "resolution" / "worker")
        worker_image = ecr_assets.DockerImageAsset(
            self, "WorkerImage",
            directory=worker_dir,
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        worker_execution_role = iam.Role(
            self, "WorkerExecutionRole",
            role_name=f"agentic-pipeline-worker-execution-{self.region}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        worker_task_role = iam.Role(
            self, "WorkerTaskRole",
            role_name=f"agentic-pipeline-resolution-worker-{self.region}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        worker_task_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:InvokeAgentRuntime",
                "bedrock-agentcore:InvokeAgentRuntimeCommand",
            ],
            resources=[
                runtime.agent_runtime_arn,
                f"{runtime.agent_runtime_arn}/*",
            ],
        ))
        worker_task_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:DeleteItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/agentic-pipeline-task-tokens",
            ],
        ))
        worker_task_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "states:SendTaskSuccess",
                "states:SendTaskFailure",
                "states:SendTaskHeartbeat",
            ],
            resources=["*"],
        ))
        worker_task_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[
                f"arn:aws:s3:::agentic-pipeline-resolution-output-{self.account}-{self.region}/*",
            ],
        ))

        worker_task_def = ecs.FargateTaskDefinition(
            self, "WorkerTaskDef",
            family="agentic-pipeline-resolution-worker",
            cpu=512,
            memory_limit_mib=1024,
            execution_role=worker_execution_role,
            task_role=worker_task_role,
        )

        worker_task_def.add_container(
            "worker",
            image=ecs.ContainerImage.from_docker_image_asset(worker_image),
            logging=ecs.LogDrivers.aws_logs(
                log_group=worker_log_group,
                stream_prefix="worker",
            ),
            environment={
                "AWS_REGION": self.region,
                "AGENT_RUNTIME_ARN": runtime.agent_runtime_arn,
                "TASK_TOKEN_TABLE": "agentic-pipeline-task-tokens",
                "OUTPUT_BUCKET": f"agentic-pipeline-resolution-output-{self.account}-{self.region}",
            },
        )

        sg = ec2.SecurityGroup(
            self, "WorkerSG",
            vpc=vpc,
            security_group_name="agentic-pipeline-resolution-worker",
            description="Resolution Worker - egress only",
            allow_all_outbound=True,
        )

        # ── Dispatcher Lambda ──────────────────────────────────────────────────
        dispatcher_fn = lambda_.Function(
            self, "ResolutionDispatcherFn",
            function_name="agentic-pipeline-resolution-dispatcher",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(self._dispatcher_code()),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "TASK_TOKEN_TABLE": "agentic-pipeline-task-tokens",
                "AGENT_RUNTIME_ARN": runtime.agent_runtime_arn,
                "OUTPUT_BUCKET": f"agentic-pipeline-resolution-output-{self.account}-{self.region}",
                "ECS_CLUSTER_ARN": f"arn:aws:ecs:{self.region}:{self.account}:cluster/agentic-pipeline-resolution",
                "WORKER_TASK_DEF_ARN": worker_task_def.task_definition_arn,
                "WORKER_SUBNETS": ",".join([s.subnet_id for s in vpc.private_subnets]),
                "WORKER_SECURITY_GROUP": sg.security_group_id,
            },
        )
        dispatcher_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[
                f"arn:aws:s3:::agentic-pipeline-resolution-output-{self.account}-{self.region}/dispatch/*",
            ],
        ))
        dispatcher_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/agentic-pipeline-task-tokens",
            ],
        ))
        dispatcher_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ecs:RunTask", "ecs:TagResource"],
            resources=[
                worker_task_def.task_definition_arn,
                f"arn:aws:ecs:{self.region}:{self.account}:task/agentic-pipeline-resolution/*",
            ],
        ))
        dispatcher_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[
                worker_execution_role.role_arn,
                worker_task_role.role_arn,
            ],
        ))

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "AgentRuntimeArn",
                  value=runtime.agent_runtime_arn,
                  description="AgentCore Runtime ARN")
        CfnOutput(self, "AgentRuntimeVersion",
                  value=runtime.agent_runtime_id,
                  description="AgentCore Runtime ID")
        CfnOutput(self, "DispatcherFunctionName",
                  value=dispatcher_fn.function_name,
                  description="Dispatcher Lambda — stores token and starts ECS worker")
        CfnOutput(self, "WorkerTaskDefArn",
                  value=worker_task_def.task_definition_arn,
                  description="ECS Worker task definition ARN")
        CfnOutput(self, "WorkerLogGroupName",
                  value=worker_log_group.log_group_name,
                  description="CloudWatch log group for ECS worker")
        CfnOutput(self, "ImageUri",
                  value=docker_image.image_uri,
                  description="Docker image URI deployed to AgentCore")
        CfnOutput(self, "MemoryId",
                  value=memory.memory_id,
                  description="AgentCore Memory ID for cross-session learning")

        self.agent_runtime_arn = runtime.agent_runtime_arn
        self.dispatcher_fn_arn = dispatcher_fn.function_arn

    def _dispatcher_code(self) -> str:
        """Stores task token in DynamoDB, starts ECS worker task, returns immediately."""
        return '''
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TASK_TOKEN_TABLE = os.environ["TASK_TOKEN_TABLE"]
AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
ECS_CLUSTER_ARN = os.environ["ECS_CLUSTER_ARN"]
WORKER_TASK_DEF_ARN = os.environ["WORKER_TASK_DEF_ARN"]
WORKER_SUBNETS = os.environ["WORKER_SUBNETS"].split(",")
WORKER_SECURITY_GROUP = os.environ["WORKER_SECURITY_GROUP"]


def handler(event, context):
    """Store task token, start ECS worker, return immediately."""
    issue_key = event.get("issue_key", "UNKNOWN-0")
    stage = event.get("stage", "awaiting_resolution")
    task_token = event.get("task_token")
    plan = event.get("plan", {})
    rca_result = event.get("rca_result", {})

    if not task_token:
        raise ValueError("task_token is required")

    # Store task token in DynamoDB
    ddb = boto3.resource("dynamodb")
    table = ddb.Table(TASK_TOKEN_TABLE)
    table.put_item(Item={
        "issue_key": issue_key,
        "stage": stage,
        "task_token": task_token,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "expires_at": int(time.time()) + 14400,
    })
    logger.info(f"[DISPATCH] Stored task token for {issue_key}/{stage}")

    # Write payload to S3 (ECS container overrides limited to 8KB)
    task_payload = json.dumps({
        "issue_key": issue_key,
        "stage": stage,
        "plan": plan,
        "rca_result": rca_result,
    })
    s3_client = boto3.client("s3")
    output_bucket = os.environ.get("OUTPUT_BUCKET", "")
    payload_key = f"dispatch/{issue_key}/{int(time.time())}.json"
    s3_client.put_object(
        Bucket=output_bucket,
        Key=payload_key,
        Body=task_payload.encode(),
        ContentType="application/json",
    )
    logger.info(f"[DISPATCH] Payload written to s3://{output_bucket}/{payload_key} ({len(task_payload)} bytes)")

    # Pass S3 reference to worker (fits well within 8KB limit)
    payload_ref = json.dumps({
        "bucket": output_bucket,
        "key": payload_key,
    })

    # Start ECS worker task
    ecs_client = boto3.client("ecs")
    response = ecs_client.run_task(
        cluster=ECS_CLUSTER_ARN,
        taskDefinition=WORKER_TASK_DEF_ARN,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": WORKER_SUBNETS,
                "securityGroups": [WORKER_SECURITY_GROUP],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "worker",
                    "environment": [
                        {"name": "TASK_PAYLOAD_S3", "value": payload_ref},
                    ],
                }
            ]
        },
        tags=[
            {"key": "issue_key", "value": issue_key},
            {"key": "pipeline", "value": "agentic-resolution"},
        ],
    )

    tasks = response.get("tasks", [])
    if not tasks:
        failures = response.get("failures", [])
        logger.error(f"[DISPATCH] ECS RunTask failed: {failures}")
        raise RuntimeError(f"Failed to start worker: {failures}")

    task_arn = tasks[0]["taskArn"]
    logger.info(f"[DISPATCH] Worker started: {task_arn}")

    return {"dispatched": True, "issue_key": issue_key, "worker_task_arn": task_arn}
'''
