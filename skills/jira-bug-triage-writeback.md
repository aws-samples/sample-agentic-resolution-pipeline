# DevOps Agent Space — Skill for Jira Bug-Triage Write-Back

A single custom Skill to add to your DevOps Agent Space so the autonomous bug-triage loop closes itself: Jira webhook fires → agent investigates → agent posts RCA back to the originating ticket without a human in the chat.

This supersedes the older `AGENT_SPACE_PROMPT.md` (which assumed a single system-prompt surface and bundled scope the agent already handles natively).

## Why a Skill, not Instructions

Instructions are loaded for **every session** — chat-driven Q&A, PagerDuty alerts, Grafana alerts, Jira webhooks alike. We don't want "post to Jira" behavior bleeding into interactive chat or alert sessions where the trigger has nothing to do with Jira.

A Skill is loaded **conditionally** — the agent picks it up only when the incident's `data.metadata.source == "jira"`. Cleaner separation, no per-session token cost, no risk of the wrong heuristic firing.

## What's NOT in this Skill (and why)

- **PagerDuty incident write-back.** The DevOps Agent has built-in PagerDuty incident write-back — it always writes its findings back to the originating PagerDuty incident. We don't need a Skill for that.
- **Grafana / CloudWatch / Datadog → Jira follow-up tickets.** Out of scope for v1. If a team later wants alert-driven engineering follow-ups in Jira, that's a separate Skill — it can be added without touching this one.

This Skill addresses one specific gap: the DevOps Agent has no built-in behavior for writing back to a Jira ticket that was *the trigger* of an investigation. That's what we're filling.

## How to add this Skill

1. Open your DevOps Agent Space.
2. Knowledge → **Skills** tab.
3. Under **Custom skills**, click **Add skill**.
4. Paste the body below into the editor. The Console renders Skill bodies as markdown.
5. Save.

---

## Skill — `jira-bug-triage-writeback`

**Name:** `jira-bug-triage-writeback`

**Description (one-liner shown in the Skills list):**

> Closes the loop on Jira-sourced bug tickets — posts the RCA as a comment back to the originating ticket using the Jira MCP.

**Body:**

```
# When to use this skill

Use this skill when an incident event arrives with
`data.metadata.source == "jira"`. These come from the Jira webhook
receiver Lambda — they represent a newly-created Bug ticket (or
other issue type) in a Jira project that the team wants the agent
to triage.

The originating Jira issue key is in `data.metadata.issue_key`
(e.g. "KAN-4"). The Jira project key is in `data.metadata.project`.

Do NOT use this skill if `data.metadata.source` is anything other
than "jira". Other event sources (PagerDuty, Grafana, CloudWatch,
etc.) have their own native write-back paths handled by the agent.

Do NOT use this skill if the trigger is a chat message from the
user. Chat sessions are interactive — wait for the user to ask
before writing to Jira.

# What to do

1. IMMEDIATELY post a starting comment to the Jira ticket using
   `jira-mcp-server.add_comment`:

   ```
   [Agent] Investigation started for {issue_key}. Will post
   findings as I discover them.
   ```

   Do this FIRST, before any other tool call.

2. Investigate the issue. Use any Capability Provider necessary —
   Jira MCP for additional ticket context, Grafana / Datadog /
   CloudWatch / X-Ray for telemetry, CodeCommit or GitHub for code
   inspection, custom MCP for business context.

   IMPORTANT — PROGRESSIVE UPDATES: Every time you confirm a
   significant finding during investigation, IMMEDIATELY post it
   to the Jira ticket via `jira-mcp-server.add_comment` before
   continuing to the next investigation step. Do NOT wait until
   the end. Post each finding as you go. Format each update as:

   ```
   [Agent] Finding: <one-line title>

   <2-3 sentence explanation with evidence>
   ```

   You should be calling add_comment roughly every 3-5 minutes
   throughout the investigation. If you have gone more than 5
   minutes without posting an update, you are doing it wrong —
   stop and post what you have so far.

3. After completing your investigation, post a DETAILED final
   summary comment using `jira-mcp-server.add_comment`. This is
   the comprehensive write-up — be thorough and specific. Include
   exact metrics, timestamps, resource names, and evidence chains.
   Format:

   ```
   [Agent] Investigation completed for <ticket summary>

   ROOT CAUSE:
   <detailed multi-sentence explanation of the root cause. Include
    specific resource names, timestamps, config values, and the
    causal chain. If unable to confirm root cause due to missing
    evidence or access, write "Unable to confirm" and explain what
    evidence would be needed. Be as specific as the KAN-4 example:
    name the service, the exact change, who made it, when, and the
    mechanism by which it causes the reported symptoms.>

   KEY FINDINGS:
   - <specific finding with quantitative evidence, e.g. "ECS task
     role only permits xray, cloudwatch — no ses:SendEmail">
   - <specific finding with metrics, e.g. "Zero emails sent via
     SES in past 7 days; 81,034 log records contain no email refs">
   - <specific finding with resource details>
   - <include as many findings as your investigation uncovered —
     do not limit to 3>

   RECOMMENDED REMEDIATION:
   1. <specific actionable step with technical detail — name the
      exact config/code/resource to change>
   2. <specific actionable step>
   3. <specific actionable step>

   INVESTIGATION GAP (if any):
   <observability or access limitations that blocked deeper
    investigation. Name specific permissions denied, APIs that
    returned errors, or telemetry that was missing. Explain what
    convergent evidence you used instead and your confidence level.>
   ```

   This final comment should be comprehensive enough that an
   engineer can act on it without re-investigating. Think of it as
   a mini incident report posted directly on the ticket.

5. Do NOT transition the ticket. Even if you believe the issue is
   resolved, leave the workflow state to humans. The Jira MCP's
   policy engine will reject most terminal transitions anyway, but
   don't even attempt them.

6. Do NOT close, resolve, or assign the ticket. Comment-only is the
   contract. Engineers and managers own those decisions.

7. If your investigation reveals follow-up engineering work that's
   distinct from the original bug (a missing test, a broken
   pipeline, a related observability gap), file separate Jira
   tickets via `jira-mcp-server.create_issue` in the same project,
   and link them to the original via `jira-mcp-server.link_issues`
   with the `Relates` link type.

# Why each rule earns its slot

| Rule | What it prevents |
|---|---|
| Post "[Agent] Investigation started" comment FIRST | Forces immediate add_comment call; engineer sees engagement within seconds |
| Progressive updates every 3-5 minutes during investigation | Keeps add_comment in active tool memory throughout; prevents context-window loss over long investigations |
| Final summary comment after investigation completes | Provides structured RCA even if progressive updates covered the findings |
| Always comment back on `source == "jira"` | Closes the autonomous loop — engineer wakes up to a triaged ticket, no manual prompt needed |
| `[Agent]` prefix | Webhook receiver's self-loop guardrail recognizes this and drops `comment_created` events from the agent |
| Don't transition / close / resolve / assign | Workflow decisions belong to humans; MCP policy engine enforces this in code, the skill enforces it in intent |
| Follow-ups via `create_issue` + `link_issues` | Concrete deliverables instead of buried recommendations |
| Skip on chat and non-Jira triggers | Avoids accidentally writing to Jira during exploratory user conversations or duplicating PagerDuty's native write-back |

# Tools used

- `jira-mcp-server.get_issue` (additional ticket context if needed)
- `jira-mcp-server.add_comment` (mandatory — the close-the-loop write)
- `jira-mcp-server.create_issue` (only if filing follow-up tickets)
- `jira-mcp-server.link_issues` (only if filing follow-up tickets)
- Other Capability Providers as needed for the investigation itself
```

