#!/usr/bin/env python3
"""
agentcore-websearch — search the web via an AWS Bedrock AgentCore Gateway from the
CLI, using your local AWS credentials (SigV4 / IAM). No API keys or bearer tokens.

All the hard parts — SigV4 signing, the MCP `initialize`/`tools/call` handshake,
retries, and Streamable-HTTP transport — are handled by AWS's `mcp-proxy-for-aws`
(https://pypi.org/project/mcp-proxy-for-aws/), which this tool runs as a local
stdio MCP server and talks to with a `fastmcp` client. That keeps this file a thin
wrapper instead of a hand-rolled signing/transport stack.

Usage:
  export AGENTCORE_GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
  export AWS_PROFILE=your-profile            # optional; default credential chain otherwise
  ./agentcore_websearch.py "your search query"
  ./agentcore_websearch.py "latest python release" --max-results 5
  ./agentcore_websearch.py "aws news" --json          # raw tool result
  ./agentcore_websearch.py --list-tools               # tools/list

Config resolution (highest first):
  --gateway-url flag  >  AGENTCORE_GATEWAY_URL env   (required — no built-in default)
  --region flag       >  region parsed from gateway URL  >  AWS_REGION  >  us-east-1
  --profile flag      >  AWS_PROFILE env  >  default credential chain
  --proxy-cmd flag    >  AGENTCORE_PROXY_CMD env  >  "uvx mcp-proxy-for-aws"
"""
import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import sys
import urllib.parse

try:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
except ImportError:
    sys.exit(
        "ERROR: fastmcp is required.  pip install -r requirements.txt "
        "(or: pip install mcp-proxy-for-aws fastmcp)"
    )

TOOL_NAME = "WebSearch"
DEFAULT_PROXY_CMD = "uvx mcp-proxy-for-aws"

# The gateway endpoint must be an HTTPS AgentCore Gateway host. We validate it before
# launching the proxy so a malformed or unexpected value fails fast with a clear
# message (and can't point the proxy at an unintended host / non-https scheme).
GATEWAY_HOST_RE = re.compile(
    r"^[a-z0-9-]+\.gateway\.bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com$"
)


class WebSearchError(RuntimeError):
    pass


def validate_url(url):
    """Reject anything that is not an HTTPS AgentCore Gateway URL."""
    if not url:
        raise WebSearchError("gateway URL is empty")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise WebSearchError(
            f"gateway URL must use https, got '{parsed.scheme or url}://...'"
        )
    if not parsed.hostname or not GATEWAY_HOST_RE.match(parsed.hostname):
        raise WebSearchError(
            "gateway URL host is not an AgentCore Gateway endpoint "
            "(expected <id>.gateway.bedrock-agentcore.<region>.amazonaws.com): "
            f"{parsed.hostname or url}"
        )
    return url


def region_from_url(url):
    """Extract the AWS region embedded in a gateway host, or None."""
    m = re.search(r"gateway\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com", url)
    return m.group(1) if m else None


def build_transport(url, region, profile, proxy_cmd):
    """Build a fastmcp StdioTransport that runs mcp-proxy-for-aws for our gateway."""
    parts = shlex.split(proxy_cmd)
    if not shutil.which(parts[0]):
        raise WebSearchError(
            f"proxy command '{parts[0]}' not found. Install uv (https://docs.astral.sh/uv/) "
            "so `uvx mcp-proxy-for-aws` works, or set AGENTCORE_PROXY_CMD / --proxy-cmd "
            "to a path that runs mcp-proxy-for-aws."
        )
    command, *prefix_args = parts
    args = [*prefix_args, url, "--region", region]
    if profile:
        args += ["--profile", profile]
    env = dict(os.environ)
    if profile:
        env["AWS_PROFILE"] = profile
    return StdioTransport(command=command, args=args, env=env)


async def _list_tools(transport):
    async with Client(transport) as client:
        return await client.list_tools()


async def _call_search(transport, tool_name, query, max_results):
    args = {"query": query}
    if max_results is not None:
        args["maxResults"] = max_results
    async with Client(transport) as client:
        if tool_name is None:
            tools = await client.list_tools()
            tool_name = resolve_tool_name([t.name for t in tools])
        return await client.call_tool(tool_name, args)


def resolve_tool_name(names):
    """The gateway namespaces tools as `<target>___WebSearch`; find it."""
    for n in names:
        if n == TOOL_NAME or n.endswith("___" + TOOL_NAME):
            return n
    for n in names:
        if TOOL_NAME.lower() in n.lower():
            return n
    if names:
        return names[0]
    raise WebSearchError("gateway exposes no tools")


def result_payload(call_result):
    """Pull the structured JSON payload out of an MCP tool result."""
    # fastmcp exposes structured data when available, else text content blocks.
    data = getattr(call_result, "data", None)
    if isinstance(data, dict) and "results" in data:
        return data
    for block in getattr(call_result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict) and "results" in parsed:
                return parsed
    return {}


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
    p = argparse.ArgumentParser(
        prog="agentcore-websearch",
        description="Search the web via AWS Bedrock AgentCore Web Search (IAM/SigV4), "
                    "using mcp-proxy-for-aws under the hood.",
    )
    p.add_argument("query", nargs="?", help="search query (<=200 chars)")
    p.add_argument("-n", "--max-results", type=int, default=10,
                   help="max results 1-25 (default 10)")
    p.add_argument("--json", action="store_true", help="print raw tool result JSON")
    p.add_argument("--list-tools", action="store_true", help="list gateway tools and exit")
    p.add_argument("--gateway-url",
                   default=os.environ.get("AGENTCORE_GATEWAY_URL"),
                   help="AgentCore Gateway MCP URL (or set AGENTCORE_GATEWAY_URL)")
    p.add_argument("--region",
                   default=os.environ.get("AWS_REGION")
                   or os.environ.get("AWS_DEFAULT_REGION"))
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument("--proxy-cmd",
                   default=os.environ.get("AGENTCORE_PROXY_CMD", DEFAULT_PROXY_CMD),
                   help="command that runs mcp-proxy-for-aws "
                        f"(default: {DEFAULT_PROXY_CMD!r})")
    args = p.parse_args()

    if not args.gateway_url:
        p.error("no gateway URL: set AGENTCORE_GATEWAY_URL or pass --gateway-url "
                "(e.g. https://<gateway-id>.gateway.bedrock-agentcore."
                "us-east-1.amazonaws.com/mcp)")
    if args.query and len(args.query) > 200:
        p.error("query must be <= 200 characters")
    if not args.list_tools and not args.query:
        p.error("a query is required (or use --list-tools)")
    if not 1 <= args.max_results <= 25:
        p.error("--max-results must be between 1 and 25")

    try:
        url = validate_url(args.gateway_url)
        region = region_from_url(url) or args.region or "us-east-1"
        transport = build_transport(url, region, args.profile, args.proxy_cmd)

        if args.list_tools:
            tools = asyncio.run(_list_tools(transport))
            print(json.dumps(
                {"tools": [
                    {"name": t.name,
                     "description": getattr(t, "description", None),
                     "inputSchema": getattr(t, "inputSchema", None)}
                    for t in tools
                ]},
                indent=2, default=str))
            return

        result = asyncio.run(_call_search(transport, None, args.query, args.max_results))
    except WebSearchError as e:
        sys.exit(f"ERROR: {e}")
    except Exception as e:  # surface proxy/transport failures cleanly
        sys.exit(f"ERROR: {type(e).__name__}: {e}")

    payload = result_payload(result)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return
    print_results(payload)


if __name__ == "__main__":
    main()
