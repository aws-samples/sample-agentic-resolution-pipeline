# Deployment Guide

Complete setup guide for the Agentic Resolution Pipeline. Follow these steps in order to deploy the entire system from scratch.

---

## Prerequisites

### AWS Account

You need an AWS account with access to the following services:

| Service | Purpose |
|---------|---------|
| ECS (Fargate) | Resolution worker containers |
| Lambda | Orchestrator functions, webhook receiver, MCP server |
| Step Functions | Pipeline orchestration state machine |
| DynamoDB | Task tokens, idempotency store |
| S3 | Knowledge base data, resolution output, frontend hosting |
| SNS | Pipeline notifications |
| Bedrock | Claude models, Knowledge Bases, Guardrails |
| AgentCore | Planner agent + Resolution agent runtimes (Strands) |
| CloudWatch | Logs, alarms |
| API Gateway | Webhook and MCP endpoints |
| Secrets Manager | Jira API token, repo credentials, webhook secrets |
| ElastiCache (Redis) | IoT sample app caching |
| OpenSearch Serverless | Knowledge base vector store |
| CloudFront | IoT dashboard CDN |

### Local Tools

- **Docker Desktop** running (required for building container images)
- **Node.js 20+** (for CDK and frontend build)
- **Python 3.12+** (for Lambda code and scripts)
- **AWS CLI v2** configured with credentials for your target account

### External Services

- **Jira Cloud** tenant (Atlassian account)
- **Bitbucket Cloud** workspace (for target repos and PR creation)
- **AWS DevOps Agent** access (for automated investigation)

---

## Step 1: Clone and Install

```bash
git clone <YOUR_REPO_URL> agentic-resolution-pipeline
cd agentic-resolution-pipeline

# Set your target region (used throughout this guide)
export REGION=us-east-1  # or us-west-2, eu-west-1, etc.

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install CDK CLI (pinned version) and Python dependencies
cd infrastructure && npm install && cd ..
pip install -r infrastructure/requirements.txt
```

### VPC Setup

Several stacks (ECS worker, Resolution tasks) require a VPC with private and public subnets. You need a VPC ID before proceeding — CDK requires it even during synthesis.

**Requirements:**
- At least 2 private subnets (for ECS Fargate tasks)
- At least 2 public subnets (for NAT Gateway / internet access)
- NAT Gateway or VPC endpoints for AWS services

**Use an existing VPC:**
```bash
export VPC_ID=vpc-0abc123def456
```

**Or create a new one** (default VPC works for testing):
```bash
aws ec2 create-default-vpc --region $REGION 2>/dev/null || true
export VPC_ID=$(aws ec2 describe-vpcs --region $REGION \
  --filters Name=is-default,Values=true \
  --query "Vpcs[0].VpcId" --output text)
echo "Using VPC: $VPC_ID"
```

### Bootstrap CDK

CDK requires a one-time bootstrap per account/region. This creates the staging bucket and roles CDK uses for deployments:

```bash
cd infrastructure
npx cdk bootstrap aws://<YOUR_ACCOUNT_ID>/$REGION -c vpc_id=$VPC_ID
cd ..
```

### Verify CDK Synthesis

```bash
cd infrastructure
AWS_REGION=$REGION npx cdk synth --quiet -c vpc_id=$VPC_ID
cd ..
```

This validates that all 7 stacks synthesize correctly with your Python, CDK, and AWS credentials.

---

## Step 2: Jira Setup

### Create a Jira Project

1. Go to your Jira Cloud instance (e.g., `https://<YOUR_JIRA_TENANT>.atlassian.net`)
2. Create a new project:
   - Template: **Scrum** or **Kanban**
   - Name: e.g., `IoT Fleet Management`
   - Key: e.g., `IOT`
3. Ensure the project has a `Bug` issue type
4. Add a workflow transition to the `In Review` status (used as an approval gate)

### Create a Jira API Token

1. Go to: https://id.atlassian.com/manage-profile/security/api-tokens
2. Click **Create API token**
3. Label: `agentic-pipeline`
4. Copy and save the token (you will not see it again)
5. Note your service account email (the Atlassian account email that owns the token)

You will need:
- `<YOUR_JIRA_EMAIL>` - the email associated with the API token
- `<YOUR_JIRA_API_TOKEN>` - the token value
- `<YOUR_JIRA_TENANT>` - subdomain (e.g., `mycompany` for `mycompany.atlassian.net`)

---

## Step 3: Bitbucket Setup

### Create a Bitbucket Workspace

1. Go to https://bitbucket.org and create a workspace (or use an existing one)
2. Note the workspace name (slug): `<YOUR_BB_WORKSPACE>`

### Create an API Token