---

## Testing the skill

After the skill is added to the Space:

### Test — autonomous Jira write-back

Create a new Bug ticket in project KAN, e.g., `KAN-5`. Title: "Order confirmation email not sent for international shipping addresses." **Don't say anything in chat.** Wait 5–30 minutes.

Expected:
- **Webhook receiver audit log:** `forwarded` event for KAN-5
- **MCP server audit log:** `tool_ok get_issue` (initial autonomous read), then later `tool_ok add_comment` (RCA write-back)
- **Jira UI on KAN-5:** new comment starting with `[Agent] Investigation completed for...`
- **No re-trigger:** the agent's comment does NOT re-fire the webhook (self-loop guardrail catches the `[Agent]` prefix)

### Negative test — chat session

In the Space chat, type: "What's in KAN-1?". Expected: agent reads and tells you, **no comment posted**. The skill's "skip on chat" rule keeps it out of interactive sessions.

### Negative test — PagerDuty incident

Trigger any PagerDuty alert (existing chaos pipeline or manual). Expected: agent investigates and writes back to **PagerDuty** (via its built-in path), NOT to Jira. Confirms the skill correctly stays dormant when `source != "jira"`.

---

## How this composes with the rest of the integration

| Component | Direction | Purpose |
|---|---|---|
| Webhook receiver Lambda | Jira → Agent | HMAC-verify, JQL-filter, normalize, dedupe, sign-forward |
| **This Skill** | Agent → Jira | Conditional: post RCA back on the originating Jira ticket |
| `policy.yaml` | server-side gate | Per-project field/transition allowlists, never-auto-close-P1 |
| Idempotency cache | retry safety | Webhook redelivery doesn't double-investigate |
| Self-loop guardrail | trigger filter | Agent-authored comments (recognized via `[Agent]` prefix) don't re-fire the agent |
| Audit log | observability | Every event captured to CloudWatch |

The Skill is the *behavioral contract*. The MCP + policy + redaction are the *enforcement layer*. They compose: even if the Skill were misconfigured to attempt a forbidden action (e.g., "transition the ticket to Done"), the policy engine would reject it server-side. Belt and suspenders.

---

## Future Skills

If the team adopts this pattern, candidates for additional Skills:

| Skill | Trigger | Action |
|---|---|---|
| `jira-stale-ticket-sweep` | Scheduled cron | JQL search for tickets stale > N days; bulk-comment + label |
| `jira-deploy-update` | CI/CD success event | Find linked tickets, comment with deploy ID + commit SHA, transition to "Ready for QA" |
| `jira-duplicate-merge` | When investigating, find existing tickets with same root cause | `link_issues` with "Duplicate" type and post a "marked as duplicate of <key>" comment |

Each is small (a few paragraphs of behavior), uses the same MCP tools, composes with the same policy + redaction safety. They make the agent's Jira behavior **library-shaped** rather than monolithic.
