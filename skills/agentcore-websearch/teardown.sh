#!/usr/bin/env bash
#
# teardown.sh — delete everything setup.sh created.
#
# Configuration (must match what setup.sh used):
#   AWS_PROFILE, REGION, GATEWAY_NAME, ROLE_NAME, TARGET_NAME
#
set -euo pipefail

REGION="${REGION:-us-east-1}"
GATEWAY_NAME="${GATEWAY_NAME:-WebSearchGateway}"
ROLE_NAME="${ROLE_NAME:-AgentCoreWebSearchGatewayRole}"
TARGET_NAME="${TARGET_NAME:-web-search-tool}"
POLICY_NAME="WebSearchGatewayInline"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }

AWS_ARGS=(--region "$REGION")
[ -n "${AWS_PROFILE:-}" ] && AWS_ARGS+=(--profile "$AWS_PROFILE")

GATEWAY_ID="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control list-gateways \
  --query "items[?name=='$GATEWAY_NAME'].gatewayId | [0]" --output text 2>/dev/null || true)"

if [ -n "$GATEWAY_ID" ] && [ "$GATEWAY_ID" != "None" ]; then
  TARGET_ID="$(aws "${AWS_ARGS[@]}" bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "$GATEWAY_ID" \
    --query "items[?name=='$TARGET_NAME'].targetId | [0]" --output text 2>/dev/null || true)"
  if [ -n "$TARGET_ID" ] && [ "$TARGET_ID" != "None" ]; then
    say "Deleting target $TARGET_ID..."
    aws "${AWS_ARGS[@]}" bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier "$GATEWAY_ID" --target-id "$TARGET_ID" >/dev/null || warn "target delete failed"
    sleep 5
  fi
  say "Deleting gateway $GATEWAY_ID..."
  aws "${AWS_ARGS[@]}" bedrock-agentcore-control delete-gateway \
    --gateway-identifier "$GATEWAY_ID" >/dev/null || warn "gateway delete failed"
else
  warn "No gateway named $GATEWAY_NAME found."
fi

if aws "${AWS_ARGS[@]}" iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  say "Deleting role policy and role $ROLE_NAME..."
  aws "${AWS_ARGS[@]}" iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" >/dev/null 2>&1 || true
  aws "${AWS_ARGS[@]}" iam delete-role --role-name "$ROLE_NAME" >/dev/null || warn "role delete failed"
else
  warn "No role named $ROLE_NAME found."
fi

say "Teardown complete."