Bitbucket has deprecated App Passwords in favor of scoped API tokens.

1. Click your profile avatar → **Account settings**
2. On the Atlassian Account page, select the **Security** tab
3. Click **Create and manage API tokens**
4. Click **Create API token with scopes**
5. Configure:
   - **Name**: `agentic-pipeline`
   - **App**: Bitbucket
   - **Scopes** required:
     - `read:repository:bitbucket`
     - `write:repository:bitbucket`
     - `read:pullrequest:bitbucket`
     - `write:pullrequest:bitbucket`
     - `read:account`
6. Copy and save the token (it is only shown once)

You will need:
- `<YOUR_BB_EMAIL>` - the Bitbucket/Atlassian account email
- `<YOUR_BB_API_TOKEN>` - the API token value
- `<YOUR_BB_WORKSPACE>` - workspace slug

### Create or Identify the Target Repository

Either push the IoT sample app to Bitbucket:
```bash
cd sample-app/iot-fleet-management
git init && git add . && git commit -m "Initial commit"
git remote add origin https://bitbucket.org/<YOUR_BB_WORKSPACE>/iot-fleet-management.git
git push -u origin main
```

Or use any existing repo that contains application code the agent should fix.

---

## Step 4: Deploy Infrastructure (7 Pipeline Stacks)

### Option A: Makefile (Recommended)

```bash
make deploy-pipeline VPC_ID=<YOUR_VPC_ID>
```

This deploys all 7 stacks in the correct order with `--require-approval never`.

### Option B: Manual CDK Deploy (Step by Step)

If you need to pass specific parameters or deploy incrementally:

```bash
cd infrastructure

# Set your target region (must match cdk bootstrap region)
export REGION=us-east-1  # or us-west-2, eu-west-1, etc.

# 1. Guardrails (no dependencies)
AWS_REGION=$REGION npx cdk deploy GuardrailStack --require-approval never

# 2. Knowledge Base infrastructure (AOSS collection, S3 bucket, IAM)
AWS_REGION=$REGION npx cdk deploy KnowledgeBaseStack --require-approval never

# 3. Planner AgentCore Runtime (requires Docker running)
#    After deploy, note the AgentRuntimeArn from stack outputs
AWS_REGION=$REGION npx cdk deploy PlannerAgentCoreStack \
  --parameters KnowledgeBaseId=<KB_ID> \
  --parameters GuardrailId=<GUARDRAIL_ID> \
  --require-approval never

# 4. Resolution AgentCore Runtime + ECS Worker (requires Docker running)
#    After deploy, note the DispatcherFunctionArn from stack outputs
AWS_REGION=$REGION npx cdk deploy ResolutionAgentCoreStack \
  --parameters GuardrailId=<GUARDRAIL_ID> \
  --require-approval never

# 5. Orchestrator (wires everything together)
AWS_REGION=$REGION npx cdk deploy OrchestratorStack \
  --parameters KnowledgeBaseId=<KB_ID> \
  --parameters GuardrailId=<GUARDRAIL_ID> \
  --parameters GuardrailVersion=1 \
  --parameters PlannerRuntimeArn=<PLANNER_RUNTIME_ARN> \
  --parameters ResolutionDispatcherArn=<DISPATCHER_LAMBDA_ARN> \
  --parameters DevOpsAgentWebhookUrl=<DEVOPS_AGENT_WEBHOOK_URL> \
  --require-approval never

# 6. Jira Intake (API Gateway + webhook + MCP server)
AWS_REGION=$REGION npx cdk deploy JiraIntakeStack \
  --parameters JiraBaseUrl=https://<YOUR_JIRA_TENANT>.atlassian.net \
  --require-approval never

# 7. Shared resolution infrastructure (ECS cluster, S3 output bucket, secrets)
AWS_REGION=$REGION npx cdk deploy ResolutionStack --require-approval never
```

### VPC

Use the `VPC_ID` you set up in Step 1. Both the Makefile and manual CDK commands pass it as CDK context:
```bash
# Via Makefile
make deploy-pipeline VPC_ID=$VPC_ID

# Via CDK context
npx cdk deploy --all -c vpc_id=$VPC_ID
```

### Stack Parameters Reference

| Stack | Parameter | Source |
|-------|-----------|--------|
| PlannerAgentCoreStack | `KnowledgeBaseId` | Output of `setup_knowledge_base.py` |
| PlannerAgentCoreStack | `GuardrailId` | Output of GuardrailStack |
| ResolutionAgentCoreStack | `GuardrailId` | Output of GuardrailStack |
| OrchestratorStack | `KnowledgeBaseId` | Output of `setup_knowledge_base.py` |
| OrchestratorStack | `GuardrailId` | Output of GuardrailStack |
| OrchestratorStack | `GuardrailVersion` | Usually `1` |
| OrchestratorStack | `PlannerRuntimeArn` | Output of PlannerAgentCoreStack |
| OrchestratorStack | `ResolutionDispatcherArn` | Output of ResolutionAgentCoreStack |
| OrchestratorStack | `DevOpsAgentWebhookUrl` | From DevOps Agent console (Step 9) |
| JiraIntakeStack | `JiraBaseUrl` | `https://<YOUR_JIRA_TENANT>.atlassian.net` |

