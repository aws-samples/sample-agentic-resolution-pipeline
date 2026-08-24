# Agentic Resolution Pipeline — Visual Flow

## End-to-End Flow

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                              TRIGGER PHASE                                               │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌──────────┐     ┌─────────────┐     ┌──────────────┐     ┌────────────────────┐    │
│   │   Jira   │────▶│ API Gateway │────▶│   Webhook    │────▶│  Step Functions    │    │
│   │  (Bug)   │     │  /webhook   │     │  Receiver    │     │  (Orchestrator)    │    │
│   └──────────┘     └─────────────┘     └──────────────┘     └─────────┬──────────┘    │
│                                          - HMAC verify                  │               │
│                                          - Deduplicate                  │               │
│                                          - Normalize                    ▼               │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                              CLASSIFICATION                                             │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌──────────────────┐                                                                  │
│   │   Classify Fn    │──── BUG_TICKET / PROD_INCIDENT ────▶ Continue                   │
│   │ (Rules + Haiku)  │──── NOISE ─────────────────────────▶ Log & Close                │
│   │                  │──── DATA_QUALITY ──────────────────▶ Approval Gate → Continue    │
│   │                  │──── FEATURE_REQUEST ───────────────▶ Plan directly               │
│   └──────────────────┘                                                                  │
│                                                                                         │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                              INVESTIGATION PHASE                                        │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌──────────────┐     ┌──────────────────────────┐     ┌──────────────────────┐      │
│   │  Dispatch Fn │────▶│   AWS DevOps Agent       │────▶│   Jira Comment       │      │
│   │(HMAC sign +  │     │   (Agent Space)          │     │   (RCA findings)     │      │
│   │ post to Jira)│     │                          │     └──────────┬───────────┘      │
│   └──────────────┘     │ • CloudWatch Logs/Metrics│                │                   │
│                         │ • X-Ray Traces          │                │                   │
│                         │ • ECS Service state     │                ▼                   │
│   ┌─────────────────┐  │ • Jira MCP (read/write) │     ┌──────────────────────┐      │
│   │ WaitForRCA      │◀─│ • Progressive updates   │     │  Webhook detects     │      │
│   │ (task token)    │  └──────────────────────────┘     │  RCA comment         │──┐   │
│   └─────────────────┘                                   └──────────────────────┘  │   │
│          ▲                                                                         │   │
│          └─────────────────── SendTaskSuccess ────────────────────────────────────┘   │
│                                                                                         │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                              APPROVAL GATE                                              │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌────────────────────────┐     ┌─────────────────┐     ┌────────────────────────┐   │
│   │ NotifyResolutionReady  │────▶│  Human reviews   │────▶│  Jira transition to    │   │
│   │ (stores token, SNS)    │     │  RCA in Jira     │     │  "In Review"           │   │
│   └────────────────────────┘     └─────────────────┘     └──────────┬─────────────┘   │
│                                                                       │                 │
│          ┌──────────────── Webhook detects transition ◀──────────────┘                 │
│          ▼                                                                              │
│   SendTaskSuccess                                                                       │
│                                                                                         │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                              PLANNING PHASE                                             │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌────────────────────┐     ┌──────────────────────────────────────┐                  │
│   │  PlanResolution    │────▶│   Bedrock Agent (Planner)            │                  │
│   │  (Lambda)          │     │                                      │                  │
│   └────────────────────┘     │  1. Query Knowledge Base (past fixes)│                  │
│                               │  2. Resolve repo (repo-config.yaml) │                  │
│                               │  3. Build resolution prompt         │                  │
│                               └──────────────────┬───────────────────┘                  │
│                                                   │                                     │
│                                                   ▼                                     │
│   ┌────────────────────┐     ┌──────────────────────────────────────┐                  │
│   │ PostPlanForReview  │────▶│  Posts plan as Jira comment          │                  │
│   │ (stores token)     │     │  Waits for /approve-plan             │                  │
│   └────────────────────┘     └──────────────────┬───────────────────┘                  │
│          ▲                                       │                                      │
│          └── SendTaskSuccess ◀── Webhook detects /approve-plan comment                 │
│                                                                                         │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                              RESOLUTION PHASE                                           │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌───────────────┐     ┌────────────────┐     ┌──────────────────────────────────┐   │
│   │ RunResolution │────▶│  Dispatcher    │────▶│   ECS Worker (Fargate)           │   │
│   │ Task          │     │  Lambda (30s)  │     │   (no timeout)                   │   │
│   │(waitForToken) │     │                │     │                                  │   │
│   └───────────────┘     │ • Store token  │     │ • Read payload from S3           │   │
│                          │ • Write to S3  │     │ • Start heartbeat (300s)         │   │
│                          │ • Start ECS    │     │ • Setup workspace (clone repo)   │   │
│                          └────────────────┘     │ • invoke_agent_runtime           │   │
│                                                  │   ┌─────────────────────────┐    │   │
│                                                  │   │  AgentCore Runtime      │    │   │
│                                                  │   │  (Strands Agent)        │    │   │
│                                                  │   │                         │    │   │
│                                                  │   │  • Analyze code         │    │   │
│                                                  │   │  • Write fix            │    │   │
│                                                  │   │  • Run tests            │    │   │
│                                                  │   │  • Commit & push        │    │   │
│                                                  │   │  • Create PR            │    │   │
│                                                  │   │  • Memory (learn)       │    │   │
│                                                  │   └─────────────────────────┘    │   │
│                                                  │                                  │   │
│                                                  │ • Write output to S3             │   │
│                                                  │ • SendTaskSuccess (PR URL)       │   │
│                                                  └──────────────────────────────────┘   │
│                                                                                         │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                              POST-RESOLUTION                                            │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌──────────────────┐     ┌────────────────────────────────────────┐                  │
│   │ PostResolution   │────▶│ • Read output from S3                  │                  │
│   │ (Lambda)         │     │ • Post PR URL as Jira comment          │                  │
│   └──────────────────┘     │ • Send SNS email notification          │                  │
│                             └────────────────────────────────────────┘                  │
│                                                                                         │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                              FEEDBACK LOOP (on PR merge)                                │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   ┌──────────────┐     ┌─────────────┐     ┌────────────────┐     ┌───────────────┐   │
│   │  Bitbucket   │────▶│ API Gateway │────▶│  PR Merge      │────▶│ KB Ingestion  │   │
│   │  PR Merged   │     │  /pr-merge  │     │  Handler       │     │ Lambda        │   │
│   └──────────────┘     └─────────────┘     │                │     │               │   │
│                                             │ • Repo in      │     │ • Write doc   │   │
│                                             │   allowlist?   │     │   to S3       │   │
│                                             │ • fix/ branch? │     │ • Trigger KB  │   │
│                                             │ • Get output   │     │   sync        │   │
│                                             │   from S3      │     │               │   │
│                                             └────────────────┘     └───────┬───────┘   │
│                                                                             │           │
│                                                                             ▼           │
│                                                                    ┌───────────────┐   │
│                                                                    │ Bedrock KB    │   │
│                                                                    │ (Vector DB)   │   │
│                                                                    │               │   │
│                                                                    │ Future planner│   │
│                                                                    │ queries find  │   │
│                                                                    │ this fix      │   │
│                                                                    └───────────────┘   │
│                                                                                         │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## Component Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            AWS Account                                       │
│                                                                             │
│  ┌─ JiraIntakeStack ────────────────────────────────────────────────────┐   │
│  │  API Gateway (/webhook, /mcp, /pr-merge)                             │   │
│  │  Jira Webhook Lambda • Jira MCP Lambda • PR Merge Handler Lambda     │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─ OrchestratorStack ──────────────────────────────────────────────────┐   │
│  │  Step Functions State Machine                                         │   │
│  │  ClassifyFn • DispatchFn • NoiseFn • StoreTokenFn                    │   │
│  │  KBRetrievalFn • ResolveRepoFn • ResolutionPlannerFn                 │   │
│  │  PostPlanFn • PostResolutionFn                                        │   │
│  │  DynamoDB (task tokens) • SNS (notifications)                         │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─ ResolutionPlannerStack ─────────────────────────────────────────────┐   │
│  │  Bedrock Agent (Claude Sonnet) + 3 Action Groups                      │   │
│  │  Knowledge Base (AOSS + S3)                                           │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─ ResolutionAgentCoreStack ───────────────────────────────────────────┐   │
│  │  AgentCore Runtime (Strands Agent + shell/file tools)                 │   │
│  │  AgentCore Memory (semantic + preferences + summaries)                │   │
│  │  ECS Worker Task Definition + Dispatcher Lambda                       │   │
│  │  S3 (resolution output + dispatch payloads)                           │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─ ResolutionStack (shared infra) ─────────────────────────────────────┐   │
│  │  ECS Cluster • VPC • Security Groups • Secrets Manager               │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─ IoTFleetStack (sample app) ─────────────────────────────────────────┐   │
│  │  ECS (4 services) • ALB • DynamoDB • Redis • CloudWatch Alarms       │   │
│  │  S3+CloudFront (dashboard) • SNS (alerts)                             │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─ External ───────────────────────────────────────────────────────────┐   │
│  │  Jira Cloud • Bitbucket • AWS DevOps Agent                            │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Timing (typical run)

| Phase | Duration | What happens |
|-------|----------|--------------|
| Trigger → Classify | ~3s | Webhook → rules + LLM classification |
| Dispatch → RCA | 5-20 min | DevOps Agent investigates via CloudWatch/X-Ray |
| Approval Gate | Human | Review RCA, transition to "In Review" |
| Plan | ~60s | Bedrock Agent queries KB, resolves repo, builds strategy |
| Plan Approval | Human | Review plan, comment `/approve-plan` |
| Resolution | 1-15 min | Clone → analyze → fix → test → commit → push → PR |
| Post-Resolution | ~3s | Jira comment + SNS notification |
| KB Ingestion | Async | On PR merge: write to KB, future plans benefit |
