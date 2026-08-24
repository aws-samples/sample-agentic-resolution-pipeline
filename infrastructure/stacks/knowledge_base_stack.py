"""
Knowledge Base Stack — Bedrock Knowledge Base with OpenSearch Serverless.

Two-phase deployment:
  Phase 1: `cdk deploy KnowledgeBaseStack` — deploys AOSS collection, S3 bucket,
            policies, and Ingestion Lambda. KB is created with a condition that
            requires the index to exist.
  Phase 2: `python scripts/setup_knowledge_base.py` — creates the AOSS vector
            index (using your Admin role), then creates the Bedrock KB + data source
            via API calls.

This two-phase approach is necessary because:
  - Bedrock KB requires the AOSS index to exist at creation time
  - AOSS data access policies are eventually consistent (~60-120s)
  - CloudFormation custom resources cannot reliably wait for propagation

The KB stores successful resolution records so the Resolution Agent can
retrieve context from similar past fixes before attempting a new one.
"""

import json
import os
import shutil
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_opensearchserverless as aoss,
    aws_s3 as s3,
    CfnOutput,
    Duration,
    RemovalPolicy,
)
from constructs import Construct

COLLECTION_NAME = "agentic-pipeline-kb"
INDEX_NAME = "resolution-fixes"
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024