---

## Step 5: Post-Deploy Configuration -- Secrets

After the stacks deploy, populate the secrets in AWS Secrets Manager.

### Jira API Secret

The JiraIntakeStack creates a secret. Populate it with your Jira credentials:

```bash
aws secretsmanager put-secret-value \
  --secret-id JiraApiSecret \
  --secret-string '{"email":"<YOUR_JIRA_EMAIL>","api_token":"<YOUR_JIRA_API_TOKEN>"}' \
  --region $REGION
```

Or find the exact secret name from the stack output:
```bash
aws cloudformation describe-stacks --stack-name JiraIntakeStack \
  --query "Stacks[0].Outputs[?OutputKey=='JiraApiSecretArn'].OutputValue" \
  --output text --region $REGION
```

### Repository Credentials Secret

The ResolutionStack creates a secret for git clone/push authentication. Store it as `email:api_token` (no newlines, no trailing whitespace):

```bash
aws secretsmanager put-secret-value \
  --secret-id agentic-pipeline/repo-credentials \
  --secret-string '<YOUR_BB_EMAIL>:<YOUR_BB_API_TOKEN>' \
  --region $REGION
```

> **Caution:** When pasting long API tokens from the browser, some terminals insert line breaks. Verify with: `aws secretsmanager get-secret-value --secret-id agentic-pipeline/repo-credentials --region $REGION --query SecretString --output text | wc -l` — should return `1` (a single line).

### Bitbucket Webhook Secret (for PR merge events)

```bash
aws secretsmanager put-secret-value \
  --secret-id agentic-pipeline/bitbucket-webhook-secret \
  --secret-string '<A_RANDOM_SECRET_STRING>' \
  --region $REGION
```

Generate a random secret:
```bash
openssl rand -hex 32
```

---

## Step 6: Knowledge Base Setup

After KnowledgeBaseStack deploys, run the post-deploy script to create the AOSS vector index, Bedrock Knowledge Base, and seed initial resolution documents:

```bash
python scripts/setup_knowledge_base.py
```

This script:
1. Creates an OpenSearch Serverless vector index (embedding dimension: 1024)
2. Creates a Bedrock Knowledge Base pointing to the AOSS collection
3. Creates a Data Source pointing to the S3 bucket
4. Seeds 10 sample resolution documents
5. Triggers a KB sync

Note the output values:
- `KnowledgeBaseId` - needed for PlannerAgentCoreStack and OrchestratorStack
- `DataSourceId` - for reference

If you deployed stacks without the KB ID (using the Makefile), redeploy the dependent stacks after this step:
```bash
cd infrastructure
AWS_REGION=$REGION npx cdk deploy PlannerAgentCoreStack OrchestratorStack \
  --parameters KnowledgeBaseId=<KB_ID> \
  --require-approval never
```

---

## Step 7: Jira Webhook Configuration

Configure Jira to send events to your pipeline's webhook endpoint.

### Get Your Webhook URL

```bash
aws cloudformation describe-stacks --stack-name JiraIntakeStack \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" \
  --output text --region $REGION
```

The URL will look like: `https://<API_ID>.execute-api.<YOUR_REGION>.amazonaws.com/prod/webhook`

### Create the Webhook in Jira

1. Go to: `https://<YOUR_JIRA_TENANT>.atlassian.net/plugins/servlet/webhooks`
   (or Jira Settings -> System -> Webhooks)
2. Click **Create a WebHook**
3. Configure:
   - **Name**: `agentic-pipeline`
   - **Status**: Enabled
   - **URL**: `<YOUR_WEBHOOK_URL>` (from above)
   - **Secret**: The value you stored in `agentic-pipeline/bitbucket-webhook-secret` (or a separate Jira webhook secret)
   - **JQL Filter**: `project in (IOT) AND issuetype = Bug`
   - **Events**:
     - Issue: created, updated
     - Comment: created
4. Click **Create**

### Verify

Create a test Bug in your Jira project. You should see:
- API Gateway access logs (if enabled)
- The webhook receiver Lambda invocation in CloudWatch
- A new Step Functions execution

---

## Step 8: Bitbucket Webhook Configuration

