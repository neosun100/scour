#!/usr/bin/env bash
#
# setup.sh — create AWS Bedrock AgentCore Web Search from scratch.
#
# Creates:
#   1. An IAM service role the gateway assumes (InvokeGateway + InvokeWebSearch).
#   2. An AgentCore Gateway with AWS_IAM inbound auth (callers use local creds).
#   3. A web-search connector target exposing the WebSearch tool.
#
# Then prints the gateway URL and (optionally) writes a local .env.
#
# Configuration via environment (all optional except credentials):
#   AWS_PROFILE     AWS profile to use (or rely on the default credential chain)
#   REGION          default: us-east-1  (Web Search is only in us-east-1)
#   GATEWAY_NAME    default: WebSearchGateway
#   ROLE_NAME       default: AgentCoreWebSearchGatewayRole
#   TARGET_NAME     default: web-search-tool
#
# Requirements: aws CLI v2, python3 with boto3>=1.43 (auto-installed into .venv).
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REGION="${REGION:-us-east-1}"
GATEWAY_NAME="${GATEWAY_NAME:-WebSearchGateway}"
ROLE_NAME="${ROLE_NAME:-AgentCoreWebSearchGatewayRole}"
TARGET_NAME="${TARGET_NAME:-web-search-tool}"
POLICY_NAME="WebSearchGatewayInline"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v aws >/dev/null     || die "aws CLI not found"
command -v python3 >/dev/null || die "python3 not found"

AWS_ARGS=(--region "$REGION")
[ -n "${AWS_PROFILE:-}" ] && AWS_ARGS+=(--profile "$AWS_PROFILE")

say "Resolving account identity..."
ACCOUNT_ID="$(aws "${AWS_ARGS[@]}" sts get-caller-identity --query Account --output text)" \
  || die "could not get caller identity — check your AWS credentials"
say "Account: $ACCOUNT_ID   Region: $REGION"

# ---------------------------------------------------------------------------
# Python env with boto3>=1.43 (the bundled CLI/older boto3 lack 'connector').
# ---------------------------------------------------------------------------
VENV="$HERE/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "Creating virtualenv with boto3>=1.43..."
  python3 -m venv "$VENV"
fi
PY="$VENV/bin/python"
"$PY" -m pip install -q --upgrade pip >/dev/null
"$PY" -m pip install -q "boto3>=1.43" "botocore>=1.43" >/dev/null
BOTO_VER="$("$PY" -c 'import boto3;print(boto3.__version__)')"
say "boto3 $BOTO_VER"

# ---------------------------------------------------------------------------
# 1. IAM service role
# ---------------------------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() { # render template $1 -> $2, substituting ACCOUNT_ID and REGION
  sed -e "s/\${ACCOUNT_ID}/$ACCOUNT_ID/g" -e "s/\${REGION}/$REGION/g" "$1" > "$2"
}
render "$HERE/iam/trust-policy.template.json"       "$TMP/trust.json"
render "$HERE/iam/permissions-policy.template.json" "$TMP/perms.json"

if aws "${AWS_ARGS[@]}" iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  say "IAM role $ROLE_NAME already exists — updating trust policy."
  aws "${AWS_ARGS[@]}" iam update-assume-role-policy \
    --role-name "$ROLE_NAME" --policy-document "file://$TMP/trust.json" >/dev/null
else
  say "Creating IAM role $ROLE_NAME..."
  aws "${AWS_ARGS[@]}" iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$TMP/trust.json" \
    --description "AgentCore Gateway role for Web Search connector" >/dev/null
fi

aws "${AWS_ARGS[@]}" iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "file://$TMP/perms.json" >/dev/null
ROLE_ARN="$(aws "${AWS_ARGS[@]}" iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)"
say "Role ARN: $ROLE_ARN"

say "Waiting for IAM role to propagate..."
sleep 10

# ---------------------------------------------------------------------------
# 2. Gateway (AWS_IAM inbound auth) — reuse if one with this name exists.
# ---------------------------------------------------------------------------
GATEWAY_ID="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control list-gateways \
  --query "items[?name=='$GATEWAY_NAME'].gatewayId | [0]" --output text 2>/dev/null || true)"

