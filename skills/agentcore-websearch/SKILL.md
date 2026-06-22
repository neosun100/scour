---
name: agentcore-websearch
description: Web search via AWS Bedrock AgentCore Web Search, called with local AWS/IAM credentials (SigV4) — no API keys or bearer tokens. Use when the user asks to search the web, look up current events, find recent/online information, or research topics needing up-to-date web results, AND prefers using their own AWS AgentCore gateway. Triggers - 'agentcore search', 'search the web with agentcore', 'web search', 'look up', 'find online', 'what is the latest', 'current news about', 'research'.
---

# Web Search via AWS Bedrock AgentCore

Search the web using the **AgentCore Web Search** tool through a private MCP gateway
in the user's AWS account. Authentication is **local AWS credentials (SigV4/IAM)** —
there are no API keys or tokens to manage. Results are grounded, cited, and current.

## Prerequisites

- The `agentcore-websearch` repo is checked out locally (contains the `websearch`
  CLI). Default location assumed: `$AGENTCORE_WEBSEARCH_DIR`, else `~/agentcore-websearch`.
- `AGENTCORE_GATEWAY_URL` is set in the environment or in the repo's `.env`
  (produced by the repo's `setup.sh`).
- AWS credentials are available (an `AWS_PROFILE` or the default credential chain).

## Usage

Run the CLI wrapper from the repo (resolve the directory first):

```bash
DIR="${AGENTCORE_WEBSEARCH_DIR:-$HOME/agentcore-websearch}"
"$DIR/websearch" "<search query>"
```

Options:
- `--max-results N` — number of results, 1–25 (default 10)
- `--json` — raw MCP result JSON (use when you need structured `results[]`)
- `--list-tools` — show the gateway's tools (diagnostic)

If `AGENTCORE_GATEWAY_URL` is not already exported, the wrapper reads it from the
repo's `.env`. If neither is set, run the repo's `setup.sh` first to create the
gateway, or export the URL manually.

## Workflow

1. Formulate a focused query (**must be ≤ 200 characters**).
2. Run the wrapper with an appropriate `--max-results`.
3. Read the printed results — each has a title, URL, publication date, and snippet.
4. **Always cite sources** (title + URL) in your answer — this is an AWS acceptable-use
   requirement for AgentCore Web Search.
5. For programmatic post-processing, add `--json` and parse
   `content[0].text` → JSON → `results[]` (`text`, `url`, `title`, `publishedDate`).

## Examples

```bash
DIR="${AGENTCORE_WEBSEARCH_DIR:-$HOME/agentcore-websearch}"
"$DIR/websearch" "latest TypeScript release"
"$DIR/websearch" "AWS re:Invent 2026 keynotes" --max-results 15 --json
```

## Notes & limits

- **Region:** the gateway lives in `us-east-1`; the CLI signs for the host region
  automatically, so a different `AWS_REGION` in the shell is fine.
- **Cost:** ~$7 per 1,000 queries (each run = one query).
- **Auto-retry:** the CLI re-signs and retries transient `401/403/429/5xx` responses
  up to 5 times with backoff. If an error still surfaces, it's a real failure:
  - `401 Authentication error` → AWS credentials missing/expired (refresh SSO/profile).
  - `403 Insufficient permissions` → caller lacks `bedrock-agentcore:InvokeGateway`.
  - `Execution role is not authorized for connector` → gateway service-role policy
    issue (infra, not the caller).
  - `AGENTCORE_GATEWAY_URL is not set` → set it or run `setup.sh`.
