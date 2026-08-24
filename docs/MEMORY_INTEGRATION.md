# AgentCore Memory Integration

How the Resolution Agent uses AgentCore Memory to learn from past resolutions and get smarter over time.

---

## Overview

AgentCore Memory gives the Resolution Agent **institutional memory** — it learns from every fix it performs and applies that knowledge to future resolutions. Without memory, every invocation starts cold. With memory, the agent recalls repo patterns, successful strategies, and past mistakes.

---

## How Memory Works

### Two Layers

| Layer | What it stores | Lifetime | How it's used |
|-------|---------------|----------|---------------|
| **Short-Term Memory (STM)** | Raw conversation turns (user message, tool calls, responses) | TTL: 90 days | Provides full context within a session |
| **Long-Term Memory (LTM)** | Extracted insights (facts, patterns, preferences) | TTL: 90 days | Retrieved via semantic search before each new resolution |

STM is written automatically on every turn. LTM is **extracted asynchronously** by the service — you never write LTM records directly. Built-in extraction strategies analyze the conversation and distill durable knowledge.

### Extraction Strategies (What Gets Remembered)

We use three built-in strategies:

| Strategy | What it captures | Example |
|----------|-----------------|---------|
| **Semantic** | Facts, knowledge, technical concepts | "order_calculator.py uses a divisor pattern for percentage calculations" |
| **User Preference** | Repo patterns, conventions, recurring behaviors | "This repo uses pytest, conventional commits, fix/ branch naming" |
| **Summarization** | Session summaries (what happened, what was decided) | "ARP-38: Fixed /10→/100 discount bug, added parametric tests, PR#4 created in 61s" |

### What Gets Retrieved (Before Each Resolution)

When the agent starts a new resolution, the Memory session manager automatically:
1. Searches LTM for records semantically related to the current task
2. Injects the top-10 most relevant memories into the agent's context
3. The agent uses these as additional context alongside the RCA, fix strategy, and KB results

---

## How This Helps

### First fix on a repo (cold start)
- No memories available
- Agent explores from scratch: finds test framework, understands structure, learns conventions
- After completing: STM → LTM extraction captures what it learned

### Subsequent fixes on the same repo
- Memory retrieves: "this repo uses pytest with fixtures in conftest.py", "PRs need conventional commit titles", "tests live in tests/ not test/"
- Agent skips exploration, goes straight to the fix
- Faster resolution, fewer tool calls

### Cross-repo pattern learning
- Memory retrieves: "division bugs in financial calculations often need validation guards"
- Agent applies lessons from one repo's fixes to another
- Compound value over time

### Failure avoidance
- Memory retrieves: "main branch has pre-commit hook rejecting non-conventional commits"
- Agent avoids repeating past mistakes (failed pushes, wrong branch names)

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Resolution Run                      │
│                                                     │
│  1. Retrieve: search LTM for relevant memories      │
│          ↓                                          │
│  2. Agent runs: clone → analyze → fix → test → PR   │
│          ↓                                          │
│  3. Write: each turn → STM (automatic)              │
│          ↓                                          │
│  4. Extract: STM → LTM (async, by service)          │
│          ↓                                          │
│  5. Next run benefits from extracted LTM            │
└─────────────────────────────────────────────────────┘
```

---

## Implementation Details

### CDK (infrastructure/stacks/resolution_agentcore_stack.py)

```python
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
```

The `MEMORY_ID` is passed to the AgentCore Runtime as an environment variable. The runtime role is granted `grant_full_access` on the Memory resource.

### Agent Code (resolution/agentcore/agent.py)

```python
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig

config = AgentCoreMemoryConfig(
    memory_id=os.getenv("MEMORY_ID"),
    session_id=f"resolution-{issue_key}",
    actor_id="resolution-agent",
    batch_size=1,
    retrieval_config={
        "/strategies/": RetrievalConfig(top_k=10, relevance_score=0.5),
    },
)

session_manager = AgentCoreMemorySessionManager(config, region_name="us-east-1")

agent = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=[shell, file_read, file_write, editor],
    session_manager=session_manager,
)
```

### Key Design Choices

| Choice | Rationale |
|--------|-----------|
| `actor_id = "resolution-agent"` | Single actor — all resolutions share the same memory pool. The agent learns globally, not per-user. |
| `session_id = "resolution-{issue_key}"` | One session per ticket. If the same ticket is retried, the agent can recall what it tried last time. |
| `batch_size = 1` | Immediate write — every turn goes to STM as it happens. No risk of losing data if the container exits unexpectedly. |
| `top_k = 10, relevance_score = 0.5` | Retrieve 10 memories with at least 50% relevance. Balances context window usage vs. memory recall. |
| `expiration_duration = 90 days` | Long enough to accumulate useful patterns; short enough to avoid stale knowledge. |

---

## What We Did NOT Use (and Why)

| Feature | Why skipped |
|---------|-------------|
| **Episodic strategy** | Useful for multi-turn conversations with a human. Our agent runs a single burst session — no episodic patterns to extract. |
| **Custom extraction prompts** | Built-in strategies are sufficient for v1. Custom prompts add complexity; we can add them later if extraction quality is poor. |
| **Per-repo actor_id** | Would isolate memories by repo. Decided against because cross-repo learning (patterns in Python discount code apply to other Python services) is more valuable. |
| **Self-managed strategy** | Maximum control but requires running our own extraction pipeline. Overkill for now. |

---

## Observability

- Memory records can be listed via `aws bedrock-agentcore list-memory-records --memory-id <id>`
- STM events can be listed via `aws bedrock-agentcore list-events --memory-id <id> --actor-id resolution-agent --session-id <session>`
- CloudWatch metrics for Memory are published under the `bedrock-agentcore` namespace

---

## Cost

At our volume (a few resolutions per day):
- STM writes: ~$0.001/day (negligible)
- LTM storage: ~$0.01/month
- LTM retrieval: ~$0.005/day

Total: less than $1/month.