Configure Bitbucket to notify the pipeline when PRs are merged (triggers KB ingestion).

### Configure the Repository Allowlist

The PR merge handler only processes webhooks from repos in its allowlist. Pass your repos when deploying JiraIntakeStack:

```bash
cdk deploy JiraIntakeStack \
  --parameters RepoAllowlist="https://bitbucket.org/<WORKSPACE>/<REPO1>,https://bitbucket.org/<WORKSPACE>/<REPO2>"
```

This must match the repo URLs in your `repo-config.yaml` (without `.git` suffix).

### Get Your PR Merge Endpoint

The JiraIntakeStack exposes a `/pr-merge` endpoint on the same API Gateway:
```
https://<API_ID>.execute-api.<YOUR_REGION>.amazonaws.com/prod/pr-merge
```

### Create the Webhook in Bitbucket

1. Go to: `https://bitbucket.org/<YOUR_BB_WORKSPACE>/<YOUR_REPO>/admin/webhooks`
   (Repository Settings -> Webhooks -> Add webhook)
2. Configure:
   - **Title**: `agentic-pipeline-kb-ingestion`
   - **URL**: `https://<API_ID>.execute-api.<YOUR_REGION>.amazonaws.com/prod/pr-merge`
   - **Secret**: The value from `agentic-pipeline/bitbucket-webhook-secret`
   - **Triggers**: Choose **Pull Request** -> **Merged**
3. Click **Save**

### What Happens on PR Merge

When a PR created by the resolution agent is merged:
1. Bitbucket fires webhook to `/pr-merge`
2. The handler extracts the resolution details (ticket ID, files changed, commit message)
3. KB Ingestion Lambda writes a resolution document to S3
4. Bedrock KB Data Source sync is triggered
5. Future tickets benefit from this resolution in KB retrieval

---

## Step 9: DevOps Agent Space Setup

The AWS DevOps Agent performs automated investigation (reading CloudWatch logs, X-Ray traces, etc.) and posts findings as Jira comments.

### Create an Agent Space

1. Open the AWS DevOps Agent console in your target region
2. Click **Create agent space**
3. Name it (e.g., `iot-fleet-investigation`)

### Create a Scoped IAM Role

Create a role that the agent assumes for investigation. Minimum permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:GetLogEvents",
        "logs:FilterLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "xray:GetTraceSummaries",
        "xray:BatchGetTraces"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable",
        "dynamodb:ListTables",
        "dynamodb:Scan",
        "dynamodb:Query"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeClusters",
        "ecs:ListClusters",
        "ecs:DescribeServices",
        "ecs:ListServices",
        "ecs:DescribeTasks",
        "ecs:ListTasks"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs"
      ],
      "Resource": "*"
    }
  ]
}
```

Trust policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "aidevops.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"aws:SourceAccount": "<YOUR_ACCOUNT_ID>"}}
  }]
}
```

### Assign the Role

In the Agent Space settings, assign the IAM role you created as the source monitor role.

### Enable Agent Actions

Agent Actions allow the DevOps Agent to write findings back to Jira (post comments, link issues). Without this, investigations complete but results aren't written back.

1. In the Agent Space, go to **Configuration** → **Agent Actions**
2. Enable Agent Actions
3. Assign the same investigation role (or a dedicated actions role)

### Add MCP Permissions to the Role

Before adding the MCP server, the investigation role needs permission to invoke the API Gateway endpoint. Get your API ID and add the policy:

```bash
# Get the MCP endpoint URL (the API ID is the subdomain before .execute-api)
aws cloudformation describe-stacks --stack-name JiraIntakeStack \
  --query "Stacks[0].Outputs[?OutputKey=='MCPEndpointUrl'].OutputValue" \
  --output text --region $REGION
```

Add this statement to the investigation role's inline policy (replace `<API_ID>` with the subdomain from the URL above):
```json
{
  "Effect": "Allow",
  "Action": "execute-api:Invoke",
  "Resource": "arn:aws:execute-api:<YOUR_REGION>:<YOUR_ACCOUNT_ID>:<API_ID>/*/POST/mcp"
}
```

### Add the Jira MCP Server

The DevOps Agent needs MCP access to read Jira tickets and post comments.

1. In the Agent Space, go to **MCP Servers** -> **Add**
2. Configure:

| Page | Field | Value |
|------|-------|-------|
| 1. Server Details | Name | `jira-mcp-server` |
| | Endpoint URL | `https://<API_ID>.execute-api.<YOUR_REGION>.amazonaws.com/prod/mcp` |
| | Dynamic Client Registration | Unchecked |
| | Private connection | Unchecked |
| | Encryption | AWS owned key |
| 2. Authorization Flow | Type | **AWS SigV4** |
| 3. Authorization Config | Role | Your scoped role name |
| | AWS Region | `<YOUR_REGION>` |
| | Service Name | `execute-api` |
| | Custom Headers | None |
| 4. Tool Selection | All tools (16) | Classify as **"Read only"** |

