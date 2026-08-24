#!/usr/bin/env python3
"""
Seed the Knowledge Base with sample resolution documents.

Usage:
  # Creates AOSS index, writes documents to S3, and triggers a KB sync job
  python scripts/seed_knowledge_base.py

  # Just write to S3 (skip sync — useful if KB isn't deployed yet)
  python scripts/seed_knowledge_base.py --no-sync

  # Skip index creation (already exists)
  python scripts/seed_knowledge_base.py --skip-index

  # Write to a local directory for inspection
  python scripts/seed_knowledge_base.py --local ./seed-output

Environment variables:
  KB_DATA_BUCKET   — S3 bucket (default: auto-detect from CloudFormation)
  KB_ID            — Bedrock KB ID (default: auto-detect from CloudFormation)
  DATA_SOURCE_ID   — Bedrock data source ID (default: auto-detect from CloudFormation)
  AWS_REGION       — Region (default: us-east-1)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow importing from orchestrator/
sys.path.insert(0, str(Path(__file__).parent.parent / "orchestrator"))

SEED_DOCUMENTS = [
    {
        "issue_key": "CHECKOUT-101",
        "rca_summary": "NullPointerException in OrderService.processOrder() at line 42. The cart items list was null when a user had an expired session cookie and hit checkout directly from a bookmarked URL.",
        "resolution_summary": "Added defensive null check for cart items in processOrder(). Initialized empty ArrayList in the constructor instead of relying on lazy initialization from session. Added integration test covering the expired-session-direct-checkout path.",
        "pr_url": "https://bitbucket.org/acme/order-service/pull-requests/287",
        "repo_url": "https://bitbucket.org/acme/order-service.git",
        "files_changed": [
            "src/main/java/com/acme/order/OrderService.java",
            "src/main/java/com/acme/order/CartManager.java",
            "src/test/java/com/acme/order/OrderServiceExpiredSessionTest.java",
        ],
        "category": "PROD_INCIDENT",
        "merged_at": "2026-06-15T09:30:00Z",
        "metadata": {
            "service": "order-service",
            "component": "checkout",
            "error_pattern": "java.lang.NullPointerException\n\tat com.acme.order.OrderService.processOrder(OrderService.java:42)\n\tat com.acme.order.CheckoutController.submit(CheckoutController.java:88)",
        },
    },
    {
        "issue_key": "API-205",
        "rca_summary": "Connection pool exhaustion in api-gateway causing HTTP 503 errors under load. Default HikariCP maxPoolSize=10 was insufficient for peak traffic (200+ concurrent requests). Connections were leaked by the ReportService which didn't close ResultSet in finally block.",
        "resolution_summary": "Two-part fix: (1) Increased HikariCP maxPoolSize to 50 and added connectionTimeout=5000ms in application.yaml. (2) Fixed resource leak in ReportService.generateReport() by wrapping ResultSet usage in try-with-resources. Added connection pool metrics to CloudWatch dashboard.",
        "pr_url": "https://bitbucket.org/acme/api-gateway/pull-requests/412",
        "repo_url": "https://bitbucket.org/acme/api-gateway.git",
        "files_changed": [
            "src/main/resources/application.yaml",
            "src/main/java/com/acme/api/report/ReportService.java",
            "infra/cloudwatch-dashboard.json",
        ],
        "category": "PROD_INCIDENT",
        "merged_at": "2026-06-22T16:45:00Z",
        "metadata": {
            "service": "api-gateway",
            "component": "connection-pool",
            "error_pattern": "HikariPool-1 - Connection is not available, request timed out after 30000ms.\njava.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available",
        },
    },
    {
        "issue_key": "NOTIFY-88",
        "rca_summary": "Email notifications silently failing for 12 hours. SES sending quota exceeded (200/day on sandbox) after marketing campaign blast. No alarm existed for SES bounce/complaint rates or quota usage.",
        "resolution_summary": "Moved SES out of sandbox to production access (filed request, approved same day). Added CloudWatch alarms for SES Daily Sending Quota (threshold 80%), Bounce Rate (>5%), and Complaint Rate (>0.1%). Added SNS DLQ for failed sends with retry Lambda. Also added circuit breaker pattern — falls back to SNS SMS for critical notifications when email delivery rate drops below 90%.",
        "pr_url": "https://bitbucket.org/acme/notification-service/pull-requests/156",
        "repo_url": "https://bitbucket.org/acme/notification-service.git",
        "files_changed": [
            "infra/ses-alarms.tf",
            "src/notifications/email_sender.py",
            "src/notifications/circuit_breaker.py",
            "src/notifications/tests/test_circuit_breaker.py",
            "infra/dlq.tf",
        ],
        "category": "PROD_INCIDENT",
        "merged_at": "2026-07-01T11:20:00Z",
        "metadata": {
            "service": "notification-service",
            "component": "email",
            "error_pattern": "botocore.exceptions.ClientError: An error occurred (Throttling) when calling the SendEmail operation: Daily message quota exceeded.",
        },
    },
    {
        "issue_key": "AUTH-342",
        "rca_summary": "Intermittent 401 errors after token refresh. Race condition in TokenRefreshInterceptor: two concurrent requests could both detect an expired token, both trigger refresh, and the second refresh would invalidate the token the first request just started using.",
        "resolution_summary": "Replaced double-checked locking with a ReentrantLock in TokenRefreshInterceptor. Only one thread performs the refresh; others wait and reuse the new token. Added jitter to token refresh timing (refresh at 80-90% of expiry instead of exactly 85%) to reduce thundering herd. Unit test with 50 concurrent threads validates no double-refresh.",
        "pr_url": "https://bitbucket.org/acme/auth-sdk/pull-requests/89",
        "repo_url": "https://bitbucket.org/acme/auth-sdk.git",
        "files_changed": [
            "src/main/java/com/acme/auth/TokenRefreshInterceptor.java",
            "src/test/java/com/acme/auth/ConcurrentTokenRefreshTest.java",
        ],
        "category": "BUG_TICKET",
        "merged_at": "2026-06-28T14:00:00Z",
        "metadata": {
            "service": "auth-sdk",
            "component": "token-refresh",
            "error_pattern": "HTTP 401 Unauthorized\nWWW-Authenticate: Bearer error=\"invalid_token\", error_description=\"Token has been revoked\"",
        },
    },
    {
        "issue_key": "INGEST-77",
        "rca_summary": "Data pipeline DynamoDB write throttling during nightly batch. Partition key was date-string (YYYY-MM-DD), causing hot partition — all 500K records/night hit a single partition. Provisioned WCU=1000 but effective limit per partition is 1000 WCU anyway.",
        "resolution_summary": "Redesigned partition key to use composite key: {tenant_id}#{date}#{shard_n} where shard is hash(record_id) % 10. This distributes writes across ~10 partitions per date. Switched to on-demand capacity mode for the batch table. Added exponential backoff with jitter to the batch writer. Backfill ran overnight with no throttling.",
        "pr_url": "https://bitbucket.org/acme/data-pipeline/pull-requests/203",
        "repo_url": "https://bitbucket.org/acme/data-pipeline.git",
        "files_changed": [
            "src/ingestion/batch_writer.py",
            "src/ingestion/partition_strategy.py",
            "infra/dynamodb.tf",
            "tests/test_partition_strategy.py",
        ],
        "category": "PROD_INCIDENT",
        "merged_at": "2026-07-05T08:15:00Z",
        "metadata": {
            "service": "data-pipeline",
            "component": "ingestion",
            "error_pattern": "botocore.exceptions.ClientError: An error occurred (ProvisionedThroughputExceededException) when calling the BatchWriteItem operation",
        },
    },
    {
        "issue_key": "SEARCH-156",
        "rca_summary": "Elasticsearch cluster red status — primary shard unassigned after a node restart. Root cause: disk watermark exceeded (90% usage) on the data node, ES refused to allocate shards. Index lifecycle management (ILM) rollover was configured but the delete phase was missing, so indices grew indefinitely.",
        "resolution_summary": "Added delete phase to ILM policy (delete indices older than 30 days). Cleaned up 45 stale indices manually to free disk. Increased EBS volume from 100GB to 500GB to handle growth. Added CloudWatch alarm for FreeStorageSpace < 20GB. Verified shard allocation recovered automatically after disk freed.",
        "pr_url": "https://bitbucket.org/acme/search-infra/pull-requests/67",
        "repo_url": "https://bitbucket.org/acme/search-infra.git",
        "files_changed": [
            "elasticsearch/ilm-policy.json",
            "terraform/es-cluster.tf",
            "monitoring/es-alarms.tf",
        ],
        "category": "PROD_INCIDENT",
        "merged_at": "2026-07-10T13:00:00Z",
        "metadata": {
            "service": "search-platform",
            "component": "elasticsearch",
            "error_pattern": "ClusterBlockException[blocked by: [FORBIDDEN/12/index read-only / allow delete (api)]]; shard has exceeded the [90.0%] flood stage watermark",
        },
    },
    {
        "issue_key": "DEPLOY-44",
        "rca_summary": "CloudFormation stack update failed with 'UPDATE_ROLLBACK_COMPLETE' after Lambda function timeout change. The function had a reserved concurrency of 5, but the new timeout of 900s combined with average execution time caused all 5 slots to be occupied, triggering throttling of new invocations.",
        "resolution_summary": "Increased reserved concurrency from 5 to 25 to accommodate longer-running executions. Added provisioned concurrency of 3 for warm starts. Fixed the underlying slow query that necessitated the timeout increase — added composite index on (tenant_id, created_at) to the reporting table. Net result: p99 latency dropped from 120s to 8s.",
        "pr_url": "https://bitbucket.org/acme/report-generator/pull-requests/91",
        "repo_url": "https://bitbucket.org/acme/report-generator.git",
        "files_changed": [
            "infra/lambda.tf",
            "src/reports/query_builder.py",
            "migrations/005_add_composite_index.sql",
        ],
        "category": "BUG_TICKET",
        "merged_at": "2026-07-12T10:30:00Z",
        "metadata": {
            "service": "report-generator",
            "component": "lambda",
            "error_pattern": "TooManyRequestsException: Rate Exceeded.\nThrottle reason: ReservedFunctionConcurrentInvocationLimitExceeded",
        },
    },
    {
        "issue_key": "PAYMENTS-221",
        "rca_summary": "Payment processing returning incorrect amounts for JPY transactions. The currency conversion logic was applying decimal division (amount / 100) to zero-decimal currencies like JPY, YEN, KRW. A ¥1000 charge was being processed as ¥10.",
        "resolution_summary": "Added zero-decimal currency detection using ISO 4217 lookup. Currencies with exponent=0 (JPY, KRW, VND, etc.) skip the decimal conversion step. Added comprehensive test matrix covering all supported currencies including edge cases (BHD with 3 decimals, CLF with 4). Backfill script created to identify and flag affected transactions for manual review.",
        "pr_url": "https://bitbucket.org/acme/payment-engine/pull-requests/178",
        "repo_url": "https://bitbucket.org/acme/payment-engine.git",
        "files_changed": [
            "src/payments/currency.py",
            "src/payments/charge_processor.py",
            "src/payments/data/iso4217.json",
            "tests/test_currency_conversion.py",
            "scripts/identify_affected_transactions.sql",
        ],
        "category": "PROD_INCIDENT",
        "merged_at": "2026-06-20T07:45:00Z",
        "metadata": {
            "service": "payment-engine",
            "component": "currency-conversion",
            "error_pattern": "AssertionError: Expected charge amount 1000 JPY but got 10 JPY",
        },
    },
    {
        "issue_key": "CACHE-91",
        "rca_summary": "Redis cluster OOM kill during peak hours. Memory usage spiked from 4GB to 12GB (max) in 3 minutes. Root cause: a background job was caching full user profile objects (avg 15KB) for ALL users in a batch operation, instead of only active users. The job ran every 5 minutes and the TTL was 10 minutes, causing overlap.",
        "resolution_summary": "Changed the cache-warming job to only pre-cache users with activity in the last 24h (reduces from 2M to ~50K users). Reduced cached object size from full profile (15KB) to cache-optimized projection (2KB) with only the fields needed for the hot path. Set TTL to 5 minutes (matching job interval) to prevent overlap accumulation. Added Redis memory usage alarm at 70% threshold.",
        "pr_url": "https://bitbucket.org/acme/user-service/pull-requests/334",
        "repo_url": "https://bitbucket.org/acme/user-service.git",
        "files_changed": [
            "src/cache/user_cache_warmer.py",
            "src/models/user_cache_projection.py",
            "infra/redis-alarms.tf",
            "tests/test_cache_warmer.py",
        ],
        "category": "PROD_INCIDENT",
        "merged_at": "2026-07-08T15:20:00Z",
        "metadata": {
            "service": "user-service",
            "component": "redis-cache",
            "error_pattern": "OOM command not allowed when used memory > 'maxmemory'.\nRedis::CommandError: OOM",
        },
    },
    {
        "issue_key": "FRONTEND-512",
        "rca_summary": "React app white screen on Safari 15. The app used optional chaining with assignment (??=) which is unsupported in Safari < 16. Babel config excluded node_modules but a transitive dependency (chart-utils) shipped untranspiled ES2022.",
        "resolution_summary": "Added chart-utils to the Babel include list in webpack config. Pinned browserslist target to 'safari >= 15' in package.json. Added a build-time check script that runs es-check es2020 on the final bundle to catch future regressions. Considered alternative: replace chart-utils with recharts (already transpiled), deferred to tech debt ticket.",
        "pr_url": "https://bitbucket.org/acme/dashboard-ui/pull-requests/901",
        "repo_url": "https://bitbucket.org/acme/dashboard-ui.git",
        "files_changed": [
            "webpack.config.js",
            "package.json",
            "scripts/check-bundle-compat.sh",
        ],
        "category": "BUG_TICKET",
        "merged_at": "2026-07-14T12:00:00Z",
        "metadata": {
            "service": "dashboard-ui",
            "component": "frontend-build",
            "error_pattern": "SyntaxError: Unexpected token '??='\nSafari 15.6.1",
        },
    },
]


AOSS_INDEX_NAME = "resolution-fixes"
EMBEDDING_DIMENSIONS = 1024


def create_aoss_index(collection_endpoint: str):
    """Create the vector index in AOSS using opensearch-py with AWS4Auth."""
    import boto3
    from opensearchpy import OpenSearch, RequestsHttpConnection
    from requests_aws4auth import AWS4Auth

    region = os.environ.get("AWS_REGION", "us-east-1")
    credentials = boto3.Session().get_credentials()
    awsauth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        region,
        "aoss",
        session_token=credentials.token,
    )

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
            client.indices.create(index=AOSS_INDEX_NAME, body=index_body)
            print(f"  Index '{AOSS_INDEX_NAME}' created successfully")
            return True
        except Exception as e:
            error_str = str(e)
            if "resource_already_exists_exception" in error_str:
                print(f"  Index '{AOSS_INDEX_NAME}' already exists — OK")
                return True
            if "403" in error_str and attempt < max_retries - 1:
                wait = min(2 ** attempt * 5, 60)
                print(f"  Got 403 (policy propagating), retrying in {wait}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"  ERROR creating index: {error_str}")
            return False

    print("  ERROR: Exhausted retries creating AOSS index")
    return False


def build_document(record: dict) -> str:
    """Build markdown document from a resolution record (mirrors kb_ingestion._build_document)."""
    from kb_ingestion import _build_document
    return _build_document(record)


def write_local(output_dir: str):
    """Write seed documents to a local directory for inspection."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    for record in SEED_DOCUMENTS:
        issue_key = record["issue_key"]
        doc = build_document(record)
        file_path = out / f"{issue_key}.md"
        file_path.write_text(doc)
        print(f"  Written: {file_path}")

    print(f"\n{len(SEED_DOCUMENTS)} documents written to {out}/")


