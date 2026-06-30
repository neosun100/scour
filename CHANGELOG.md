# Changelog

All notable changes to **Scour** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-01

First public release. Scour turns the single managed **Amazon Bedrock AgentCore
Web Search** tool into a full toolkit for agents: a CLI, an MCP server with
concurrent fan-out + full-text research, and an API-key-protected REST service —
all authenticating to AWS with local IAM/SigV4 credentials (no API keys or tokens
to the gateway).

### Added

- **CLI (`scour`)** — one-shot web search via the AgentCore Gateway using local AWS
  credentials (SigV4/IAM). Formatted or `--json` output, `.env` loading, `--list-tools`.
- **MCP server (`scour-mcp`)** exposing four tools, runnable over **stdio** or
  **streamable-HTTP**:
  - `web_search` — single query (≤200 chars, 1–25 results).
  - `web_search_batch` — many queries concurrently, merged and **de-duplicated by URL**.
  - `fetch_articles` — concurrently fetch the **full text** of many URLs.
  - `research` — open-source-Firecrawl-style macro: search a topic → fetch the
    articles → one cited corpus to analyze.
- **REST bridge (`scour-http`)** — a plain-HTTP `GET/POST /search` endpoint for
  "custom search" plugins and any app, returning a broadly-compatible JSON shape.
- **Concurrency, sized to AgentCore quotas** — token-bucket rate limiter (default
  10 TPS) + concurrency semaphore (default 10); per-query failures are captured,
  not fatal.
- **Full-text fetching** — `httpx`-based concurrent fetch with main-text extraction
  (BeautifulSoup/lxml, optional `trafilatura` via the `[quality]` extra), robots.txt
  honored by default, rate-limited and size-bounded.
- **CloudFormation template** (`cfn/agentcore-websearch.yaml`) provisioning the
  AgentCore Gateway (AWS_IAM inbound) + the managed web-search connector target.
- **Claude Code / Codex skill** (`skills/scour/`) and MCP registration recipes.
- **Hosting recipe** — systemd + nginx + Cloudflare guide to run `scour-http` as a
  central, API-key-protected service (least-privilege IAM user).
- **47 offline unit tests** (rate limiter, batch aggregation/de-dup, fetch extraction,
  robots handling, research merge, API-key auth) — no AWS/network required.

### Security

- **API-key auth** for the HTTP services (`Authorization: Bearer <key>` or
  `X-API-Key: <key>`), constant-time comparison. **Secure-by-default**: refuses to
  bind a non-loopback host without a key unless `--insecure`.
- **Credentials never leave the host** — when hosted centrally, AWS access keys stay
  in the server's env file; downstream callers present only the Scour API key.
- **Least privilege** — documented IAM user scoped to `InvokeGateway` on a single
  gateway.

### Fixed

- Bypass a local/system HTTP proxy (e.g. macOS Clash `127.0.0.1:7890`) for the
  AgentCore gateway connection by default (`SCOUR_GATEWAY_USE_PROXY=1` to opt in),
  which otherwise broke the TLS handshake with an opaque error.
- Flatten `ExceptionGroup` (anyio TaskGroup) to the real root cause in error
  messages, so connection/transport failures are actionable.

### Notes

- **Region:** AgentCore Web Search is available only in `us-east-1`.
- **Acceptable use:** results are semantically-extracted snippets; retain and display
  source citations; do not bulk-extract or build a competing index.
- **Not for production** as-is — this is a sample; review least-privilege, monitoring,
  HA, and rate limiting before real-world use.

[1.0.0]: https://github.com/neosun100/scour/releases/tag/v1.0.0
