# Architecture Decision Records (ADR)

Ongoing record of architectural decisions made during pipeline development — what we chose, why, and what we considered.

---

## ADR-001: Async Callback for Resolution Agent Invocation

**Status:** Superseded by ADR-008 (2026-07-23)  
**Context:** The Resolution Agent (Strands on AgentCore Runtime) can take 1–15+ minutes to fix code, run tests, and create a PR. The invoker Lambda has a hard 15-minute timeout. We hit `LambdaFunctionTimedOut` on complex tasks.

**Decision:** Use the **Step Functions Task Token callback pattern** (Option 2).

> **Note:** The callback pattern is still used, but the invoker changed from Lambda to ECS Worker. See ADR-008 for the final architecture.

**How it works (original, partially superseded):**
1. ~~Invoker Lambda starts the AgentCore session (setup workspace + fire `invoke_agent_runtime` async), stores the Step Functions task token, and returns immediately.~~
2. Step Functions enters a `waitForTaskToken` state (no Lambda timeout — can wait hours). **(Still true)**
3. When the agent finishes, the ECS Worker calls `SendTaskSuccess` with the PR URL. **(Still true, invoker changed to ECS)**

**Alternatives considered:**
| Option | Pros | Cons |
|--------|------|------|
| 1. Fire-and-forget + poll loop | Simple | Wasteful polling; complex state tracking; Step Functions charges per transition |
| **2. Callback pattern (chosen)** | No timeout; native Step Functions pattern; already used for RCA and approvals | Slightly more complex wiring (need callback mechanism) |
| 3. Shell-based execution | Avoids `/invocations` endpoint | Loses Strands agent loop; harder to get structured output |

**Why Option 2:** We already use `waitForTaskToken` for RCA callbacks and approval gates — same pattern, proven reliable. AgentCore Runtime can run up to 8 hours. No polling cost, no Lambda timeout constraint.

---

## ADR-002: AgentCore Observability with ADOT

**Status:** Implemented (2026-07-23)  
**Context:** Agent execution was a black box — 10+ minute runs with no visibility into what the agent was doing (which tools it called, which model invocations, where time was spent).

**Decision:** Use the L2 CDK `Runtime` construct with:
- `tracing_enabled=True` (X-Ray traces)
- `logging_configs` with `APPLICATION_LOGS` + `USAGE_LOGS` → CloudWatch
- `aws-opentelemetry-distro` (ADOT) in the container for full span-level detail
- CloudWatch Transaction Search enabled (one-time account setup — already done)

**What this gives us:**
- Structured application logs with trace IDs, span IDs, session IDs
- Usage logs with token consumption per session
- Full span tree in CloudWatch GenAI Observability console (each model call, tool invocation, timing)

**Alternatives considered:**
| Option | Pros | Cons |
|--------|------|------|
| Manual Strands `StrandsTelemetry().setup_otlp_exporter()` | Quick to add | Wrong endpoint (404 errors), no platform-level instrumentation |
| **ADOT + L2 construct (chosen)** | Full platform instrumentation, auto-configured, span-level detail | Requires CDK upgrade (done), slightly larger container image |
| Third-party (Langfuse, LangSmith) | Rich UI | External dependency, data leaves AWS, extra cost |

---

## ADR-003: Planner → Implementer Responsibility Split

**Status:** Decided (2026-07-23)  
**Context:** The Resolution Agent needs to know which files to modify. The Planner (Bedrock Agent) reasons about the fix strategy and identifies files. The Implementer (Strands Agent) executes.

**Decision:** **Planner identifies, Implementer adapts.**
- Planner: identifies likely files, strategy, relevant KB context
- Implementer: starts with planner-identified files, but may touch additional files if the fix requires it (imports, interfaces, configs) — without refactoring unrelated code

**Alternatives considered:**
| Option | Pros | Cons |
|--------|------|------|
| Hard constraint: implementer only touches planner-identified files | Predictable, safe | Breaks on multi-file bugs where planner misses a file |
| **Planner directs, implementer adapts (chosen)** | Handles real-world complexity; planner sets direction, implementer has agency | Slightly less predictable |
| No planner — implementer does everything | Simpler pipeline | Slower (agent explores from scratch each time), no KB integration |

---

## ADR-004: AgentCore Gateway for Pipeline Access

**Status:** Planned  
**Context:** Currently the pipeline is triggered directly via Jira webhook → Step Functions. For production/multi-tenant use, we need rate limiting, auth, and routing.

**Decision:** Integrate AgentCore Gateway as the API front-door.

**Value:**
- Rate limiting per tenant/team
- API key management
- Usage tracking and billing attribution
- Routing to different pipeline variants (fast-fix vs. full-investigation)

**Implementation:** TBD — will place Gateway in front of the Jira webhook endpoint or as an alternative trigger path.

---

## ADR-005: AgentCore Memory for Cross-Session Learning

