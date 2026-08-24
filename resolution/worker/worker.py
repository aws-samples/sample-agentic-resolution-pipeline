#!/usr/bin/env python3
"""
ECS Fargate Worker — drives AgentCore runtime for resolution tasks.

Runs as a one-shot ECS task started by Step Functions (ecs:RunTask.sync).
Holds the streaming connection to invoke_agent_runtime for the full duration
of the agent's work (no timeout constraint beyond the 1h SF timeout).

Flow:
  1. Read payload from TASK_PAYLOAD env var (JSON, set by SF container overrides)
  2. Retrieve task token from DynamoDB
  3. Start heartbeat thread (keeps SF token alive)
  4. Setup workspace via invoke_agent_runtime_command
  5. Run the agent via invoke_agent_runtime (holds streaming connection)
  6. Send callback (success/failure) to Step Functions
  7. Exit (ECS task terminates, SF detects completion)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
import threading
import time
import uuid

import boto3
from botocore.config import Config

# ── Logging setup ──────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
logger = logging.getLogger("resolution-worker")

# ── Configuration ──────────────────────────────────────────────────────────────

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
TASK_TOKEN_TABLE = os.environ.get("TASK_TOKEN_TABLE", "agentic-pipeline-task-tokens")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")

AGENTCORE_CONFIG = Config(
    region_name=AWS_REGION,
    read_timeout=3600,
    connect_timeout=30,
    retries={"max_attempts": 2},
)

SFN_CLIENT = boto3.client("stepfunctions", region_name=AWS_REGION)
DDB = boto3.resource("dynamodb", region_name=AWS_REGION)
AGENTCORE_CLIENT = boto3.client("bedrock-agentcore", config=AGENTCORE_CONFIG)
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)


# ── Heartbeat ──────────────────────────────────────────────────────────────────

class HeartbeatSender:
    """Sends periodic heartbeats to Step Functions to prevent task timeout."""

    def __init__(self, task_token: str, interval: int = 300):
        self.task_token = task_token
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        logger.info(f"Heartbeat started (every {self.interval}s)")

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        logger.info("Heartbeat stopped")

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            try:
                SFN_CLIENT.send_task_heartbeat(taskToken=self.task_token)
                logger.info("Heartbeat sent")
            except Exception as e:
                logger.warning(f"Heartbeat failed: {e}")


# ── AgentCore interaction ──────────────────────────────────────────────────────

def run_command(session_id: str, command: str, timeout: int = 120) -> str:
    """Execute a shell command in the AgentCore container and return output."""
    logger.info(f"[CMD] {command[:200]}")
    response = AGENTCORE_CLIENT.invoke_agent_runtime_command(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/vnd.amazon.eventstream",
        body={"command": command, "timeout": timeout},
    )

    output = ""
    exit_code = None
    for evt in response.get("stream", []):
        if "chunk" in evt:
            chunk = evt["chunk"]
            if "contentDelta" in chunk:
                delta = chunk["contentDelta"]
                stdout = delta.get("stdout", "")
                stderr = delta.get("stderr", "")
                if stdout:
                    output += stdout
                if stderr:
                    output += stderr
                    logger.debug(f"[CMD stderr] {stderr.strip()}")
            if "contentStop" in chunk:
                exit_code = chunk["contentStop"].get("exitCode")

    if exit_code and exit_code != 0:
        logger.error(f"[CMD FAILED] exit={exit_code} output={output[-500:]}")
        raise RuntimeError(f"Command failed (exit {exit_code}): {output[-500:]}")

    logger.info(f"[CMD OK] exit={exit_code} output_len={len(output)}")
    return output


def run_agent(session_id: str, payload: dict) -> str:
    """
    Invoke the agent via invoke_agent_runtime (LLM reasoning loop).
    Passes the structured payload (issue_key, rca_summary, etc.) directly
    to the @app.entrypoint function in agent.py.
    Holds the streaming connection for the full duration of the agent's work.
    """
    logger.info(f"[AGENT] Starting agent invocation (issue_key={payload.get('issue_key')})")
    start_time = time.time()

    response = AGENTCORE_CLIENT.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
        qualifier="DEFAULT",
    )

    response_body = response.get("response", b"")
    if isinstance(response_body, bytes):
        agent_output = response_body.decode("utf-8")
    elif hasattr(response_body, "read"):
        agent_output = response_body.read().decode("utf-8")
    else:
        agent_output = str(response_body)

    elapsed = time.time() - start_time
    logger.info(f"[AGENT] Completed in {elapsed:.1f}s (output_len={len(agent_output)})")
    logger.info(f"[AGENT] Output (last 1000 chars): {agent_output[-1000:]}")

    return agent_output


# ── Workspace setup ────────────────────────────────────────────────────────────

def setup_workspace(session_id: str, repo_url: str, branch: str, provider: str, auth_secret_arn: str):
    """Clone repo and configure git credentials inside the AgentCore container."""
    logger.info(f"[SETUP] Cloning {repo_url} (branch={branch}, provider={provider})")
    setup_script = (
        f"export REPO_URL={shlex.quote(repo_url)} BRANCH={shlex.quote(branch)} "
        f"GIT_PROVIDER={shlex.quote(provider)} AUTH_SECRET_ARN={shlex.quote(auth_secret_arn)} && "
        f"bash /app/setup_workspace.sh"
    )
    output = run_command(session_id, f'bash -c "{setup_script}"', timeout=120)
    logger.info(f"[SETUP] Workspace ready: {output[:200]}")


# ── Result extraction ──────────────────────────────────────────────────────────

def extract_pr_url(text: str) -> str:
    """Try to find a PR URL in the agent's output."""
    for line in text.split("\n"):
        if line.strip().startswith("PR_URL="):
            return line.strip().split("=", 1)[1].strip()

    match = re.search(r'https://bitbucket\.org/[^/]+/[^/]+/pull-requests/\d+', text)
    if match:
        return match.group(0)

    match = re.search(r'https://github\.com/[^/]+/[^/]+/pull/\d+', text)
    if match:
        return match.group(0)

    return ""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Payload can come via direct env var (small payloads) or S3 reference (large payloads)
    task_payload_s3 = os.environ.get("TASK_PAYLOAD_S3")
    task_payload_raw = os.environ.get("TASK_PAYLOAD")

    if task_payload_s3:
        ref = json.loads(task_payload_s3)
        logger.info(f"Loading payload from s3://{ref['bucket']}/{ref['key']}")
        resp = S3_CLIENT.get_object(Bucket=ref["bucket"], Key=ref["key"])
        payload = json.loads(resp["Body"].read().decode())
    elif task_payload_raw:
        payload = json.loads(task_payload_raw)
    else:
        logger.error("Neither TASK_PAYLOAD_S3 nor TASK_PAYLOAD env var set")
        sys.exit(1)
    issue_key = payload["issue_key"]
    stage = payload.get("stage", "awaiting_resolution")
    plan = payload.get("plan", {})
    rca_result = payload.get("rca_result", {})

    logger.info(f"{'='*60}")
    logger.info(f"RESOLUTION WORKER START — {issue_key}")
    logger.info(f"{'='*60}")
    logger.info(f"Payload keys: {list(payload.keys())}")

    # ── Retrieve task token ────────────────────────────────────────────────────
    logger.info(f"[TOKEN] Retrieving task token for {issue_key}/{stage}")
    table = DDB.Table(TASK_TOKEN_TABLE)
    resp = table.get_item(Key={"issue_key": issue_key, "stage": stage})
    item = resp.get("Item")
    if not item:
        logger.error(f"[TOKEN] No task token found for {issue_key}/{stage}")
        sys.exit(1)
    task_token = item["task_token"]
    logger.info(f"[TOKEN] Retrieved (len={len(task_token)})")

    # ── Start heartbeat ────────────────────────────────────────────────────────
    heartbeat = HeartbeatSender(task_token, interval=300)
    heartbeat.start()

    try:
        # ── Build session ID ───────────────────────────────────────────────────
        session_id = f"resolution-{issue_key}-{uuid.uuid4().hex[:8]}".ljust(33, "0")
        logger.info(f"[SESSION] ID: {session_id}")

        # ── Setup workspace ────────────────────────────────────────────────────
        repo_url = plan.get("repo_url", "")
        branch = plan.get("default_branch", "main")
        provider = plan.get("provider", "github")
        auth_secret_arn = plan.get("auth_secret_arn", "")

        setup_workspace(session_id, repo_url, branch, provider, auth_secret_arn)

        # ── Build agent payload ────────────────────────────────────────────────
        agent_payload = {
            "issue_key": issue_key,
            "rca_summary": rca_result.get("rca_summary", ""),
            "kb_context": plan.get("kb_context", ""),
            "repo_url": repo_url,
            "branch": branch,
            "git_provider": provider,
            "fix_strategy": plan.get("strategy", ""),
        }
        logger.info(f"[AGENT] Payload: issue_key={issue_key}, strategy_len={len(agent_payload['fix_strategy'])}")

        # ── Run the agent ──────────────────────────────────────────────────────
        agent_output = run_agent(session_id, agent_payload)

        # ── Extract results ────────────────────────────────────────────────────
        pr_url = extract_pr_url(agent_output)
        status = "success" if pr_url else "no_pr_created"
        logger.info(f"[RESULT] status={status} pr_url={pr_url}")

        # ── Write to S3 ───────────────────────────────────────────────────────
        output_data = {
            "issue_key": issue_key,
            "pr_url": pr_url,
            "status": status,
            "session_id": session_id,
            "rca_summary": rca_result.get("rca_summary", ""),
            "strategy": plan.get("strategy", ""),
            "kb_context": plan.get("kb_context", ""),
            "target_files": plan.get("target_files", []),
            "category": payload.get("classification", {}).get("category", "BUG_TICKET"),
            "agent_output_tail": agent_output[-2000:],
        }

        if OUTPUT_BUCKET:
            S3_CLIENT.put_object(
                Bucket=OUTPUT_BUCKET,
                Key=f"resolutions/{issue_key}/output.json",
                Body=json.dumps(output_data, default=str),
                ContentType="application/json",
            )
            logger.info(f"[S3] Output written to s3://{OUTPUT_BUCKET}/resolutions/{issue_key}/output.json")

        # ── Send success callback ──────────────────────────────────────────────
        callback_output = {
            "issue_key": issue_key,
            "pr_url": pr_url,
            "status": status,
            "session_id": session_id,
        }
        SFN_CLIENT.send_task_success(
            taskToken=task_token,
            output=json.dumps(callback_output, default=str),
        )
        logger.info(f"[CALLBACK] Success sent to Step Functions")

        # ── Clean up token ─────────────────────────────────────────────────────
        table.delete_item(Key={"issue_key": issue_key, "stage": stage})
        logger.info(f"[TOKEN] Cleaned up")

    except Exception as e:
        logger.exception(f"[FAILED] Resolution failed for {issue_key}: {e}")
        try:
            SFN_CLIENT.send_task_failure(
                taskToken=task_token,
                error="ResolutionFailed",
                cause=str(e)[:256],
            )
            logger.info(f"[CALLBACK] Failure sent to Step Functions")
            table.delete_item(Key={"issue_key": issue_key, "stage": stage})
        except Exception as cb_err:
            logger.error(f"[CALLBACK] Failed to send failure callback: {cb_err}")
        sys.exit(1)

    finally:
        heartbeat.stop()
        logger.info(f"{'='*60}")
        logger.info(f"RESOLUTION WORKER END — {issue_key}")
        logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
