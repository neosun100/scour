# Scour

**Scour** — concurrent web research for agents, grounded in **Amazon Bedrock
AgentCore Web Search**. Scour provisions an **AgentCore Gateway** with the managed
**Web Search** tool and gives you a CLI + an MCP server that call it using your
**local AWS credentials (SigV4/IAM)** — no API keys or bearer tokens. On top of the
single managed search tool, Scour adds **concurrent fan-out** and **full-text
research** (search a topic → fetch the articles → one cited corpus to analyze), like
an open-source Firecrawl that runs on your own AWS.

The underlying Web Search is fully managed, MCP-compliant, served entirely within AWS
(zero data egress), and priced at ~$7 per 1,000 queries. Because the gateway speaks
MCP and authenticates callers with IAM, Scour is a good fit for grounding
IAM-authenticated, Bedrock-hosted agents (e.g. **Claude Code**, **Codex**, or
**Cowork** on Bedrock) in live web results.

> **Region:** Web Search is only available in `us-east-1`.

> [!WARNING]
> **Not for production.** This is a sample for learning/experimentation. It omits
> production concerns (least-privilege scoping, monitoring, rate limiting, HA,
> credential rotation). Review and adapt before any real-world use. Provided "as is"
> (see [LICENSE](LICENSE)).

## How it works

```
                              your AWS account (us-east-1)
 any MCP client ──SigV4 / MCP over HTTPS──▶ AgentCore Gateway ──▶ web-search connector
 (CLI, agent, …)        (AWS_IAM auth)        assumes IAM role      (managed web index)
```

- **Inbound** (client → gateway): each MCP request is SigV4-signed; the caller's IAM
  principal needs `bedrock-agentcore:InvokeGateway` on the gateway.
- **Outbound** (gateway → connector): the gateway assumes a service role granting
  `bedrock-agentcore:InvokeWebSearch`, entirely within AWS.