> **Important:** Classify ALL tools — including write tools — as "Read only". The "Mutative" classification requires human approval before execution, which blocks autonomous operation in webhook-triggered sessions (the agent cannot post RCA comments). Your MCP server already enforces safety via its own policy engine, redaction, and idempotency layers, so the platform approval gate is redundant.


### Add the Writeback Skill

1. In the Agent Space, go to **Skills** -> **Add**
2. Upload or paste the content from `skills/jira-bug-triage-writeback.md`
3. This skill instructs the agent to post structured RCA comments on Jira tickets

### Copy the Agent Space Webhook URL

In the Agent Space overview, copy the **Webhook URL**. It looks like:
```
https://event-ai.<YOUR_REGION>.api.aws/webhook/generic/<SPACE_ID>
```

You will use this as `<YOUR_DEVOPS_AGENT_WEBHOOK_URL>` in CDK context.

---

## Step 10: Wire the Pipeline

### Update Repository Mapping

Copy the examples and edit them to add your Jira project -> repo mapping:

```bash
cp orchestrator/repo-config.yaml.example orchestrator/repo-config.yaml
cp planner/agentcore/repo-config.yaml.example planner/agentcore/repo-config.yaml
```

Both files use the same format. Edit each to include your repo(s):

```yaml
mappings:
  - project: IOT
    repo: https://bitbucket.org/<YOUR_BB_WORKSPACE>/iot-fleet-management.git
    branch: main
    provider: bitbucket
    auth_secret_arn: arn:aws:secretsmanager:<YOUR_REGION>:<YOUR_ACCOUNT_ID>:secret:agentic-pipeline/repo-credentials
```

The Planner uses its copy to browse repo trees during planning; the Orchestrator uses its copy to resolve which repo to clone for resolution. Keep them in sync.

### Update CDK Context

Copy the example and fill in your values:

```bash
cp infrastructure/cdk.context.json.example infrastructure/cdk.context.json
```

Edit `infrastructure/cdk.context.json` to set:

```json
{
  "vpc_id": "<YOUR_VPC_ID>",
  "devops_agent_webhook_url": "<YOUR_DEVOPS_AGENT_WEBHOOK_URL>",
  "jira_base_url": "https://<YOUR_ORG>.atlassian.net",
  "jira_api_secret_arn": "<YOUR_JIRA_API_SECRET_ARN>"
}
```

Get the Jira API secret ARN:
```bash
aws cloudformation describe-stacks --stack-name JiraIntakeStack \
  --query "Stacks[0].Outputs[?OutputKey=='JiraApiSecretArn'].OutputValue" \
  --output text --region $REGION
```

### Redeploy

```bash
make deploy-pipeline VPC_ID=<YOUR_VPC_ID>
```

Or manually:
```bash
cd infrastructure
AWS_REGION=<YOUR_REGION> npx cdk deploy --all -c vpc_id=<YOUR_VPC_ID> --require-approval never
```

---

## Step 11: Deploy Sample App (Optional)

The IoT Fleet Management sample app provides a realistic multi-service application for E2E testing.

### Build the Frontend

```bash
make build-frontend
```

Or manually:
```bash
cd sample-app/iot-fleet-management/frontend
npm install && npm run build
```

### Deploy

```bash
make deploy-iot VPC_ID=<YOUR_VPC_ID>
```

Or manually:
```bash
cd sample-app/iot-fleet-management/infrastructure
AWS_REGION=<YOUR_REGION> npx cdk deploy IoTFleetStack -c vpc_id=<YOUR_VPC_ID> --require-approval never
```

### What It Creates

- 4 ECS Fargate services (telemetry-ingest, alert-engine, firmware-service, geofence-service)
- ALB with path-based routing
- DynamoDB tables (telemetry, firmware, geofences)
- ElastiCache Redis cluster
- CloudWatch alarms (latency, error rate, alert storms)
- S3 + CloudFront (React dashboard)
- SNS topic for alerts

### Dashboard URL

After deploy, the CloudFront URL appears in stack outputs as `DashboardUrl`.

---

## Step 12: End-to-End Test

### Create a Test Ticket

1. In your Jira project, create a new **Bug** issue:
   - Summary: `High latency on telemetry-ingest /api/telemetry endpoint`
   - Description: Include symptoms, error patterns, or log references
2. The Jira webhook fires immediately

### Watch the Execution

1. Open the Step Functions console in your target region
2. Find the `agentic-pipeline-orchestrator` state machine
3. Click the running execution to see the visual workflow

