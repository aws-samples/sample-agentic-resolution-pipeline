"""
Planner AgentCore Stack — AgentCore Runtime for the Resolution Planner.

Provisions:
  - Docker Image Asset (builds and pushes the planner container)
  - AgentCore Runtime (managed container with Strands agent)
  - IAM Role for AgentCore (Bedrock invoke, KB retrieve, Secrets Manager)
  - CloudWatch Log Group

Architecture:
  Step Functions → Orchestrator Lambda (invoke_agent_runtime, holds connection)
                 → AgentCore Runtime (Strands agent: KB query, repo resolve, browse, prompt build)
"""

import os
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as bedrockagentcore,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_logs as logs,
    CfnOutput,
    CfnParameter,
)
from constructs import Construct


class PlannerAgentCoreStack(cdk.Stack):

    def __init__(self, scope: "Construct", construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Parameters ─────────────────────────────────────────────────────────
        guardrail_id = CfnParameter(
            self, "GuardrailId", type="String",
            description="Bedrock Guardrail ID",
            default="",
        )
        kb_id = CfnParameter(
            self, "KnowledgeBaseId", type="String",
            description="Bedrock Knowledge Base ID for KB query tool",
            default="",
        )

        # ── IAM Role for AgentCore Runtime ─────────────────────────────────────
        runtime_role = iam.Role(
            self, "PlannerRuntimeRole",
            role_name=f"agentic-pipeline-planner-agentcore-{self.region}",
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
                "bedrock-kb": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["bedrock:Retrieve"],
                            resources=[
                                f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*",
                            ],
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

        # ── Docker Image Asset ─────────────────────────────────────────────────
        agent_dir = str(Path(os.path.dirname(__file__)).parent.parent / "planner" / "agentcore")
        docker_image = ecr_assets.DockerImageAsset(
            self, "PlannerAgentImage",
            directory=agent_dir,
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        # ── Observability — Log Group ──────────────────────────────────────────
        log_group = logs.LogGroup(
            self, "PlannerLogGroup",
            log_group_name="/agentic-pipeline/agentcore-planner",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── AgentCore Runtime ──────────────────────────────────────────────────
        runtime = bedrockagentcore.Runtime(
            self, "PlannerAgentRuntime",
            runtime_name="agentic_pipeline_resolution_planner",
            description="Resolution Planner — queries KB, resolves repos, browses code, builds fix strategies",
            agent_runtime_artifact=bedrockagentcore.AgentRuntimeArtifact.from_image_uri(
                docker_image.image_uri,
            ),
            execution_role=runtime_role,
            environment_variables={
                "AWS_REGION": self.region,
                "MODEL_ID": "us.anthropic.claude-sonnet-4-6",
                "KB_ID": kb_id.value_as_string,
                "GUARDRAIL_ID": guardrail_id.value_as_string,
                "REPO_CONFIG_PATH": "/app/repo-config.yaml",
            },
            tracing_enabled=False,
            logging_configs=[
                bedrockagentcore.LoggingConfig(
                    log_type=bedrockagentcore.LogType.APPLICATION_LOGS,
                    destination=bedrockagentcore.LoggingDestination.cloud_watch_logs(log_group),
                ),
                bedrockagentcore.LoggingConfig(
                    log_type=bedrockagentcore.LogType.USAGE_LOGS,
                    destination=bedrockagentcore.LoggingDestination.cloud_watch_logs(log_group),
                ),
            ],
        )

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "AgentRuntimeArn",
                  value=runtime.agent_runtime_arn,
                  description="Planner AgentCore Runtime ARN")
        CfnOutput(self, "AgentRuntimeId",
                  value=runtime.agent_runtime_id,
                  description="Planner AgentCore Runtime ID")
        CfnOutput(self, "ImageUri",
                  value=docker_image.image_uri,
                  description="Docker image URI deployed to AgentCore")
        CfnOutput(self, "LogGroupName",
                  value=log_group.log_group_name,
                  description="CloudWatch log group for Planner agent")

        self.agent_runtime_arn = runtime.agent_runtime_arn