The gateway is a plain MCP server, so **any MCP client can call it**. This repo also
ships a small CLI and a Claude Code skill on top — see [Use it](#use-it) for the
options.

## Prerequisites

- AWS credentials (`aws configure` / `AWS_PROFILE`) able to create IAM roles and
  AgentCore gateways, with access to Bedrock AgentCore in `us-east-1`.
- **AWS CLI v2 ≥ 2.35.0** (older versions lack the gateway `connector` target shape).
- **Python 3.9+** (only for the CLI) and/or [`uv`](https://docs.astral.sh/uv/) (only
  for the direct-MCP option).

## Setup — deploy the gateway (CloudFormation)

This one-time step is required for every usage option below.
[`cfn/agentcore-websearch.yaml`](cfn/agentcore-websearch.yaml) defines the IAM service
role, the gateway (`AWS_IAM` inbound auth), and the web-search target.

```bash
aws cloudformation deploy \
  --region us-east-1 --stack-name agentcore-websearch \
  --template-file cfn/agentcore-websearch.yaml --capabilities CAPABILITY_IAM

# capture the gateway URL (used by every option below)
GATEWAY_URL=$(aws cloudformation describe-stacks --region us-east-1 \
  --stack-name agentcore-websearch \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue" --output text)
echo "$GATEWAY_URL"
```

## Use it

Pick the option that fits — all use the same gateway and the same IAM/SigV4 auth:

| Option | Best when… |
|---|---|
| **A. CLI** | Searching from a shell, script, or cron job; want formatted or `--json` output |
| **B. Any MCP client** | Wiring the tool into an MCP-aware app without installing this package |
| **C. Claude Code / Codex** | Letting a coding agent search for you |
| **D. Concurrent MCP server** | Giving *any* agent one MCP endpoint that fans out **many searches in parallel** (this repo's `scour-mcp`) |

### Option A — CLI

The CLI adds ergonomics over raw MCP: argument validation, `.env` loading, tidy
result formatting, and a packaged `scour` command.

```bash
# save the gateway URL where the CLI looks for it
printf 'AGENTCORE_GATEWAY_URL=%s\n' "$GATEWAY_URL" > .env

python -m venv .venv && . .venv/bin/activate
pip install .                       # installs the `scour` command

scour "latest AWS news"                       # basic search (default 10 results)
scour "newest python version" -n 5            # -n / --max-results (1–25)
scour "aws re:Invent 2026 dates" --json       # raw tool result JSON (pipe to jq)
scour --list-tools                            # list gateway tools and exit
scour --help                                  # full usage

# Override config without a .env / exported vars:
scour "ecs vs eks" \
  --gateway-url "$GATEWAY_URL" \
  --profile my-aws-profile \
  --region us-east-1
```

The CLI reads `AGENTCORE_GATEWAY_URL` (and optional `AWS_PROFILE`) from `.env` or the
environment. Its only dependency is
[`mcp-proxy-for-aws`](https://pypi.org/project/mcp-proxy-for-aws/), which handles the
SigV4 signing and MCP transport.

### Option B — Any MCP client (no CLI)

The gateway is a standard **streamable-HTTP MCP** endpoint. Auth is **AWS SigV4
(IAM)** on service `bedrock-agentcore`, which most MCP clients can't sign on their
own — so run AWS's [`mcp-proxy-for-aws`](https://pypi.org/project/mcp-proxy-for-aws/)
as a local stdio MCP server that signs requests with your AWS credentials and
forwards them to the gateway. No install of this package required.

Configure it as an MCP server (generic form; field names vary by client):

```jsonc
{
  "mcpServers": {
    "scour": {
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws",
        "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "--region", "us-east-1"
      ],
      "env": { "AWS_PROFILE": "your-profile" }   // omit to use the default credential chain
    }
  }
}
```

It exposes one tool, `WebSearch` (namespaced as `web-search-tool___WebSearch`), with
arguments `query` (≤ 200 chars) and optional `maxResults` (1–25). Inspect it by
running the proxy standalone:

```bash
uvx mcp-proxy-for-aws "$GATEWAY_URL" --region us-east-1
```

### Option C — Claude Code or Codex

Both agents support **two** ways to add the tool: install this repo's **skill** (a
folder with [`SKILL.md`](skills/scour/SKILL.md) that drives the CLI from
Option A), or register the gateway as an **MCP server** (the proxy from Option B). Use
whichever you prefer — the skill needs the CLI installed; the MCP server doesn't.

> The MCP commands below take the gateway URL from Setup. Substitute it for
> `$GATEWAY_URL` if your client doesn't expand environment variables.

**[Claude Code](https://docs.claude.com/claude-code)**

```bash
# As a skill (requires the CLI from Option A):
cp -r skills/scour ~/.claude/skills/

# …or as an MCP server (no CLI needed):
claude mcp add scour -- uvx mcp-proxy-for-aws "$GATEWAY_URL" --region us-east-1
```

Then ask Claude Code to "search the web with agentcore".

**[Codex](https://developers.openai.com/codex/)**

```bash
# As a skill (requires the CLI from Option A):
cp -r skills/scour ~/.codex/skills/
```

```toml
# …or as an MCP server in ~/.codex/config.toml (no CLI needed):
[mcp_servers.scour]
command = "uvx"
args = ["mcp-proxy-for-aws", "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp", "--region", "us-east-1"]
```

### Option D — Concurrent MCP server

This repo also ships an MCP **server**, `scour-mcp`, that wraps the
gateway and adds **concurrent** search **plus full-text research** — an
open-source-Firecrawl-style flow: pick a topic → search → fetch the articles →
get one structured corpus your agent can summarize. Point any MCP client at it once
and every agent gets it.

Why use it instead of Option B's raw proxy:
- **Concurrent fan-out.** `web_search_batch` issues many queries at once against
  AgentCore, rate-limited and bounded to stay inside the service quotas, then merges
  and **de-duplicates results by URL**. One question → many searches → one result set.
- **Full-text research.** `fetch_articles` / `research` concurrently fetch the *full
  text* of result pages (Web Search itself returns only snippets) and extract the main
  article text — politely (robots.txt-aware, rate-limited, bounded).
- **Central identity (optional).** Run it over streamable-HTTP and it holds the AWS
  identity and SigV4-signs upstream, so downstream agents need no AWS credentials of
  their own. Run it over stdio for a local agent using your own credentials.

It exposes four tools:

| Tool | Args | Returns |
|---|---|---|
| `web_search` | `query` (≤200 chars), `max_results` (1–25, default 10) | `{results:[{title,url,publishedDate,text}], total}` |
| `web_search_batch` | `queries[]`, `max_results`, `concurrency?` | merged, de-duped `{results, total, queryCount, errorCount, queries[]}` |
| `fetch_articles` | `urls[]`, `max_chars?`, `concurrency?` | `{results:[{url,finalUrl,status,title,text,chars,error}], total, errorCount}` |
| `research` | `topic`, `max_results`, `fetch_full?`, `max_chars?`, `concurrency?` | `{topic, sourceCount, fetched, errorCount, sources:[{url,title,publishedDate,snippet,text,fetchError}]}` |

> **Where the work happens.** `web_search` / `web_search_batch` run entirely inside
> AWS (snippets only). `fetch_articles` / `research` additionally fetch full pages
> **from their origin servers using the server host's own network** — that part is not
> AWS. The agent/LLM does the final summary/analysis over the returned `sources`.

Install and run (needs the gateway from Setup and `AGENTCORE_GATEWAY_URL` set):

```bash
pip install .                              # installs `scour-mcp`

scour-mcp                     # stdio (default) — for a local agent
scour-mcp --http              # streamable-HTTP on 127.0.0.1:8000/mcp
scour-mcp --http --host 0.0.0.0 --port 9000   # host it for many agents

# Throughput knobs (defaults sized to AgentCore quotas; raise via Service Quotas):
scour-mcp --rate 10 --concurrency 10

# Full-text fetch knobs (be polite to origin sites):
scour-mcp --fetch-concurrency 10 --fetch-rate 5 --max-chars 8000
# (robots.txt is honored by default; --no-robots disables it — use responsibly)
```

For higher-quality main-text extraction, install the optional extra (the fetcher
auto-uses it, falling back to bs4/lxml when absent):

```bash
pip install ".[quality]"        # adds trafilatura
```

Register the **stdio** server with a coding agent (it signs with your AWS creds):

```bash
# Claude Code
claude mcp add scour-concurrent -- scour-mcp
```

```toml
# Codex — ~/.codex/config.toml
[mcp_servers.scour_concurrent]
command = "scour-mcp"
env = { AGENTCORE_GATEWAY_URL = "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp", AWS_PROFILE = "your-profile" }
```

Then ask the agent to "search the web" (one query) or "search these N topics in
parallel" (batch). Results carry `url`/`title`/`publishedDate` — **always cited**.

#### Concurrency, quotas & limits (read before scaling up)

The batch tool is bounded by **AgentCore service quotas** (all in `us-east-1`,
all increasable via the Service Quotas console — see [AGENTS.md](AGENTS.md)):

| Quota | Default | The server's guard |
|---|---|---|
| Rate of Web Search Tool requests | **10 / sec** | `--rate` (token-bucket limiter, default 10) |
| Gateway concurrent connections | **1000** | `--concurrency` (semaphore, default 10) |
| `maxResults` per query | **1–25** | validated per query |
| `query` length | **≤ 200 chars** | validated per query |

So a 1,000-query batch is *possible* but, at the default 10 TPS, takes ~100s — raise
the **Rate of Web Search Tool requests** quota (and `--rate`) for faster fan-out.

> [!IMPORTANT]
> **AgentCore acceptable use.** Web Search returns *semantically-extracted snippets*,
> not full pages, and you must **retain and display the source citations** (url/title)
> in any output. You may **not** use it to extract/store/reproduce results in **bulk**
> or to build a competing index. Use concurrency to *answer questions faster*, not to
> harvest content at scale.
>
> **Full-text fetching (`fetch_articles` / `research`).** Fetching a public URL from
> its origin is ordinary web access — governed by that **site's** robots.txt, terms,
> and copyright (not AgentCore's policy). The fetcher honors robots.txt by default,
> rate-limits, and bounds each download. Fetch only pages you're permitted to read,
> keep citations, and don't redistribute full text in bulk.

## Clean up

Delete the stack to remove the gateway, target, and IAM role:

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name agentcore-websearch
aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name agentcore-websearch
```

## Reference

- **[AGENTS.md](AGENTS.md)** — full setup/teardown guide, confirmation policy, and how
  auth works.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for how to report a
security issue. Do not open public issues for security findings.

## License

MIT-0. See [LICENSE](LICENSE).