**Status:** Planned  
**Context:** Each resolution runs independently — the agent doesn't learn from previous fixes. The KB stores curated knowledge, but the agent's own learnings (per-repo patterns, gotchas encountered) are lost.

**Decision:** Integrate AgentCore Memory (STM + LTM) into the Resolution Agent.

**LTM extraction strategies:**
- `user_preference` → per-repo coding patterns (test framework, PR conventions)
- `semantic_fact` → "this repo uses FastAPI", "discount logic is in order_calculator.py"
- `session_summary` → condensed history of past resolutions

**Value:** Agent gets better over time. First fix for a repo is cold; subsequent similar bugs resolve faster because the agent recalls patterns.

---

## ADR-006: Code Interpreter for Test Execution (Not Adopted)

**Status:** Rejected (2026-07-23)  
**Context:** AgentCore Code Interpreter provides sandboxed code execution. Considered for running tests in isolation (separate from the container that has git credentials).

**Decision:** Do not use Code Interpreter for this pipeline.

**Reasoning:**
- Tests need to run in the context of the cloned repo (imports, fixtures, conftest). Code Interpreter is a separate sandbox without access to the workspace.
- The security concern (test code accessing git credentials) is mitigable by using scoped tokens with read-only access for the API, write access only for the git push.
- Adding Code Interpreter would require serializing the repo into the sandbox — adds latency and complexity without meaningful benefit for this use case.
- `BYPASS_TOOL_CONSENT=true` + `STRANDS_NON_INTERACTIVE=true` already allow the shell tool to run tests directly in the workspace.

