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
# Requirements: AWS CLI v2 >= 2.35.0 (older versions lack the gateway "connector"
# target shape needed for the Web Search tool — the script checks and tells you).
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

command -v aws >/dev/null || die "aws CLI not found (install AWS CLI v2 >= 2.35.0)"

# The Web Search tool is configured as a gateway "connector" target. That shape
# was added to the AgentCore model in AWS CLI v2 2.35.0. Verify the installed CLI
# supports it rather than failing later with a confusing parameter error.
if ! aws bedrock-agentcore-control create-gateway-target help 2>/dev/null \
     | grep -q "connector"; then
  die "your AWS CLI does not support the 'connector' gateway target.
       Upgrade to AWS CLI v2 >= 2.35.0:  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
       (current: $(aws --version 2>&1))"
fi

AWS_ARGS=(--region "$REGION")
[ -n "${AWS_PROFILE:-}" ] && AWS_ARGS+=(--profile "$AWS_PROFILE")

say "Resolving account identity..."
ACCOUNT_ID="$(aws "${AWS_ARGS[@]}" sts get-caller-identity --query Account --output text)" \
  || die "could not get caller identity — check your AWS credentials"
say "Account: $ACCOUNT_ID   Region: $REGION   ($(aws --version 2>&1 | cut -d' ' -f1))"

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
# 3. web-search connector target (pure AWS CLI; requires CLI >= 2.35.0)
# ---------------------------------------------------------------------------
EXISTING_TARGET="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier "$GATEWAY_ID" \
  --query "items[?name=='$TARGET_NAME'].targetId | [0]" --output text 2>/dev/null || true)"

if [ -n "$EXISTING_TARGET" ] && [ "$EXISTING_TARGET" != "None" ]; then
  say "Target $TARGET_NAME already exists: $EXISTING_TARGET"
  TARGET_ID="$EXISTING_TARGET"
else
  say "Creating web-search target $TARGET_NAME..."
  TARGET_ID="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control create-gateway-target \
    --gateway-identifier "$GATEWAY_ID" \
    --name "$TARGET_NAME" \
    --description "Built-in Web Search connector" \
    --target-configuration '{"mcp":{"connector":{"source":{"connectorId":"web-search"},"configurations":[{"name":"WebSearch","parameterValues":{}}]}}}' \
    --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
    --query 'targetId' --output text)"
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
