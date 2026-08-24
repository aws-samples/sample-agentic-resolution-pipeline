"""
Resolution Stack — ECS Fargate infrastructure for the Resolution Agent.

Provisions:
  - VPC (2 AZs, public + private subnets with NAT)
  - ECS Cluster
  - ECR Repository (resolution-agent image)
  - Fargate Task Definition (Claude Code CLI container)
  - IAM Task Execution Role (pull image, write logs)
  - IAM Task Role (Secrets Manager, S3 output, Bedrock invoke)
  - S3 Bucket (resolution output — PR URLs, logs)
  - Secrets Manager entry for repo credentials (PAT)
  - CloudWatch Log Group

The task is invoked by Step Functions via ecs:runTask.sync — no ECS service
is needed (each resolution is a one-shot task).
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as sm,
    CfnOutput,
    Duration,
    RemovalPolicy,
)
from constructs import Construct


class ResolutionStack(cdk.Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── VPC (provide via context: cdk deploy -c vpc_id=vpc-xxx)
        vpc_id = self.node.try_get_context("vpc_id")
        if not vpc_id:
            raise ValueError("vpc_id context required: cdk deploy -c vpc_id=vpc-xxx")
        vpc = ec2.Vpc.from_lookup(self, "ResolutionVpc", vpc_id=vpc_id)

        # ── ECR Repository ─────────────────────────────────────────────────────
        ecr_repo = ecr.Repository(
            self, "ResolutionAgentRepo",
            repository_name="agentic-pipeline/resolution-agent",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Keep last 10 images",
                    max_image_count=10,
                ),
            ],
        )

        # ── ECS Cluster ────────────────────────────────────────────────────────
        cluster = ecs.Cluster(
            self, "ResolutionCluster",
            cluster_name="agentic-pipeline-resolution",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # ── CloudWatch Logs ────────────────────────────────────────────────────
        log_group = logs.LogGroup(
            self, "ResolutionLogGroup",
            log_group_name="/agentic-pipeline/resolution-agent",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── S3 Output Bucket ──────────────────────────────────────────────────
        output_bucket = s3.Bucket(
            self, "ResolutionOutputBucket",
            bucket_name=f"agentic-pipeline-resolution-output-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(30),
                    id="expire-after-30-days",
                ),
            ],
        )

        # ── Secrets — Repo Credentials ─────────────────────────────────────────
        repo_credentials_secret = sm.Secret(
            self, "RepoCredentialsSecret",
            secret_name="agentic-pipeline/repo-credentials",
            description="Git credentials for Resolution Agent (PAT or SSH key). Populate out-of-band.",
        )

        # ── Secrets — Claude API Key ───────────────────────────────────────────
        claude_api_secret = sm.Secret(
            self, "ClaudeApiSecret",
            secret_name="agentic-pipeline/claude-api-key",
            description="Anthropic API key for Claude Code CLI. Populate out-of-band.",
        )

        # ── IAM Task Execution Role (ECS agent: pull image, push logs) ─────────
        execution_role = iam.Role(
            self, "TaskExecutionRole",
            role_name=f"agentic-pipeline-resolution-execution-{self.region}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )
        # Allow pulling secrets into container env at launch
        repo_credentials_secret.grant_read(execution_role)
        claude_api_secret.grant_read(execution_role)

        # ── IAM Task Role (the container's runtime identity) ───────────────────
        task_role = iam.Role(
            self, "TaskRole",
            role_name=f"agentic-pipeline-resolution-task-{self.region}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        output_bucket.grant_write(task_role)
        repo_credentials_secret.grant_read(task_role)
        claude_api_secret.grant_read(task_role)

        # Bedrock invoke — for Claude Code CLI using Bedrock as backend
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-*",
                f"arn:aws:bedrock:{self.region}::foundation-model/us.anthropic.claude-*",
            ],
        ))

        # Per-repo secrets — entrypoint fetches from AUTH_SECRET_ARN at runtime
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentic-pipeline/*",
            ],
        ))

        # ── Fargate Task Definition ────────────────────────────────────────────
        task_def = ecs.FargateTaskDefinition(
            self, "ResolutionTaskDef",
            family="agentic-pipeline-resolution",
            cpu=1024,       # 1 vCPU
            memory_limit_mib=2048,  # 2 GB
            execution_role=execution_role,
            task_role=task_role,
        )

        container = task_def.add_container(
            "resolution-agent",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(
                log_group=log_group,
                stream_prefix="resolution",
            ),
            environment={
                "OUTPUT_BUCKET": output_bucket.bucket_name,
            },
            secrets={
                "GIT_TOKEN": ecs.Secret.from_secrets_manager(repo_credentials_secret),
                "ANTHROPIC_API_KEY": ecs.Secret.from_secrets_manager(claude_api_secret),
            },
        )

        # ── Security Group (egress only — no inbound) ──────────────────────────
        sg = ec2.SecurityGroup(
            self, "ResolutionTaskSG",
            vpc=vpc,
            security_group_name="agentic-pipeline-resolution-task",
            description="Resolution Agent Fargate task - egress only",
            allow_all_outbound=True,
        )

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "ClusterArn", value=cluster.cluster_arn,
                  description="ECS cluster ARN for Step Functions runTask")
        CfnOutput(self, "TaskDefinitionArn", value=task_def.task_definition_arn,
                  description="Fargate task definition ARN for Step Functions runTask")
        CfnOutput(self, "EcrRepoUri", value=ecr_repo.repository_uri,
                  description="ECR repository URI — push resolution-agent image here")
        CfnOutput(self, "OutputBucketName", value=output_bucket.bucket_name,
                  description="S3 bucket for resolution output (PR URLs, logs)")
        CfnOutput(self, "VpcId", value=vpc.vpc_id,
                  description="VPC ID housing the resolution tasks")
        CfnOutput(self, "PrivateSubnetIds",
                  value=",".join([s.subnet_id for s in vpc.private_subnets]),
                  description="Private subnet IDs for Step Functions runTask network config")
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id,
                  description="Security group ID for Step Functions runTask network config")
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name,
                  description="CloudWatch log group for resolution agent output")
        CfnOutput(self, "RepoCredentialsSecretArn", value=repo_credentials_secret.secret_arn,
                  description="Populate with GitHub PAT or SSH key for repo access")
        CfnOutput(self, "ClaudeApiSecretArn", value=claude_api_secret.secret_arn,
                  description="Populate with Anthropic API key for Claude Code CLI")

        # ── Export values needed by OrchestratorStack state machine ─────────────
        self.cluster_arn = cluster.cluster_arn
        self.task_definition_arn = task_def.task_definition_arn
        self.output_bucket_name = output_bucket.bucket_name
        self.private_subnets = vpc.private_subnets
        self.security_group_id = sg.security_group_id
