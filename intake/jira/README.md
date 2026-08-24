# Jira MCP — Integration

Sample Jira Cloud integration for AI agents over MCP, demonstrating production-ready patterns. This is sample code for educational purposes and requires additional security testing and customization before production deployment.

Bidirectional flow:
- **Agent → Jira** via 16 MCP tools (8 read, 8 write) on a Lambda + API Gateway endpoint, IAM-authed (SigV4).
- **Jira → Agent** via a webhook receiver Lambda (HMAC-verified, JQL-filtered, deduped, signed-forward to the agent).

See `CONCEPTS.md` for explanations of the production layers; `JIRA_USE_CASES.md` for what this is and isn't useful for; `diagrams/jira-mcp-architecture.drawio` for the architecture.

---

## Layout

```
integrations/jira/
├── server.py            # MCP server Lambda — 15 tools, JSON-RPC
├── jira_client.py       # Jira REST v3 wrapper with retry/backoff
├── policy.yaml          # Per-project field/transition allowlists
├── policy_engine.py     # Loads + enforces policy.yaml
├── idempotency.py       # DynamoDB-backed idempotency cache
├── redaction.py         # Regex secret-scrubber for ADF + filenames
├── webhook_receiver.py  # Jira webhook → normalize → agent webhook
├── requirements.txt
├── README.md            # this file
├── SETUP_GUIDE.md       # step-by-step Jira + AWS deploy
├── CONCEPTS.md          # production-layer explanations
├── JIRA_USE_CASES.md    # use cases (good and not-useful)
└── diagrams/
    └── jira-mcp-architecture.drawio   # 4-page architecture diagram

infrastructure/stacks/jira_stack.py    # CDK stack
```

## Tool surface

### Read (8)

| Tool | Purpose |
|---|---|
| `search_issues_jql` | JQL search with pagination + field selection. Workhorse for prior-incident lookup, SLA sweeps. |
| `get_issue` | Single issue with optional field/expansion selection. |
| `get_issue_comments` | Paginated comments. |
| `get_issue_changelog` | Status / field history. |
| `get_transitions` | Workflow transitions available for an issue. |
| `get_project_metadata` | Issue types + create-meta with field schemas. |
| `get_attachments` | Attachment metadata + download URLs. |
| `get_user` | Resolve users by email or name. |

### Write (8)

| Tool | Purpose | Production guardrails |
|---|---|---|
| `create_issue` | RCA follow-ups, bug tickets, scheduled-monitor findings. | Idempotency, field allowlist, redaction. |
| `update_issue` | Patch fields (assignee, labels, priority, custom fields). | Idempotency, field allowlist, redaction. |
| `transition_issue` | Move ticket through workflow. | Transition allowlist, never-auto-close-P1, comment-required-on-terminal. |
| `add_comment` | Post diagnosis, PR links, deploy IDs. | Idempotency, redaction. |
| `add_attachment` | Upload diagnostic bundles. | Idempotency, filename redaction. |
| `link_issues` | Caused by / duplicate of / blocks / relates. | Idempotency. |
| `bulk_transition` | Mass workflow transitions (SLA sweeps). | Per-project cap (`max_bulk_operations`). |
| `bulk_update` | Mass field updates (labeling, assignment). | Per-project cap + field allowlist. |

Every write call passes through: **API Gateway IAM (SigV4) → idempotency lookup (DDB) → policy engine → secret redactor → circuit breaker → Jira REST v3 → idempotency record (DDB) → audit log**.

Read calls skip idempotency + redaction (GETs are naturally idempotent; no body to redact). All other layers still apply.

## Dry-run mode

Deploy with `--parameters DryRun=true` (or set env var `JIRA_MCP_DRY_RUN=true` locally) to make every write tool **log what it would send to Jira and return a dry-run response** without actually calling Jira. Read tools are unaffected.

Use this for:
- First production rollout — observe agent behavior in CloudWatch audit logs before letting it write.
- Tuning `policy.yaml` from real traffic without risk.
- Debugging odd agent behavior in isolation.

Dry-run responses always carry `dry_run: true` and are **not** cached in the idempotency table — flipping the parameter back to `false` and replaying the same idempotency key will fire a real write.

```bash
# Toggle without redeploying code:
cdk deploy JiraIntegrationStack --parameters DryRun=false
```

## Quickstart

1. **Create Jira service account + API token.** See `SETUP_GUIDE.md` §1.
2. **Deploy the stack:**
   ```bash
   cd infrastructure
   cdk deploy JiraIntegrationStack \
     --parameters JiraBaseUrl=https://your-org.atlassian.net \
     --parameters AgentWebhookUrl=https://<your-agent-webhook> \
     --parameters WebhookFilter="project=OPS AND priority in (P1,P2)"
   ```
3. **Populate the secrets** (see CDK outputs for ARNs):
   ```bash
   aws secretsmanager put-secret-value --secret-id <JiraSecretArn> \
     --secret-string '{"email":"agent@example.com","api_token":"..."}'
   aws secretsmanager put-secret-value --secret-id <JiraWebhookSecretArn> \
     --secret-string '{"secret":"<random-32-bytes>"}'
   aws secretsmanager put-secret-value --secret-id <AgentWebhookSecretArn> \
     --secret-string '{"secret":"<random-32-bytes>"}'
   ```
4. **Configure Jira webhook** to POST to the `WebhookEndpointUrl` output, using the `JiraWebhookSecret` value as the secret. See `SETUP_GUIDE.md` §3.
5. **Grant your agent role** `execute-api:Invoke` on the MCP endpoint (see CDK output `InvokePolicyHint`).
6. **Customize `policy.yaml`** for your projects.

## Calling the MCP endpoint

```bash
# Example: list tools
aws apigateway test-invoke-method --rest-api-id <id> --resource-id <id> \
  --http-method POST --path-with-query-string /mcp \
  --body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

In production, the agent runtime (Bedrock / AgentCore / etc.) signs requests with SigV4 using its assigned IAM role. Pass an idempotency key on writes:

```
X-Idempotency-Key: incident-2026-06-17-a3f9-create_issue
```

## Local development

```bash
pip install -r requirements.txt
export JIRA_BASE_URL=https://your-org.atlassian.net
export JIRA_SECRET_ARN=<arn-or-leave-blank-for-mock>
export JIRA_IDEMPOTENCY_TABLE=<table>
python server.py
# listens on http://localhost:8081/mcp
```

## What's intentionally NOT here

- No Atlassian SDK dependency — keeps cold start <500ms.
- No MCP Python SDK — matches the existing `integrations/custom-mcp/` pattern.
- No FastAPI / Flask — stdlib HTTP for the local server, raw Lambda handler for prod.
- No webhook implementation in the MCP server — webhook ingress is a separate Lambda by design (different auth, different lifecycle).
- No tests in v1 — see decision in CONCEPTS.md / repo conventions. A focused redaction test suite is a fast follow.
