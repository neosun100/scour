#!/usr/bin/env python3
"""
scour — search the web via an AWS Bedrock AgentCore Gateway from the
CLI, using your local AWS credentials (SigV4 / IAM). No API keys or bearer tokens.

A thin client over AWS's `mcp-proxy-for-aws` library: `aws_iam_streamablehttp_client`
opens a SigV4-signed Streamable-HTTP MCP connection and the `mcp` SDK's `ClientSession`
drives the protocol. All signing/transport live in those libraries (see core.py).

Usage:
  export AGENTCORE_GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
  export AWS_PROFILE=your-profile            # optional; default credential chain otherwise
  scour "your search query"
  scour "latest python release" --max-results 5
  scour "aws news" --json               # raw tool result
  scour --list-tools                     # tools/list

Config resolution (highest first):
  --gateway-url flag  >  AGENTCORE_GATEWAY_URL env   (required — no built-in default)
  --region flag       >  region parsed from gateway URL  >  AWS_REGION  >  us-east-1
  --profile flag      >  AWS_PROFILE env  >  default credential chain
"""
import argparse
import asyncio
import json
import sys

from . import core


def print_results(payload):
    results = payload.get("results", [])
    if not results:
        print("No results.")
        return
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(untitled)"
        url = r.get("url", "")
        date = r.get("publishedDate", "")
        text = (r.get("text") or "").strip().replace("\n", " ")
        if len(text) > 280:
            text = text[:277] + "..."
        head = f"{i}. {title}"
        if date:
            head += f"  ({date})"
        print(head)
        if url:
            print(f"   {url}")
        if text:
            print(f"   {text}")
        print()


def main():
    core.load_dotenv()
    import os

    p = argparse.ArgumentParser(
        prog="scour",
        description="Search the web via AWS Bedrock AgentCore Web Search (IAM/SigV4), "
                    "using the mcp-proxy-for-aws library.",
    )
    p.add_argument("query", nargs="?", help="search query (<=200 chars)")
    p.add_argument("-n", "--max-results", type=int, default=10,
                   help="max results 1-25 (default 10)")
    p.add_argument("--json", action="store_true", help="print raw tool result JSON")
    p.add_argument("--list-tools", action="store_true",
                   help="list gateway tools and exit")
    p.add_argument("--gateway-url",
                   default=os.environ.get("AGENTCORE_GATEWAY_URL"),
                   help="AgentCore Gateway MCP URL (or set AGENTCORE_GATEWAY_URL)")
    p.add_argument("--region",
                   default=os.environ.get("AWS_REGION")
                   or os.environ.get("AWS_DEFAULT_REGION"))
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    args = p.parse_args()

    if not args.gateway_url:
        p.error("no gateway URL: set AGENTCORE_GATEWAY_URL or pass --gateway-url "
                "(e.g. https://<gateway-id>.gateway.bedrock-agentcore."
                "us-east-1.amazonaws.com/mcp)")
    if args.query and len(args.query) > core.MAX_QUERY_LEN:
        p.error(f"query must be <= {core.MAX_QUERY_LEN} characters")
    if not args.list_tools and not args.query:
        p.error("a query is required (or use --list-tools)")
    if not core.MIN_RESULTS <= args.max_results <= core.MAX_RESULTS:
        p.error(f"--max-results must be between {core.MIN_RESULTS} and "
                f"{core.MAX_RESULTS}")

    try:
        url, region, profile = core.resolve_config(
            args.gateway_url, args.region, args.profile
        )

        if args.list_tools:
            tools = asyncio.run(core.list_tools(url, region, profile))
            print(json.dumps(
                {"tools": [
                    {"name": t.name,
                     "description": getattr(t, "description", None),
                     "inputSchema": getattr(t, "inputSchema", None)}
                    for t in tools
                ]},
                indent=2, default=str))
            return

        payload = asyncio.run(
            core.search_one(url, region, profile, args.query, args.max_results)
        )
    except core.WebSearchError as e:
        sys.exit(f"ERROR: {e}")
    except Exception as e:  # surface signing/transport/connection failures cleanly
        sys.exit(f"ERROR: {core.format_error(e)}")

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return
    print_results(payload)


if __name__ == "__main__":
    main()