### Approval Gates

The pipeline pauses at three human-in-the-loop gates:

| Gate | How to Approve |
|------|----------------|
| **RCA Review** | DevOps Agent posts RCA comment automatically. Human transitions Jira ticket to `In Review` status. |
| **Plan Review** | Pipeline posts a resolution plan as a Jira comment. Human comments `/approve-plan` on the ticket. |
| **PR Review** | Pipeline creates a PR on Bitbucket. Human reviews and merges. |

### Verify PR Creation

After the `/approve-plan` gate:
1. The Resolution Agent clones the repo, writes a fix, and creates a PR
2. Check your Bitbucket repo for a new PR (branch name: `fix/<TICKET_KEY>-<timestamp>`)
3. The Step Functions execution completes with the PR URL

### Verify KB Ingestion

After merging the PR:
1. Bitbucket webhook fires to `/pr-merge`
2. KB Ingestion Lambda writes a resolution document
3. Check the S3 bucket (`agentic-pipeline-kb-data-*`) for a new document
4. Check Bedrock KB console for a completed sync

### Expected Timeline

| Phase | Duration |
|-------|----------|
| Classify + Dispatch | ~5 seconds |
| DevOps Agent investigation | 5-20 minutes |
| Plan generation (KB + repo browsing + strategy) | 30-120 seconds |
| Resolution (clone + fix + PR) | 60-120 seconds |
| **Total (excluding human gates)** | **~7-22 minutes** |

---

## AWS Services -- How They're Used

### Amazon Bedrock (Foundation Models)

| Model | Model ID | Purpose |
|-------|----------|---------|
| Claude Sonnet | `us.anthropic.claude-sonnet-4-6` | Resolution planning, code generation, enriched prompts |
| Claude Haiku | `us.anthropic.claude-haiku-3-5-v2` | Ticket classification (fast, cheap fallback when rules are ambiguous) |

The Orchestrator's classify Lambda uses Haiku via `InvokeModel` for ambiguous tickets. Both the Resolution Planner and Resolution Agent run on AgentCore Runtime with Strands SDK using Sonnet — the Planner for multi-step reasoning (KB retrieval, repo browsing, strategy planning) and the Resolution Agent for code analysis and fix generation.

### Bedrock AgentCore Runtime

Hosts two Strands-based agents as managed runtimes:

| Runtime | Purpose | Invocation |
|---------|---------|------------|
| `agentic_pipeline_resolution_planner` | Plans fix strategy: queries KB, browses repo tree, builds enriched prompt | Lambda → `InvokeAgentRuntime` (direct, up to 10 min) |
| `agentic_pipeline_resolution_agent` | Clones repo, writes code fix, creates PR | ECS Worker → `InvokeAgentRuntime` (streaming, up to 1 hour) |

Key APIs:

| API | Purpose |
|-----|---------|
| `InvokeAgentRuntimeCommand` | Runs shell commands inside the Resolution Agent runtime (e.g., `setup_workspace.sh` to clone the repo) |
| `InvokeAgentRuntime` | Sends structured payload to either agent for reasoning |

Logs are written to:
- `/agentic-pipeline/agentcore-planner` (Planner agent: KB queries, repo browsing, strategy reasoning)
- `/aws/bedrock-agentcore/runtimes/agentic_pipeline_resolution_agent-*-DEFAULT` (Resolution Agent runtime command logs, setup outputs)
- `/agentic-pipeline/agentcore-resolution` (Resolution Agent application logs: tool calls, model invocations, token usage)

The Planner is invoked directly by a Lambda (fast, <10 min). The Resolution Agent uses an ECS Worker that holds the streaming connection open for the full execution (no 15-min Lambda timeout constraint).

### Bedrock Knowledge Base

Uses OpenSearch Serverless (AOSS) as the vector store for resolution documents.

| Operation | When |
|-----------|------|
| `Retrieve` API | Called by the Planner Agent's `query_knowledge_base` tool to find similar past resolutions |
| `StartIngestionJob` | Triggered after a PR is merged (KB Ingestion Lambda syncs new docs from S3) |

The KB stores structured resolution documents: ticket context, root cause, files changed, fix description. Each successful resolution enriches future planning.

### Bedrock Guardrails

Applied at both input and output of LLM calls:

- **Content filters**: Block harmful/inappropriate content
- **PII detection**: Redacts personally identifiable information
- **Secret detection**: Catches leaked API keys, passwords, tokens
- **Denied topics**: Prevents off-topic or dangerous instructions

Guardrail ID and version are passed to the Planner Agent, AgentCore runtime, and classifier Lambda.

### AgentCore Memory

The resolution agent uses AgentCore Memory for cross-session learning:

