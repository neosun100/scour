# AgentCore Web Search CLI (IAM / SigV4)

Call the **AWS Bedrock AgentCore Web Search** tool from the command line using your
**local AWS credentials** — no API keys, no bearer tokens. Includes a from-scratch
setup script and a [Claude Code](https://docs.claude.com/claude-code) skill.

AgentCore Web Search is a fully managed, MCP-compliant web search tool. Queries are
served entirely within AWS (zero data egress), and results come back with titles,
URLs, snippets, and publication dates. Pricing is ~$7 per 1,000 queries.

> **Region:** Web Search is currently available only in `us-east-1`.

## How it works

```
your machine                         AWS account (us-east-1)
┌──────────────┐   SigV4 (IAM)   ┌─────────────────────────┐   assume role   ┌──────────────┐
│ websearch CLI│ ───────────────▶│ AgentCore Gateway (MCP) │ ───────────────▶│ web-search    │
│ (this repo)  │  tools/call     │ authorizer: AWS_IAM     │  InvokeWebSearch│ connector     │
└──────────────┘                 └─────────────────────────┘                 └──────────────┘
```

- **Inbound** (you → gateway): each HTTPS request is SigV4-signed as service
  `bedrock-agentcore`. The signing region is derived from the gateway hostname, so a
  mismatched `AWS_REGION` in your shell won't break it. Your IAM principal needs
  `bedrock-agentcore:InvokeGateway` on the gateway.
- **Outbound** (gateway → connector): the gateway assumes a service role that grants
  `bedrock-agentcore:InvokeWebSearch`.

## Prerequisites

- An AWS account and credentials (`aws configure` or SSO profile) with permission to
  create IAM roles and AgentCore gateways.
- **AWS CLI v2 >= 2.35.0** and `bash` — earlier CLI versions lack the gateway
  `connector` target shape (`setup.sh` checks and tells you to upgrade).
- **Python 3.9+** — only for running the search CLI (`agentcore_websearch.py`),
  which uses `boto3` to SigV4-sign requests. Not needed to create the infrastructure.
- Access to Amazon Bedrock AgentCore in `us-east-1`.

> **Layout:** all runnable files live in the self-contained bundle
> `skills/agentcore-websearch/`. The commands below `cd` into it first. (This is
> the same folder you copy into `~/.claude/skills/` to use it as a Claude Code
> skill — see [Claude Code skill](#claude-code-skill).)

```bash
cd skills/agentcore-websearch
```

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

> `boto3` here is only used by the search CLI to SigV4-sign requests; any recent
> version works. (Creating the infrastructure is done entirely by `setup.sh` via the
> AWS CLI — no Python or boto3 involved.)

## 3. Search

```bash
./websearch "latest AWS AgentCore announcement"
./websearch "newest python version" --max-results 5
./websearch "aws news" --json          # raw MCP result for parsing
./websearch --list-tools               # diagnostic
```

You can also call the Python client directly:

```bash
export AGENTCORE_GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
python agentcore_websearch.py "your query" --max-results 10
```

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
    ├── agentcore_websearch.py      # MCP client (SigV4-signs initialize + tools/call, auto-retries)
    ├── iam/*.template.json         # IAM trust/permission templates (placeholders filled by setup.sh)
    ├── requirements.txt            # boto3 (runtime, for the search CLI only)
    └── .env.example                # copy to .env and fill in
```

## Claude Code skill

This repo ships a skill at `skills/agentcore-websearch/SKILL.md`. To use it, copy the
folder into your Claude Code skills directory:

```bash
cp -r skills/agentcore-websearch ~/.claude/skills/
```

Because the folder is self-contained (scripts, Python client, IAM templates, and
`requirements.txt` all live inside it), that single `cp -r` is the entire install —
nothing else to clone or configure.

The skill manages the **full lifecycle** — checking status, creating the gateway
(`setup.sh`), searching, and tearing it down (`teardown.sh`). It asks for your
explicit confirmation before any setup or teardown, since those create or delete
billable AWS resources. Searching an already-configured gateway runs without
prompting.

Then ask Claude Code to "set up agentcore websearch", "search the web with
agentcore", or "tear down agentcore" — see the SKILL.md for details.

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
avoid any further charges (run from `skills/agentcore-websearch/`):

```bash
cd skills/agentcore-websearch
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