if [ -n "$GATEWAY_ID" ] && [ "$GATEWAY_ID" != "None" ]; then
  say "Gateway $GATEWAY_NAME already exists: $GATEWAY_ID"
else
  say "Creating gateway $GATEWAY_NAME..."
  GATEWAY_ID="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control create-gateway \
    --name "$GATEWAY_NAME" \
    --description "AgentCore Web Search via IAM/SigV4" \
    --role-arn "$ROLE_ARN" \
    --protocol-type MCP \
    --authorizer-type AWS_IAM \
    --query 'gatewayId' --output text)"
  say "Gateway ID: $GATEWAY_ID"
fi

say "Waiting for gateway to become READY..."
for _ in $(seq 1 30); do
  ST="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control get-gateway \
        --gateway-identifier "$GATEWAY_ID" --query 'status' --output text)"
  [ "$ST" = "READY" ] && break
  [ "$ST" = "FAILED" ] && die "gateway creation failed"
  sleep 5
done

GATEWAY_URL="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control get-gateway \
  --gateway-identifier "$GATEWAY_ID" --query 'gatewayUrl' --output text)"

# ---------------------------------------------------------------------------
# 3. web-search target (needs the boto3 'connector' shape -> use our venv).
# ---------------------------------------------------------------------------
EXISTING_TARGET="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier "$GATEWAY_ID" \
  --query "items[?name=='$TARGET_NAME'].targetId | [0]" --output text 2>/dev/null || true)"

if [ -n "$EXISTING_TARGET" ] && [ "$EXISTING_TARGET" != "None" ]; then
  say "Target $TARGET_NAME already exists: $EXISTING_TARGET"
  TARGET_ID="$EXISTING_TARGET"
else
  say "Creating web-search target $TARGET_NAME..."
  TARGET_ID="$(
    AWS_REGION="$REGION" AWS_PROFILE="${AWS_PROFILE:-}" \
    GATEWAY_ID="$GATEWAY_ID" TARGET_NAME="$TARGET_NAME" REGION="$REGION" \
    "$PY" - <<'PYEOF'
import os, boto3
gw = boto3.client("bedrock-agentcore-control", region_name=os.environ["REGION"])
r = gw.create_gateway_target(
    name=os.environ["TARGET_NAME"],
    description="Built-in Web Search connector",
    gatewayIdentifier=os.environ["GATEWAY_ID"],
    targetConfiguration={
        "mcp": {"connector": {
            "source": {"connectorId": "web-search"},
            "configurations": [{"name": "WebSearch", "parameterValues": {}}],
        }}
    },
    credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
)
print(r["targetId"])
PYEOF
  )"
  say "Target ID: $TARGET_ID"
fi

say "Waiting for target to become READY..."
for _ in $(seq 1 30); do
  ST="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control get-gateway-target \
        --gateway-identifier "$GATEWAY_ID" --target-id "$TARGET_ID" \
        --query 'status' --output text)"
  [ "$ST" = "READY" ] && break
  [ "$ST" = "FAILED" ] && die "target creation failed"
  sleep 5
done

# ---------------------------------------------------------------------------
# Done — report and offer to write .env
# ---------------------------------------------------------------------------
echo
say "✅ AgentCore Web Search is ready."
echo
echo "  Gateway URL:"
echo "    $GATEWAY_URL"
echo
echo "  Add this to your shell:"
echo "    export AGENTCORE_GATEWAY_URL=\"$GATEWAY_URL\""
echo

ENV_FILE="$HERE/.env"
if [ -f "$ENV_FILE" ]; then
  warn ".env already exists — not overwriting. Update AGENTCORE_GATEWAY_URL manually if needed."
else
  {
    echo "AGENTCORE_GATEWAY_URL=$GATEWAY_URL"
    [ -n "${AWS_PROFILE:-}" ] && echo "AWS_PROFILE=$AWS_PROFILE"
  } > "$ENV_FILE"
  say "Wrote $ENV_FILE"
fi

echo
say "Test it:  ./websearch \"latest AWS news\""