| Strategy | Purpose |
|----------|---------|
| `semantic` | Stores and retrieves resolution patterns by semantic similarity |
| `user_preference` | Remembers repo-specific conventions (test frameworks, style guides) |
| `summarization` | Compresses long investigation context into reusable summaries |

Memory is transparent via `session_manager` -- the agent automatically retrieves relevant memories at the start of each session and stores new learnings at the end.

### AWS DevOps Agent

Performs automated investigation of production issues:

1. Receives HMAC-signed webhook from the Dispatch Lambda
2. Investigates using scoped IAM role (CloudWatch Logs, X-Ray, DynamoDB, ECS)
3. Reads Jira ticket details via the MCP server (`/mcp` endpoint, SigV4 auth)
4. Posts structured RCA findings as a Jira comment using the writeback skill
5. The Jira comment webhook fires, and the pipeline's callback detection resumes the state machine

### Verifying Services Are Working

All components write to CloudWatch Logs. Check these log groups to trace execution:

| Log Group | Component |
|-----------|-----------|
| `/aws/lambda/agentic-pipeline-classify` | Ticket classifier (rules + Haiku) |
| `/aws/lambda/agentic-pipeline-dispatch` | DevOps Agent webhook dispatch |
| `/aws/lambda/agentic-pipeline-resolution-planner` | AgentCore Planner invocation (Lambda wrapper) |
| `/agentic-pipeline/agentcore-planner` | Planner agent reasoning (KB query, repo browse, strategy) |
| `/aws/lambda/agentic-pipeline-resolution-dispatcher` | ECS task launcher (stores token, runs task) |
| `/agentic-pipeline/resolution-worker` | ECS Worker lifecycle (token retrieval, agent start/complete, callback) |
| `/aws/bedrock-agentcore/runtimes/agentic_pipeline_resolution_agent-*-DEFAULT` | AgentCore runtime commands and setup |
| `/agentic-pipeline/agentcore-resolution` | Strands agent application logs (tool calls, model invocations) |
| `/aws/lambda/agentic-pipeline-post-resolution` | Post-resolution notification |
| `/aws/lambda/agentic-pipeline-pr-merge-handler` | Bitbucket PR merge webhook handler |
| `/aws/lambda/agentic-pipeline-kb-ingestion` | Knowledge base document ingestion and sync |

Quick health check:
```bash
# Check for recent errors across all pipeline Lambdas
aws logs filter-log-events \
  --log-group-name-prefix "/aws/lambda/agentic-pipeline" \
  --filter-pattern "ERROR" \
  --start-time $(date -d '1 hour ago' +%s000 2>/dev/null || date -v-1H +%s000) \
  --region $REGION \
  --query "events[].message" --output text

# Check ECS worker logs for the last execution
aws logs tail /agentic-pipeline/resolution-worker --since 1h --region $REGION

# Check AgentCore runtime logs
aws logs tail /agentic-pipeline/agentcore-resolution --since 1h --region $REGION
```

---

## Troubleshooting

### Webhook Not Firing

| Symptom | Fix |
|---------|-----|
| No Step Functions execution after creating ticket | Verify Jira webhook URL is correct and enabled |
| Webhook shows 403 in Jira logs | Check API Gateway resource policy; ensure no IP restrictions |
| Webhook shows 500 | Check webhook receiver Lambda logs in CloudWatch |
| Duplicate executions | The idempotency store deduplicates; check DynamoDB TTL settings |

### DevOps Agent Issues

| Symptom | Fix |
|---------|-----|
| "fetch failed" on MCP server | Add `execute-api:Invoke` to the agent role |
| "Invalid STS role configuration for sigv4McpServerSession" | Remove `ArnLike` condition from the role trust policy (use only `StringEquals` on `aws:SourceAccount`) |
| "Tools no longer available" | MCP endpoint URL changed after redeploy; update in DevOps Agent console |
| Agent does not post RCA comment | Check the writeback skill is attached and ALL Jira MCP tools are classified as "Read only" (not "Mutative" — mutative requires approval which blocks autonomous sessions) |
| "approval-ref header is required" | Write tools are classified as "Mutative" in the MCP server tool selection. Reclassify ALL tools as "Read only" |
| Agent space webhook returns 403 | Verify the dispatch Lambda is using the correct webhook URL and HMAC secret |

### Container / ECS Issues

| Symptom | Fix |
|---------|-----|
| `exec format error` in ECS task | Architecture mismatch. Worker image must be AMD64 (`--platform linux/amd64` in Docker build). Rebuild and redeploy. |
| Task starts but exits immediately | Check `/agentic-pipeline/resolution-worker` log group for errors |
| "Unable to pull secrets" | ECS task execution role needs `secretsmanager:GetSecretValue` permission |
| Git clone fails in container | Verify repo credentials secret is populated with correct `email:token` format |
| Heartbeat timeout (task killed after 10 min) | Agent execution exceeded heartbeat interval. Check AgentCore logs for stuck operations. |