**Where Code Interpreter fits:** Better suited for data analysis agents, math/science agents, or scenarios where the code being executed is untrusted user input (not the case here — we control the agent's actions).

---

## ADR-007: IoT Fleet Management as Demo Application

**Status:** Decided (2026-07-22)  
**Context:** Need a realistic multi-service application with planted bugs for E2E pipeline testing. User's team is auto/manufacturing.

**Decision:** IoT Fleet Management app (not e-commerce, not ride-sharing).

**Services:**
1. `telemetry-ingest` (Python/FastAPI) — device payload ingestion
2. `alert-engine` (Node.js/Express) — threshold evaluation, anomaly detection
3. `firmware-service` (Python/FastAPI) — OTA update orchestration
4. `geofence-service` (Node.js/Express) — boundary definitions, location checks

**Planted bugs:**
1. Telemetry timestamp drift (UTC vs local time)
2. Alert window off-by-one (N-1 samples instead of N)
3. Firmware version string comparison (not semver)
4. Geofence boundary float precision (flicker at edges)
5. Reconnect stale state (ghost alerts)

**Why not e-commerce:** Doesn't resonate with auto/manufacturing customers. IoT fleet management maps to AWS services they already use (IoT Core, Kinesis, TimeStream, Lambda) and matches their incident patterns.

---

## ADR-008: ECS Worker for AgentCore Invocation (Replaces nohup + Dispatcher Lambda)

**Status:** Decided (2026-07-23)  
**Context:** The Resolution Agent (Strands on AgentCore) takes 1–15+ minutes. We tried several approaches to invoke it without hitting timeout limits:

1. **Dispatcher Lambda + nohup (failed):** Lambda stores task token, calls `invoke_agent_runtime_command` to run `nohup python run_fix.py &`, returns immediately. **Problem:** `nohup` detaches the process, but when the command "completes" (instantly, due to `&`), AgentCore's runtime session ends and kills the child process. The background process never actually runs.

2. **Dispatcher Lambda foreground (failed):** Remove nohup, run `run_fix.py` in foreground via `invoke_agent_runtime_command` with a long timeout. **Problem:** Lambda max timeout is 15 minutes. Complex fixes exceed this. Also, `invoke_agent_runtime_command` (shell commands) is not the right API for LLM reasoning loops.

3. **invoke_agent_runtime from Lambda (failed):** Call the LLM API directly from the dispatcher. **Problem:** Same 15-minute Lambda limit. The streaming connection must be held open for the entire agent execution.

**Decision:** **ECS Fargate Worker** — a lightweight container (just boto3) that:
1. Receives the payload via `TASK_PAYLOAD` environment variable (set by container overrides in `ecs:RunTask`)
2. Retrieves the Step Functions task token from DynamoDB
3. Starts a heartbeat thread (every 300s)
4. Calls `invoke_agent_runtime_command` to setup the workspace (clone repo, configure git)
5. Calls `invoke_agent_runtime` with a **structured payload** (issue_key, rca_summary, fix_strategy, etc.) — holds the streaming connection for the full duration
6. Sends `send_task_success` or `send_task_failure` back to Step Functions
7. Exits (ECS task terminates)

**Architecture:**
```
Step Functions (waitForTaskToken, 1h timeout, 10-min heartbeat)
  → Dispatcher Lambda (30s) — stores token, calls ecs:RunTask, returns
    → ECS Worker (no timeout) — drives AgentCore, sends callback
      → AgentCore Runtime (Strands agent: clone, fix, test, PR)
```

**Key lessons learned:**
- `invoke_agent_runtime` expects a **structured dict** matching the `@app.entrypoint` function's parameter. Passing `{"prompt": "..."}` causes the agent to receive empty fields (`issue_key = UNKNOWN-0`).
- `nohup ... &` inside `invoke_agent_runtime_command` is unreliable — the runtime session lifecycle is tied to the command, not the shell process tree.
- ECS Fargate has no timeout constraint (tasks run until they exit or are stopped). Combined with Step Functions heartbeats, this gives unlimited execution time.
- The ECS worker is tiny (just boto3, ~50MB image) — no heavy dependencies, starts in <5s.

**Alternatives considered:**
| Option | Pros | Cons |
|--------|------|------|
| nohup background (tried) | No Lambda timeout | Doesn't work — session kills orphaned processes |
| Lambda foreground (tried) | Simple | 15-min hard cap |
| Step Functions direct SDK integration | No Lambda at all | Streaming response incompatible with SF output format; InvokeHarness capped at 15 min |
| **ECS Worker (chosen)** | No timeout; holds streaming connection; heartbeats keep SF alive; full logging | Adds ECS task definition; ~30s cold start on first run |

**Logging visibility:**
- `/agentic-pipeline/resolution-worker` — worker lifecycle (every step logged: token retrieval, workspace setup, agent start/complete, callback sent)
- `/aws/bedrock-agentcore/runtimes/agentic_pipeline_resolution_agent-*` — AgentCore runtime command logs
- `/agentic-pipeline/agentcore-resolution` — application-level agent logs (tool calls, model invocations)

---

## ADR-009: AgentCore Memory for Cross-Session Learning

**Status:** Implemented (2026-07-24)  
**Context:** Each resolution runs independently — the agent doesn't learn from previous fixes. The Knowledge Base stores curated resolution documents, but the agent's own operational learnings (per-repo patterns, gotchas encountered, successful strategies) are lost between sessions.

**Decision:** Integrate AgentCore Memory with three built-in extraction strategies.

**Strategies chosen:**
| Strategy | What it captures | Why |
|----------|-----------------|-----|
| **Semantic** | Technical facts, code patterns | "This repo uses divisor pattern for percentage calculations" |
| **User Preference** | Repo conventions, tools, naming | "Uses pytest, conventional commits, fix/ branches" |
| **Summarization** | Session outcomes | "ARP-38: fixed /10→/100, added parametric tests, PR#4" |

**Design choices:**
- Single `actor_id = "resolution-agent"` — all resolutions share one memory pool for cross-repo learning
- `session_id = "resolution-{issue_key}"` — one session per ticket (retries recall previous attempts)
- Transparent integration via Strands `session_manager` — no code changes to the agent's reasoning loop
- 90-day TTL — long enough to accumulate; short enough to avoid stale patterns

**What we skipped:**
| Feature | Why |
|---------|-----|
| Episodic strategy | Useful for multi-turn human conversations, not single-burst agent sessions |
| Custom extraction prompts | Built-in strategies are sufficient for v1 |
| Per-repo actor_id | Would isolate memories; cross-repo learning is more valuable |

**Value proposition:** First fix on a repo is cold (~60s). Subsequent fixes benefit from recalled patterns — fewer tool calls, faster resolution, fewer failures from repeated mistakes.

See [MEMORY_INTEGRATION.md](MEMORY_INTEGRATION.md) for full implementation details.

---

## ADR-010: AgentCore Identity for Credential Management

**Status:** Planned  
**Context:** The Resolution Agent currently fetches credentials from Secrets Manager via shell script (`setup_workspace.sh`) and writes them to disk (`/workspace/.api-credentials`). This works but leaves long-lived tokens on the container filesystem.

**Decision:** Migrate to AgentCore Identity in a future iteration.

**What it gives us:**
- Credentials injected in-memory via `@requires_api_key` decorator — never on disk
- Centralized audit trail (CloudTrail) under the agent's workload identity ARN
- Automatic token refresh for OAuth providers
- Can reference existing Secrets Manager secrets (`EXTERNAL` source) — no data migration

**Why not now:**
- Current approach works and is deployed
- `setup_workspace.sh` (git clone) runs before Python starts — shell-based fetch stays unless we move clone into Python
- Low incremental risk reduction for the effort right now
- Better to implement after the IoT demo app when we have more services to authenticate against

**Migration path (when ready):**
1. Create `ApiKeyCredentialProvider` via CDK (reference existing `agentic-pipeline/repo-credentials` secret)
2. Update Python agent code to use `@requires_api_key` for PR creation and Jira calls
3. Keep shell-based clone as-is (or move into Python)