class KnowledgeBaseStack(cdk.Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── S3 Data Source Bucket ──────────────────────────────────────────────
        data_bucket = s3.Bucket(
            self, "KBDataBucket",
            bucket_name=f"agentic-pipeline-kb-data-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="intelligent-tiering",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                            transition_after=Duration.days(30),
                        ),
                    ],
                ),
            ],
        )

        # ── OpenSearch Serverless — Encryption Policy ──────────────────────────
        encryption_policy = aoss.CfnSecurityPolicy(
            self, "AOSSEncryptionPolicy",
            name=f"{COLLECTION_NAME}-enc",
            type="encryption",
            policy=json.dumps({
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{COLLECTION_NAME}"],
                    }
                ],
                "AWSOwnedKey": True,
            }),
        )

        # ── OpenSearch Serverless — Network Policy ─────────────────────────────
        network_policy = aoss.CfnSecurityPolicy(
            self, "AOSSNetworkPolicy",
            name=f"{COLLECTION_NAME}-net",
            type="network",
            policy=json.dumps([
                {
                    "Rules": [
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{COLLECTION_NAME}"],
                        },
                        {
                            "ResourceType": "dashboard",
                            "Resource": [f"collection/{COLLECTION_NAME}"],
                        },
                    ],
                    "AllowFromPublic": True,
                }
            ]),
        )

        # ── OpenSearch Serverless Collection ───────────────────────────────────
        collection = aoss.CfnCollection(
            self, "AOSSCollection",
            name=COLLECTION_NAME,
            type="VECTORSEARCH",
            description="Vector store for agentic resolution pipeline knowledge base",
        )
        collection.add_dependency(encryption_policy)
        collection.add_dependency(network_policy)

        # ── IAM Role for Bedrock KB (access S3 + AOSS) ─────────────────────────
        kb_role = iam.Role(
            self, "BedrockKBRole",
            role_name=f"agentic-pipeline-bedrock-kb-{self.region}",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            inline_policies={
                "s3-access": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["s3:GetObject", "s3:ListBucket"],
                            resources=[
                                data_bucket.bucket_arn,
                                f"{data_bucket.bucket_arn}/*",
                            ],
                        ),
                    ]
                ),
                "aoss-access": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["aoss:APIAccessAll"],
                            resources=[
                                f"arn:aws:aoss:{self.region}:{self.account}:collection/*",
                            ],
                        ),
                    ]
                ),
                "embedding-access": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["bedrock:InvokeModel"],
                            resources=[
                                f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}",
                            ],
                        ),
                    ]
                ),
            },
        )

        # ── AOSS Data Access Policy ───────────────────────────────────────────
        # Grants the Bedrock KB role + Administrator role access.
        # Administrator is needed for post-deploy index creation via script.
        data_access_policy = aoss.CfnAccessPolicy(
            self, "AOSSDataAccessPolicy",
            name=f"{COLLECTION_NAME}-access",
            type="data",
            policy=json.dumps([
                {
                    "Rules": [
                        {
                            "ResourceType": "index",
                            "Resource": [f"index/{COLLECTION_NAME}/*"],
                            "Permission": [
                                "aoss:CreateIndex",
                                "aoss:DeleteIndex",
                                "aoss:UpdateIndex",
                                "aoss:DescribeIndex",
                                "aoss:ReadDocument",
                                "aoss:WriteDocument",
                            ],
                        },
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{COLLECTION_NAME}"],
                            "Permission": [
                                "aoss:CreateCollectionItems",
                                "aoss:DeleteCollectionItems",
                                "aoss:UpdateCollectionItems",
                                "aoss:DescribeCollectionItems",
                            ],
                        },
                    ],
                    "Principal": [
                        f"arn:aws:iam::{self.account}:role/agentic-pipeline-bedrock-kb-{self.region}",
                        f"arn:aws:iam::{self.account}:role/Administrator",
                    ],
                    "Description": "Full access for KB role and Administrator",
                }
            ]),
        )

        # ── Ingestion Lambda ──────────────────────────────────────────────────
        # Deployed now so it's ready once the KB is created via setup script.
        ingestion_fn = lambda_.Function(
            self, "KBIngestionFn",
            function_name="agentic-pipeline-kb-ingestion",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="kb_ingestion.ingestion_handler",
            code=lambda_.Code.from_asset(self._build_ingestion_asset()),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "KB_DATA_BUCKET": data_bucket.bucket_name,
                "KB_ID": "",  # Set by setup_knowledge_base.py after KB creation
                "DATA_SOURCE_ID": "",  # Set by setup_knowledge_base.py after KB creation
            },
        )
        data_bucket.grant_write(ingestion_fn)
        ingestion_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:StartIngestionJob", "bedrock:GetIngestionJob"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*",
            ],
        ))

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "DataBucketName",
                  value=data_bucket.bucket_name,
                  description="S3 bucket for resolution documents (KB data source)")
        CfnOutput(self, "DataBucketArn",
                  value=data_bucket.bucket_arn,
                  description="S3 bucket ARN for KB data source config")
        CfnOutput(self, "CollectionEndpoint",
                  value=collection.attr_collection_endpoint,
                  description="AOSS collection endpoint for index creation")
        CfnOutput(self, "CollectionArn",
                  value=collection.attr_arn,
                  description="AOSS collection ARN for KB storage config")
        CfnOutput(self, "KBRoleArn",
                  value=kb_role.role_arn,
                  description="Bedrock KB IAM role ARN")
        CfnOutput(self, "IngestionFunctionName",
                  value=ingestion_fn.function_name,
                  description="Lambda to invoke for KB ingestion after confirmed PR merge")

        # ── Exports for setup script ──────────────────────────────────────────
        self.data_bucket_name = data_bucket.bucket_name
        self.collection_endpoint = collection.attr_collection_endpoint
        self.collection_arn = collection.attr_arn
        self.kb_role_arn = kb_role.role_arn
        self.ingestion_fn_name = ingestion_fn.function_name

    def _build_ingestion_asset(self) -> str:
        """Stage the kb_ingestion module for Lambda packaging."""
        src = Path(os.path.dirname(__file__)).parent.parent / "orchestrator"
        out = src.parent / "_build" / "kb-ingestion-lambda"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        ingestion_file = src / "kb_ingestion.py"
        if ingestion_file.exists():
            shutil.copy2(ingestion_file, out / "kb_ingestion.py")

        return str(out)
