# AgentCore Web Search CLI (IAM / SigV4)

This project **creates an Amazon Bedrock AgentCore Gateway and connects the managed
Web Search tool to it**, then lets you call that tool from the command line using your
**local AWS credentials** — no API keys, no bearer tokens. It ships a from-scratch
setup script (gateway + IAM role + web-search target) and a
[Claude Code](https://docs.claude.com/claude-code) skill that drives the whole
lifecycle.

Because the gateway speaks the **Model Context Protocol (MCP)** and authenticates
callers with **IAM (SigV4)**, it is a good fit for grounding IAM-authenticated,
Bedrock-hosted agents in live web results — for example:

- **Claude Code on Bedrock** — give the coding agent current web knowledge.
- **Codex on Bedrock** — same, for OpenAI-style coding agents running against Bedrock.
- **Cowork (3P) on Bedrock** — let third-party Cowork agents search the web through
  your own gateway, with all queries staying inside your AWS account.

AgentCore Web Search itself is a fully managed, MCP-compliant web search tool. Queries
are served entirely within AWS (zero data egress), and results come back with titles,
URLs, snippets, and publication dates. Pricing is ~$7 per 1,000 queries.

> **Region:** Web Search is currently available only in `us-east-1`.

> [!WARNING]
> **Not for production use.** This repository is a sample / demonstration intended
> for learning and experimentation. It has not been hardened for production
> workloads — it omits production concerns such as least-privilege role scoping,
> monitoring/alarming, rate limiting, multi-region/HA, automated credential
> rotation, and comprehensive error handling. Review, adapt, and test it against
> your own security and operational requirements before any real-world use. It is
> provided "as is" without warranty (see [LICENSE](LICENSE)).

## How it works

```
your machine                                          AWS account (us-east-1)
┌────────────────────────────────┐   MCP over HTTPS    ┌──────────────────────┐   assume role    ┌──────────────┐
│ websearch CLI (this repo)       │   (JSON-RPC 2.0:    │  AgentCore Gateway   │ ───────────────▶ │ web-search   │
│  agentcore_websearch.py         │    initialize,      │  MCP endpoint  /mcp  │  InvokeWebSearch │ connector    │
│  • mcp_proxy_for_aws library    │ ──────────────────▶ │  authorizer: AWS_IAM │                  │ (managed     │
│    (SigV4 streamable-http)      │    tools/list,      │                      │                  │  web index)  │
│  • mcp SDK ClientSession        │    tools/call)      │                      │                  │              │
└────────────────────────────────┘   SigV4-signed      └──────────────────────┘                  └──────────────┘
```

The CLI is a thin client built on AWS's
[`mcp-proxy-for-aws`](https://pypi.org/project/mcp-proxy-for-aws/) **library**: it
calls `aws_iam_streamablehttp_client(...)` to open a **SigV4-signed, Streamable-HTTP
MCP connection** to the gateway's `/mcp` endpoint, and drives the protocol
(`initialize`, `tools/list`, `tools/call`) with the official `mcp` SDK's
`ClientSession`. Credential resolution, region signing, and transport all live in
those libraries — the connection runs **in-process**, with no subprocess to spawn and
no hand-rolled signing code.

> A Bedrock-hosted agent (Claude Code / Codex / Cowork on Bedrock) reaches the same
> gateway by registering `mcp-proxy-for-aws` as a stdio MCP **server** — see
> [Connect a Bedrock-hosted agent](#connect-a-bedrock-hosted-agent-claude-code--codex--cowork-on-bedrock).
> This CLI uses the same package as a library instead.

- **Inbound** (CLI → gateway): each MCP HTTPS request is SigV4-signed as service
  `bedrock-agentcore`. The signing region is derived from the gateway hostname, so a
  mismatched `AWS_REGION` in your shell won't break it. Your IAM principal needs
  `bedrock-agentcore:InvokeGateway` on the gateway.
- **Outbound** (gateway → connector): the gateway assumes a service role that grants
  `bedrock-agentcore:InvokeWebSearch`, then calls the managed web-search connector
  entirely within AWS.

## Prerequisites

- An AWS account and credentials (`aws configure` or SSO profile) with permission to
  create IAM roles and AgentCore gateways.
- **AWS CLI v2 >= 2.35.0** and `bash` — earlier CLI versions lack the gateway
  `connector` target shape (`setup.sh` checks and tells you to upgrade).
- **Python 3.9+** — only for running the search CLI. Its one dependency is
  `mcp-proxy-for-aws` (which brings `boto3`, `botocore`, and the `mcp` SDK). Not
  needed to create the infrastructure.
- Access to Amazon Bedrock AgentCore in `us-east-1`.
- `git` to clone this repository.

## Get the code

Clone the repository, then move into the runnable bundle. **All runtime files live in
`skills/agentcore-websearch/`** (the same self-contained folder you can later import
as a Claude Code skill — see [Use it as a Claude Code skill](#use-it-as-a-claude-code-skill)).

```bash
git clone https://github.com/aws-samples/agentcore-websearch.git
cd agentcore-websearch/skills/agentcore-websearch
```

> Every command in the steps below is run **from `skills/agentcore-websearch/`**.

## Two ways to use it

1. **Run it locally** — follow steps 1–3 below to create the gateway and search from
   your terminal with the `websearch` CLI.
2. **Use it as a Claude Code skill** — import the bundle so Claude Code can run the
   whole lifecycle for you. See [Use it as a Claude Code skill](#use-it-as-a-claude-code-skill).

Both paths share the same setup (steps 1–2); the skill just lets Claude drive it.

## 1. Create the infrastructure

The setup script creates the IAM service role, the gateway (with `AWS_IAM` inbound
auth), and the web-search target — then prints the gateway URL.

```bash
# uses your current AWS credentials / AWS_PROFILE
AWS_PROFILE=your-profile ./setup.sh
```

Useful overrides (all optional):

```bash
GATEWAY_NAME=MyWebSearchGateway \
ROLE_NAME=MyWebSearchGatewayRole \
REGION=us-east-1 \
AWS_PROFILE=your-profile ./setup.sh
```

At the end it prints an `export AGENTCORE_GATEWAY_URL=...` line and writes a local
`.env` (in the bundle folder).

## 2. Configure the CLI

`setup.sh` already writes `.env`; if you need to do it manually, copy the example and
fill in the gateway URL from step 1:

```bash
cp .env.example .env
# edit .env -> AGENTCORE_GATEWAY_URL=...   (and optionally AWS_PROFILE=...)
```

Install the Python dependency (a virtualenv is recommended):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

> The only dependency is [`mcp-proxy-for-aws`](https://pypi.org/project/mcp-proxy-for-aws/)
> (it brings `boto3`, `botocore`, and the `mcp` SDK). The CLI imports its
> `aws_iam_streamablehttp_client` to do the SigV4 signing and MCP transport in-process.
> Creating the infrastructure (`setup.sh`) uses the AWS CLI only — no Python involved.

## 3. Trigger and test the search locally

From `skills/agentcore-websearch/`, run the `websearch` wrapper. It loads
`AGENTCORE_GATEWAY_URL` (and optionally `AWS_PROFILE`) from the `.env` that `setup.sh`
wrote, opens a SigV4-signed MCP connection with your local AWS credentials, and prints ranked results.

**Smoke test** — confirm the gateway and tool are reachable:

```bash
./websearch --list-tools
```

Expected (the tool name is namespaced by the target):

```json
{
  "tools": [
    { "name": "web-search-tool___WebSearch",
      "inputSchema": { "type": "object",
        "properties": { "query": {"type": "string"},
                        "maxResults": {"type": "integer"} },
        "required": ["query"] } }
  ]
}
```

**Run searches:**

```bash
./websearch "latest AWS AgentCore announcement"
./websearch "newest python version" --max-results 5
./websearch "aws news" --json          # raw MCP result for parsing
```

A successful search prints numbered results — each with a title, URL, publication
date, and snippet. For example:

```
1. Introducing Web Search on Amazon Bedrock AgentCore  (2026-06-18)
   https://aws.amazon.com/blogs/machine-learning/introducing-web-search-on-amazon-bedrock-agentcore/
   Web Search on Amazon Bedrock AgentCore, now generally available, ...
```

**Direct Python invocation** (without the wrapper / `.env`):

```bash
. .venv/bin/activate
export AGENTCORE_GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
python agentcore_websearch.py "your query" --max-results 10
```

**If a search fails**, the error usually tells you which credential is missing:

| Message | Cause | Fix |
|---|---|---|
| `AGENTCORE_GATEWAY_URL is not set` | no `.env` / env var | run step 1, or `cp .env.example .env` and fill it in |
| `401 Authentication error` | AWS credentials missing/expired | refresh your `AWS_PROFILE` / SSO login |
| `403 Insufficient permissions` | caller lacks `bedrock-agentcore:InvokeGateway` | grant it (admin creds already have it) |
| `Execution role is not authorized for connector` | gateway service-role policy | re-run `./setup.sh` to repair the role |

The CLI auto-retries transient `401/403/429/5xx` responses up to 5 times before
surfacing an error.

### Configuration reference

| Variable | Required | Purpose |
|---|---|---|
| `AGENTCORE_GATEWAY_URL` | yes | Gateway MCP endpoint (from `setup.sh`) |
| `AWS_PROFILE` | no | AWS profile; otherwise the default credential chain is used |
| `AWS_REGION` | no | Overridden by the region in the gateway URL anyway |

CLI flags `--gateway-url`, `--profile`, `--region` override the environment.

## Layout

The repo root holds the README and open-source governance files. Everything runnable
lives in the self-contained skill bundle, so copying that one folder is all you need:

```
.                                   # README, LICENSE, NOTICE, CONTRIBUTING, CODE_OF_CONDUCT
└── skills/agentcore-websearch/     # ← self-contained bundle (also the Claude Code skill)
    ├── SKILL.md                    # Claude Code skill definition (full lifecycle)
    ├── setup.sh                    # creates IAM role + gateway + web-search target
    ├── teardown.sh                 # deletes everything setup.sh created
    ├── websearch                   # bash wrapper; loads .env, requires AGENTCORE_GATEWAY_URL
    ├── agentcore_websearch.py      # thin CLI: mcp-proxy-for-aws library + mcp SDK ClientSession
    ├── iam/*.template.json         # IAM trust/permission templates (placeholders filled by setup.sh)
    ├── requirements.txt            # mcp-proxy-for-aws (brings boto3/botocore/mcp SDK)
    └── .env.example                # copy to .env and fill in
```

## Use it as a Claude Code skill

Instead of running the CLI yourself, you can let [Claude Code](https://docs.claude.com/claude-code)
drive the whole lifecycle. Install the skill by copying the bundle into your Claude
Code skills directory:

```bash
# from the cloned repo root
cp -r skills/agentcore-websearch ~/.claude/skills/
```

Because the folder is self-contained (scripts, Python client, IAM templates, and
`requirements.txt` all live inside it), that single `cp -r` is the entire install —
no other files from the repo are needed. (You still clone the repo first to get the
folder to copy.)

Once installed, the skill manages the **full lifecycle** — checking status, creating
the gateway (`setup.sh`), searching, and tearing it down (`teardown.sh`). It asks for
your explicit confirmation before any setup or teardown, since those create or delete
billable AWS resources; searching an already-configured gateway runs without prompting.

Then ask Claude Code to:

- *"set up agentcore websearch"* — creates the gateway (after confirming)
- *"search the web with agentcore for …"* — runs a query
- *"tear down agentcore websearch"* — deletes the resources (after confirming)

See [`skills/agentcore-websearch/SKILL.md`](skills/agentcore-websearch/SKILL.md) for
the full skill instructions.

## Connect a Bedrock-hosted agent (Claude Code / Codex / Cowork on Bedrock)

You don't need this repo's CLI to use the gateway from an agent — any MCP client can
register AWS's `mcp-proxy-for-aws` as a **stdio MCP server** pointed at your gateway.
The proxy SigV4-signs every call with the agent host's IAM role, so an
IAM-authenticated, Bedrock-hosted agent gets web search with no extra credentials.

Example MCP server entry (the shape most clients accept; field names vary by client):

```json
{
  "mcpServers": {
    "agentcore-websearch": {
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws",
        "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "--region", "us-east-1"
      ]
    }
  }
}
```

- **Claude Code on Bedrock** — add it with
  `claude mcp add agentcore-websearch -- uvx mcp-proxy-for-aws <gateway-url> --region us-east-1`.
- **Codex on Bedrock** — add the same `command`/`args` under `mcp_servers` in your
  Codex config.
- **Cowork (3P) on Bedrock** — register the same stdio server; queries stay inside
  your AWS account via your own gateway.

The agent's IAM principal (or instance/role credentials) needs
`bedrock-agentcore:InvokeGateway` on the gateway, exactly like the CLI.

## ⚠️ Note on the InvokeWebSearch ARN

The AWS docs show `arn:aws:bedrock-agentcore::aws:tool/web-search.v1` (empty region),
which is **rejected** with "Execution role is not authorized for connector
web-search". The working ARN includes the region:

```
arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1
```

`setup.sh` and the IAM templates use the correct form.

## Cleanup

Delete all resources created by `setup.sh` (the gateway, target, and IAM role) to
avoid any further charges. Run from `skills/agentcore-websearch/`:

```bash
AWS_PROFILE=your-profile ./teardown.sh
```

Also remove local artifacts if you no longer need them:

```bash
rm -rf .venv .env
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for information on
reporting a potential security issue. Do not create a public GitHub issue for
security findings.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). This project
follows the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
