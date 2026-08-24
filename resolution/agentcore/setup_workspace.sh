#!/bin/bash
# Pre-session setup: clone repo and configure git credentials.
# Called via InvokeAgentRuntimeCommand before the agent is invoked.

set -euo pipefail

REPO_URL="${REPO_URL:?REPO_URL required}"
BRANCH="${BRANCH:-main}"
GIT_PROVIDER="${GIT_PROVIDER:-github}"
AUTH_SECRET_ARN="${AUTH_SECRET_ARN:-}"

WORKSPACE="/workspace/repo"

# Fetch credentials from Secrets Manager if configured (strip stray newlines from paste errors)
if [ -n "$AUTH_SECRET_ARN" ]; then
    GIT_TOKEN=$(aws secretsmanager get-secret-value --secret-id "$AUTH_SECRET_ARN" --query 'SecretString' --output text 2>/dev/null | tr -d '\n\r' || echo "")
fi

# Configure git credentials by provider using credential helper (avoids URL-encoding issues)
CLONE_URL="$REPO_URL"
if [ -n "${GIT_TOKEN:-}" ]; then
    CRED_FILE="/tmp/.git-credentials"
    case "$GIT_PROVIDER" in
        github)
            GIT_USERNAME="x-access-token"
            ENCODED_TOKEN=$(printf '%s' "$GIT_TOKEN" | python3 -c "import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read(),safe=''))")
            echo "https://${GIT_USERNAME}:${ENCODED_TOKEN}@github.com" > "$CRED_FILE"
            ;;
        gitlab)
            GIT_USERNAME="oauth2"
            ENCODED_TOKEN=$(printf '%s' "$GIT_TOKEN" | python3 -c "import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read(),safe=''))")
            echo "https://${GIT_USERNAME}:${ENCODED_TOKEN}@gitlab.com" > "$CRED_FILE"
            ;;
        bitbucket)
            # Token format: email:token — use workspace name for git, email for API
            REPO_PATH="${REPO_URL#https://bitbucket.org/}"
            WORKSPACE_NAME="${REPO_PATH%%/*}"
            API_TOKEN=$(printf '%s' "$GIT_TOKEN" | cut -d: -f2-)
            ENCODED_TOKEN=$(printf '%s' "$API_TOKEN" | python3 -c "import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read(),safe=''))")
            echo "https://${WORKSPACE_NAME}:${ENCODED_TOKEN}@bitbucket.org" > "$CRED_FILE"
            ;;
        *)
            CRED_FILE=""
            ;;
    esac
    if [ -n "$CRED_FILE" ] && [ -f "$CRED_FILE" ]; then
        chmod 600 "$CRED_FILE"
        git config --global credential.helper "store --file=$CRED_FILE"
    fi
fi

# Clone
rm -rf "$WORKSPACE"
git clone --depth 50 --branch "$BRANCH" "$CLONE_URL" "$WORKSPACE"

# Configure git user
cd "$WORKSPACE"
git config user.name "Resolution Agent"
git config user.email "resolution-agent@pipeline.local"

# Write API credentials for PR creation (Bitbucket needs email:token for REST API)
if [ -n "${GIT_TOKEN:-}" ] && [ "$GIT_PROVIDER" = "bitbucket" ]; then
    API_EMAIL=$(echo "$GIT_TOKEN" | cut -d: -f1)
    API_TOKEN_VAL=$(echo "$GIT_TOKEN" | cut -d: -f2-)
    BB_REPO_PATH="${REPO_URL#https://bitbucket.org/}"
    BB_WORKSPACE="${BB_REPO_PATH%%/*}"
    BB_REPO="${BB_REPO_PATH#*/}"
    BB_REPO="${BB_REPO%.git}"
    cat > /workspace/.api-credentials <<EOF
BITBUCKET_EMAIL=$API_EMAIL
BITBUCKET_TOKEN=$API_TOKEN_VAL
BITBUCKET_WORKSPACE=$BB_WORKSPACE
BITBUCKET_REPO=$BB_REPO
EOF
    chmod 600 /workspace/.api-credentials
fi

echo "Workspace ready: $WORKSPACE (branch: $BRANCH)"
