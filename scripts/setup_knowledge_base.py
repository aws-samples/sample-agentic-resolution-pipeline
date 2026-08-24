#!/usr/bin/env python3
"""
Post-deploy Knowledge Base setup — creates AOSS index, Bedrock KB, and seeds data.

Run this AFTER `cdk deploy KnowledgeBaseStack`.

Usage:
  python scripts/setup_knowledge_base.py           # Full setup + seed
  python scripts/setup_knowledge_base.py --no-seed # Setup only, skip seeding

What it does:
  1. Reads stack outputs from CloudFormation
  2. Creates the AOSS vector index (with retry for policy propagation)
  3. Creates the Bedrock Knowledge Base via API
  4. Creates the S3 data source on the KB
  5. Updates the Ingestion Lambda env vars with KB_ID and DATA_SOURCE_ID
  6. Optionally seeds sample resolution documents and triggers sync
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3
COLLECTION_NAME = "agentic-pipeline-kb"
INDEX_NAME = "resolution-fixes"
EMBEDDING_MODEL_ARN_TEMPLATE = "arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Allow importing from orchestrator/ for seed step
sys.path.insert(0, str(Path(__file__).parent.parent / "orchestrator"))


def get_stack_outputs() -> dict:
    """Read KnowledgeBaseStack CloudFormation outputs."""
    cfn = boto3.client("cloudformation", region_name=REGION)
    resp = cfn.describe_stacks(StackName="KnowledgeBaseStack")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    return outputs


def create_aoss_index(collection_endpoint: str) -> bool:
    """Create the vector index in AOSS using opensearch-py with AWS4Auth."""
    print("\n[Step 1] Creating AOSS vector index...")

    from opensearchpy import OpenSearch, RequestsHttpConnection
    from requests_aws4auth import AWS4Auth

    credentials = boto3.Session().get_credentials()
    awsauth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        REGION,
        "aoss",
        session_token=credentials.token,
    )

    # Extract host from endpoint URL
    host = collection_endpoint.replace("https://", "")

    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=300,
    )

    index_body = {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": 512,
            }
        },
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": EMBEDDING_DIMENSIONS,
                    "method": {
                        "engine": "faiss",
                        "name": "hnsw",
                        "space_type": "l2",
                        "parameters": {"ef_construction": 512, "m": 16},
                    },
                },
                "text": {"type": "text"},
                "metadata": {"type": "text"},
            }
        },
    }

    max_retries = 8
    for attempt in range(max_retries):
        try:
            response = client.indices.create(index=INDEX_NAME, body=index_body)
            print(f"  Index '{INDEX_NAME}' created successfully")
            return True
        except Exception as e:
            error_str = str(e)
            if "resource_already_exists_exception" in error_str:
                print(f"  Index '{INDEX_NAME}' already exists — OK")
                return True
            if "403" in error_str and attempt < max_retries - 1:
                wait = min(2 ** attempt * 5, 60)
                print(f"  Got 403 (policy propagating), retrying in {wait}s... ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"  ERROR: {error_str}")
            return False

    print("  ERROR: Exhausted retries")
    return False


def create_knowledge_base(collection_arn: str, kb_role_arn: str) -> str:
    """Create Bedrock Knowledge Base via API. Returns KB ID."""
    print("\n[Step 2] Creating Bedrock Knowledge Base...")

    client = boto3.client("bedrock-agent", region_name=REGION)

    # Check if KB already exists
    try:
        resp = client.list_knowledge_bases(maxResults=100)
        for kb in resp.get("knowledgeBaseSummaries", []):
            if kb["name"] == "agentic-pipeline-resolutions":
                kb_id = kb["knowledgeBaseId"]
                print(f"  KB already exists: {kb_id}")
                return kb_id
    except Exception:
        pass

    embedding_model_arn = EMBEDDING_MODEL_ARN_TEMPLATE.format(region=REGION)

    resp = client.create_knowledge_base(
        name="agentic-pipeline-resolutions",
        description="Past resolution records — similar fixes, root causes, and PR outcomes",
        roleArn=kb_role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": embedding_model_arn,
            },
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": collection_arn,
                "vectorIndexName": INDEX_NAME,
                "fieldMapping": {
                    "vectorField": "embedding",
                    "textField": "text",
                    "metadataField": "metadata",
                },
            },
        },
    )

    kb_id = resp["knowledgeBase"]["knowledgeBaseId"]
    print(f"  Created KB: {kb_id}")
    return kb_id


def create_data_source(kb_id: str, bucket_arn: str) -> str:
    """Create S3 data source on the KB. Returns data source ID."""
    print("\n[Step 3] Creating S3 data source...")

    client = boto3.client("bedrock-agent", region_name=REGION)

    # Check if data source already exists
    try:
        resp = client.list_data_sources(knowledgeBaseId=kb_id, maxResults=10)
        for ds in resp.get("dataSourceSummaries", []):
            if ds["name"] == "resolution-documents":
                ds_id = ds["dataSourceId"]
                print(f"  Data source already exists: {ds_id}")
                return ds_id
    except Exception:
        pass

    resp = client.create_data_source(
        knowledgeBaseId=kb_id,
        name="resolution-documents",
        description="S3 bucket containing structured resolution records",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": bucket_arn,
                "inclusionPrefixes": ["resolutions/"],
            },
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": 512,
                    "overlapPercentage": 20,
                },
            },
        },
    )

    ds_id = resp["dataSource"]["dataSourceId"]
    print(f"  Created data source: {ds_id}")
    return ds_id


def update_lambda_env(function_name: str, kb_id: str, data_source_id: str):
    """Update the Ingestion Lambda with KB_ID and DATA_SOURCE_ID."""
    print("\n[Step 4] Updating Ingestion Lambda environment...")

    lambda_client = boto3.client("lambda", region_name=REGION)

    resp = lambda_client.get_function_configuration(FunctionName=function_name)
    env = resp.get("Environment", {}).get("Variables", {})
    env["KB_ID"] = kb_id
    env["DATA_SOURCE_ID"] = data_source_id

    lambda_client.update_function_configuration(
        FunctionName=function_name,
        Environment={"Variables": env},
    )
    print(f"  Updated {function_name}: KB_ID={kb_id}, DATA_SOURCE_ID={data_source_id}")


def seed_and_sync(bucket: str, kb_id: str, data_source_id: str):
    """Seed documents to S3 and trigger KB sync."""
    print("\n[Step 5] Seeding resolution documents...")

    # Import seed data
    sys.path.insert(0, str(Path(__file__).parent))
    from seed_knowledge_base import SEED_DOCUMENTS, build_document

    s3_client = boto3.client("s3", region_name=REGION)

    for record in SEED_DOCUMENTS:
        issue_key = record["issue_key"]
        doc = build_document(record)
        merged_at = record.get("merged_at", "")
        ts = merged_at.replace(":", "").replace("-", "")[:15] if merged_at else "seed"
        s3_key = f"resolutions/{issue_key}/{ts}.md"

        s3_client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=doc.encode("utf-8"),
            ContentType="text/markdown",
        )
        print(f"  Uploaded: s3://{bucket}/{s3_key}")

    print(f"\n  {len(SEED_DOCUMENTS)} documents uploaded")

    # Trigger sync
    print("\n[Step 6] Triggering KB sync...")
    bedrock_client = boto3.client("bedrock-agent", region_name=REGION)
    resp = bedrock_client.start_ingestion_job(
        knowledgeBaseId=kb_id,
        dataSourceId=data_source_id,
    )
    job_id = resp["ingestionJob"]["ingestionJobId"]
    status = resp["ingestionJob"]["status"]
    print(f"  Sync job started: {job_id} (status: {status})")
    print(f"  Monitor: aws bedrock-agent get-ingestion-job --knowledge-base-id {kb_id} --data-source-id {data_source_id} --ingestion-job-id {job_id} --region {REGION}")
    return job_id


def main():
    parser = argparse.ArgumentParser(description="Post-deploy Knowledge Base setup")
    parser.add_argument("--no-seed", action="store_true", help="Skip seeding documents")
    parser.add_argument("--skip-index", action="store_true", help="Skip AOSS index creation (already exists)")
    args = parser.parse_args()

    print("=" * 60)
    print("Knowledge Base Setup")
    print("=" * 60)

    # Read stack outputs
    print("\nReading KnowledgeBaseStack outputs...")
    try:
        outputs = get_stack_outputs()
    except Exception as e:
        print(f"ERROR: Could not read KnowledgeBaseStack outputs: {e}")
        print("Make sure you've run: cdk deploy KnowledgeBaseStack")
        sys.exit(1)

    collection_endpoint = outputs["CollectionEndpoint"]
    collection_arn = outputs["CollectionArn"]
    kb_role_arn = outputs["KBRoleArn"]
    bucket_name = outputs["DataBucketName"]
    bucket_arn = outputs["DataBucketArn"]
    ingestion_fn = outputs["IngestionFunctionName"]

    print(f"  Collection: {collection_endpoint}")
    print(f"  Bucket: {bucket_name}")
    print(f"  KB Role: {kb_role_arn}")

    # Step 1: Create AOSS index
    if args.skip_index:
        print("\n[Step 1] Skipping AOSS index creation (--skip-index)")
    elif not create_aoss_index(collection_endpoint):
        print("\nFATAL: Could not create AOSS index. Aborting.")
        sys.exit(1)

    # Step 2: Create Bedrock KB
    kb_id = create_knowledge_base(collection_arn, kb_role_arn)

    # Step 3: Create data source
    data_source_id = create_data_source(kb_id, bucket_arn)

    # Step 4: Update Lambda env
    update_lambda_env(ingestion_fn, kb_id, data_source_id)

    # Steps 5-6: Seed and sync
    if not args.no_seed:
        seed_and_sync(bucket_name, kb_id, data_source_id)

    # Summary
    print("\n" + "=" * 60)
    print("Setup complete!")
    print("=" * 60)
    print(f"\n  KB ID:          {kb_id}")
    print(f"  Data Source ID: {data_source_id}")
    print(f"  Bucket:         {bucket_name}")
    print(f"\nTo wire into the orchestrator:")
    print(f"  cdk deploy OrchestratorStack --parameters KnowledgeBaseId={kb_id}")
    print(f"\nTo query the KB:")
    print(f"  aws bedrock-agent-runtime retrieve --knowledge-base-id {kb_id} --retrieval-query '{{\"text\": \"NullPointerException in OrderService\"}}' --region {REGION}")


if __name__ == "__main__":
    main()