### Step Functions Issues

| Symptom | Fix |
|---------|-----|
| Execution stuck at `WaitForRCA` | DevOps Agent has not posted a comment yet, or webhook receiver did not detect the RCA pattern |
| `TaskTimedOut` on RunResolutionTask | ECS task did not call `SendTaskSuccess` within the timeout. Check worker logs. |
| "Payload too large" error | Step Functions has a 256KB payload limit. Trim large fields in the state machine input. |
| Execution fails at Classify | Check classifier Lambda logs; may be a malformed webhook payload |

### CDK Deploy Issues

| Symptom | Fix |
|---------|-----|
| "Cannot find VPC" | Ensure `vpc_id` in `cdk.context.json` is valid for your account/region |
| Docker build fails | Ensure Docker Desktop is running. For M1/M2 Macs, `--platform linux/amd64` is set in Dockerfiles. |
| "Resource already exists" | Stack was partially deployed before. Run `cdk destroy <StackName>` then redeploy. |
| Circular dependency error | Deploy stacks individually in the order listed in Step 4. |

---

## Stack Outputs Reference

After deploying, note these outputs from each stack (shown via `cdk deploy` or `aws cloudformation describe-stacks`):

| Stack | Output Key | Used by |
|-------|-----------|---------|
| GuardrailStack | `GuardrailId`, `GuardrailVersion` | PlannerAgentCoreStack, ResolutionAgentCoreStack, OrchestratorStack |
| KnowledgeBaseStack | `DataBucketName`, `CollectionEndpoint` | setup_knowledge_base.py |
| setup_knowledge_base.py | KB ID (printed to stdout) | PlannerAgentCoreStack, OrchestratorStack |
| PlannerAgentCoreStack | `AgentRuntimeArn`, `ImageUri` | OrchestratorStack |
| ResolutionAgentCoreStack | `DispatcherFunctionName`, `AgentRuntimeArn`, `MemoryId` | OrchestratorStack |
| OrchestratorStack | `StateMachineArn`, `TaskTokenTableName` | JiraIntakeStack |
| JiraIntakeStack | `WebhookEndpointUrl`, `MCPEndpointUrl`, `PRMergeEndpointUrl` | Jira webhook, DevOps Agent MCP, Bitbucket webhook |

---

## Makefile Reference

```bash
# Deploy all stacks (pipeline + sample app + frontend)
make deploy-all VPC_ID=<YOUR_VPC_ID>

# Deploy only the 7 pipeline stacks
make deploy-pipeline VPC_ID=<YOUR_VPC_ID>

# Deploy the IoT Fleet Management sample app
make deploy-iot VPC_ID=<YOUR_VPC_ID>

# Build the React frontend dashboard
make build-frontend

# Install Python dependencies for all components
make install-deps

# Run unit tests for all sample app services
make test-unit

# Run an E2E test (prints instructions)
make test-e2e

# Synthesize CDK templates without deploying (validation)
make synth VPC_ID=<YOUR_VPC_ID>

# Remove all build artifacts
make clean

# Show all available commands and project info
make info
```

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `REGION` | `us-east-1` | Target AWS region |
| `VPC_ID` | (required) | VPC for ECS tasks and sample app |

Override the region:
```bash
make deploy-pipeline VPC_ID=vpc-xxx REGION=us-west-2
```

---

## Architecture Overview

```
                         Jira Cloud
                             |
                     (webhook: issue_created)
                             v
                    +-----------------+
                    |  API Gateway    |
                    |  /webhook       |
                    |  /mcp           |
                    |  /pr-merge      |
                    +-----------------+
                             |
                             v
                    +-----------------+
                    | Webhook Receiver|  (normalize, dedupe, callback detection)
                    +-----------------+
                             |
              +--------------+--------------+
              |                             |
        (new ticket)                  (callback)
              |                             |
              v                             v
     +------------------+          SendTaskSuccess
     |  Step Functions  |          (resumes execution)
     |  Orchestrator    |
     +------------------+
              |
     Classify -> Dispatch -> WaitForRCA -> PlanResolution -> PostPlan -> RunResolution -> PostResolution
                    |                            |                              |
                    v                            v                              v
            DevOps Agent                  AgentCore                       AgentCore + ECS
          (investigate)            (KB + browse repo + plan)           (clone, fix, PR)
                    |                            |                              |
                    v                            v                              v
            Jira RCA comment            Jira plan comment              Bitbucket PR
```