def write_s3(bucket: str):
    """Write seed documents to S3."""
    import boto3
    s3_client = boto3.client("s3")

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
            Metadata={
                "issue_key": issue_key,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "seed": "true",
            },
        )
        print(f"  Uploaded: s3://{bucket}/{s3_key}")

    print(f"\n{len(SEED_DOCUMENTS)} documents uploaded to s3://{bucket}/resolutions/")


def trigger_sync(kb_id: str, data_source_id: str):
    """Trigger a Bedrock KB ingestion job."""
    import boto3
    client = boto3.client("bedrock-agent")
    response = client.start_ingestion_job(
        knowledgeBaseId=kb_id,
        dataSourceId=data_source_id,
    )
    job_id = response["ingestionJob"]["ingestionJobId"]
    status = response["ingestionJob"]["status"]
    print(f"\nSync job started: {job_id} (status: {status})")
    print(f"  Monitor: aws bedrock-agent get-ingestion-job --knowledge-base-id {kb_id} --data-source-id {data_source_id} --ingestion-job-id {job_id}")
    return job_id


def get_stack_outputs():
    """Auto-detect bucket/KB/datasource/endpoint from CloudFormation outputs."""
    import boto3
    cfn = boto3.client("cloudformation")
    try:
        resp = cfn.describe_stacks(StackName="KnowledgeBaseStack")
        outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
        return {
            "bucket": outputs.get("DataBucketName", ""),
            "kb_id": outputs.get("KnowledgeBaseId", ""),
            "data_source_id": outputs.get("DataSourceId", ""),
            "collection_endpoint": outputs.get("CollectionEndpoint", ""),
        }
    except Exception:
        return {"bucket": "", "kb_id": "", "data_source_id": "", "collection_endpoint": ""}


