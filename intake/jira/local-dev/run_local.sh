#!/usr/bin/env bash
# Run the Jira MCP server locally with in-memory Jira / DynamoDB / Secrets Manager.
# No AWS account, no real Jira, no credentials needed.
#
# Usage:
#   ./local-dev/run_local.sh           # start server on :8081
#   PORT=9000 ./local-dev/run_local.sh # different port
#
# Stop with Ctrl-C. State is in-process — restarts give a fresh world.

set -euo pipefail

cd "$(dirname "$0")/.."

# Local-mode flag — flips production modules to in-memory backends.
export JIRA_MCP_LOCAL_MODE=true

# Required by the production code; values don't matter in local mode.
export JIRA_BASE_URL="https://local.test"
export JIRA_SECRET_ARN="local"
export JIRA_IDEMPOTENCY_TABLE="local-idempotency"
export JIRA_METADATA_CACHE_TABLE="local-metadata"

# Try out the rollout-safety toggle by setting JIRA_MCP_DRY_RUN=true before launching.
export JIRA_MCP_DRY_RUN="${JIRA_MCP_DRY_RUN:-false}"

# Policy file is bundled alongside server.py.
export JIRA_POLICY_PATH="$(pwd)/policy.yaml"

export PORT="${PORT:-8081}"

# Point audit log at a file you can `tail -f` in a third terminal.
export JIRA_MCP_LOCAL_AUDIT_FILE="${JIRA_MCP_LOCAL_AUDIT_FILE:-/tmp/jira-mcp-audit.log}"
: > "$JIRA_MCP_LOCAL_AUDIT_FILE"   # truncate at startup so each run is fresh

echo "Starting Jira MCP server on http://localhost:${PORT}/mcp"
echo "  LOCAL_MODE=true  DRY_RUN=${JIRA_MCP_DRY_RUN}"
echo "  Seeded issues: OPS-100, OPS-101, OPS-102, SEC-100"
echo "  Audit log file: $JIRA_MCP_LOCAL_AUDIT_FILE"
echo "  Tail it in another terminal: tail -f $JIRA_MCP_LOCAL_AUDIT_FILE"
echo
exec python3 -u server.py