def main():
    parser = argparse.ArgumentParser(description="Seed the Knowledge Base with sample resolution documents")
    parser.add_argument("--local", metavar="DIR", help="Write to local directory instead of S3")
    parser.add_argument("--no-sync", action="store_true", help="Upload to S3 but skip KB sync")
    parser.add_argument("--skip-index", action="store_true", help="Skip AOSS index creation (already exists)")
    parser.add_argument("--bucket", help="S3 bucket name (overrides auto-detect)")
    parser.add_argument("--kb-id", help="Bedrock KB ID (overrides auto-detect)")
    parser.add_argument("--data-source-id", help="Bedrock data source ID (overrides auto-detect)")
    parser.add_argument("--collection-endpoint", help="AOSS collection endpoint (overrides auto-detect)")
    args = parser.parse_args()

    if args.local:
        write_local(args.local)
        return

    # Auto-detect from env vars or CloudFormation
    bucket = args.bucket or os.getenv("KB_DATA_BUCKET", "")
    kb_id = args.kb_id or os.getenv("KB_ID", "")
    data_source_id = args.data_source_id or os.getenv("DATA_SOURCE_ID", "")
    collection_endpoint = args.collection_endpoint or os.getenv("COLLECTION_ENDPOINT", "")

    if not bucket:
        print("Auto-detecting from KnowledgeBaseStack CloudFormation outputs...")
        stack_outputs = get_stack_outputs()
        bucket = bucket or stack_outputs["bucket"]
        kb_id = kb_id or stack_outputs["kb_id"]
        data_source_id = data_source_id or stack_outputs["data_source_id"]
        collection_endpoint = collection_endpoint or stack_outputs["collection_endpoint"]

    if not bucket:
        print("ERROR: No bucket found. Either deploy KnowledgeBaseStack first, or pass --bucket.")
        print("       Use --local ./output to write locally for inspection.")
        sys.exit(1)

    print(f"Bucket: {bucket}")
    print(f"KB ID: {kb_id or '(not set — will skip sync)'}")
    print(f"Data Source: {data_source_id or '(not set — will skip sync)'}")
    print(f"Collection: {collection_endpoint or '(not set — will skip index creation)'}")
    print()

    # Step 1: Create AOSS vector index (post-deploy requirement)
    if not args.skip_index and collection_endpoint:
        print("Step 1: Creating AOSS vector index...")
        if not create_aoss_index(collection_endpoint):
            print("WARNING: Index creation failed. Sync will fail without the index.")
            print("         Fix the issue and re-run, or create the index manually.")
        print()
    elif not args.skip_index:
        print("Step 1: Skipping index creation (no collection endpoint available)")
        print()

    # Step 2: Upload documents to S3
    print("Step 2: Uploading resolution documents to S3...")
    write_s3(bucket)

    # Step 3: Trigger KB sync
    if not args.no_sync and kb_id and data_source_id:
        print("\nStep 3: Triggering KB sync...")
        trigger_sync(kb_id, data_source_id)
    elif not args.no_sync:
        print("\nStep 3: Skipping sync — KB_ID or DATA_SOURCE_ID not available.")
        print("Run sync manually: aws bedrock-agent start-ingestion-job ...")


if __name__ == "__main__":
    main()
