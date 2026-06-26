#!/usr/bin/env python3
"""
scour-mcp — an MCP **server** that exposes AWS Bedrock AgentCore
Web Search to any MCP-compatible agent, with first-class **concurrent** search.

Why this exists: the AgentCore Gateway is itself an MCP server, but every caller must
SigV4-sign requests with AWS credentials (which most agents can't do natively) and can
only search one query at a time (<=25 results). This server wraps that for you:

  * **One place to plug in** — point any agent (Claude, Codex, Strands, LangGraph, ...)
    at this server over stdio or streamable-HTTP. It holds the AWS identity and signs
    upstream; agents need no AWS credentials of their own when you host it centrally.
  * **Concurrent fan-out** — `web_search_batch` runs many queries in parallel against
    AgentCore, rate-limited + bounded to stay within service quotas, then aggregates
    and de-duplicates by URL. Useful when one question needs many searches at once.

Run it:
  scour-mcp                       # stdio (default; for local agents)
  scour-mcp --http                # streamable-HTTP on 127.0.0.1:8000/mcp
  scour-mcp --http --host 0.0.0.0 --port 9000
  # tuning (defaults sized to AgentCore quotas — adjustable via Service Quotas):
  scour-mcp --rate 10 --concurrency 10

Config (same resolution as the CLI):
  --gateway-url > AGENTCORE_GATEWAY_URL (required) ; region parsed from URL ; --profile > AWS_PROFILE

Compliance: per AgentCore acceptable use, callers must retain/display the source
citations (url/title) returned with each result, and must not use the tool for bulk
extraction or to build a competing index. Results here always include those fields.
"""
import argparse
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import core
from . import fetch
from . import auth

# Runtime config, populated by main() before the server starts serving.
_CFG = {
    "url": None,
    "region": None,
    "profile": None,
    "rate": core.DEFAULT_RATE_PER_SEC,
    "concurrency": core.DEFAULT_CONCURRENCY,
    "fetch_concurrency": fetch.DEFAULT_FETCH_CONCURRENCY,
    "fetch_rate": fetch.DEFAULT_FETCH_RATE,
    "max_chars": fetch.DEFAULT_MAX_CHARS,
    "respect_robots": True,
}

INSTRUCTIONS = (
    "Web search + full-text research grounded in current results via AWS Bedrock "
    "AgentCore (an open-source-Firecrawl-style toolkit). Tools: `web_search` (one "
    "query), `web_search_batch` (many queries concurrently, de-duped), "
    "`fetch_articles` (concurrently fetch full text of given URLs), and `research` "
    "(pick a topic -> search -> fetch full text -> one structured corpus to analyze). "
    "Always cite the returned url/title. Do not bulk-extract or redistribute content."
)

mcp = FastMCP("scour", instructions=INSTRUCTIONS)


def _cfg():
    """Return (url, region, profile), resolving lazily on first use."""
    if not _CFG["url"]:
        _CFG["url"], _CFG["region"], _CFG["profile"] = core.resolve_config(
            _CFG.get("url"), _CFG.get("region"), _CFG.get("profile")
        )
    return _CFG["url"], _CFG["region"], _CFG["profile"]


@mcp.tool()
async def web_search(query: str, max_results: int = core.DEFAULT_RESULTS) -> dict:
    """Search the web for a single query via AgentCore Web Search.

    Args:
        query: Search query, 200 characters or fewer.
        max_results: How many results to return, 1-25 (default 10).

    Returns a dict: {"results": [{"title","url","publishedDate","text"}], "total": N}.
    `text` is a semantically-extracted snippet (not full page text). Cite url/title.
    """
    core.validate_query(query)
    core.validate_max_results(max_results)
    url, region, profile = _cfg()
    return await core.search_one(url, region, profile, query, max_results)


@mcp.tool()
async def web_search_batch(
    queries: list[str],
    max_results: int = core.DEFAULT_RESULTS,
    concurrency: int | None = None,
) -> dict:
    """Run MANY web searches CONCURRENTLY and return one de-duplicated result set.

    Issues each query in parallel against AgentCore (bounded by `concurrency` and an
    internal token-bucket rate limiter sized to the service quota), then merges and
    de-duplicates results by URL. Per-query failures are reported, not fatal.

    Args:
        queries: List of search queries (each <=200 chars). May be large (100s);
            throughput is capped by the configured rate limit / quota.
        max_results: Results per query, 1-25 (default 10).
        concurrency: Max in-flight searches (default: server's --concurrency).

    Returns: {
      "results": [...deduped merged results...], "total": N,
      "queryCount": M, "errorCount": K,
      "queries": [{"query","count","error"}, ...]  # per-query breakdown
    }
    Cite the returned url/title in any answer. Do not use for bulk extraction.
    """
    for q in queries:
        core.validate_query(q)
    core.validate_max_results(max_results)
    conc = concurrency or _CFG["concurrency"]
    url, region, profile = _cfg()
    return await core.search_batch(
        url, region, profile, queries,
        max_results=max_results,
        concurrency=conc,
        rate_per_sec=_CFG["rate"],
    )


@mcp.tool()
async def fetch_articles(
    urls: list[str],
    max_chars: int | None = None,
    concurrency: int | None = None,
) -> dict:
    """Concurrently fetch the FULL text of many web pages (from their origin servers).

    Unlike web_search (which returns short snippets), this retrieves and extracts the
    main article text of each URL. Polite by default: respects robots.txt, rate-limited,
    bounded, with per-URL error capture. Uses the server host's own network.

    Args:
        urls: List of http(s) URLs to fetch (may be large; throughput is rate-limited).
        max_chars: Truncate each article's extracted text to this many chars
            (default ~8000).
        concurrency: Max concurrent fetches (default: server's --fetch-concurrency).

    Returns: {"results": [{url, finalUrl, status, title, text, chars, error}],
              "total", "errorCount", "requested"}.
    Only fetch public pages you're permitted to read; cite sources; no bulk redistribution.
    """
    return await fetch.fetch_articles(
        urls,
        concurrency=concurrency or _CFG["fetch_concurrency"],
        rate_per_sec=_CFG["fetch_rate"],
        max_chars=max_chars if max_chars is not None else _CFG["max_chars"],
        respect_robots=_CFG["respect_robots"],
    )


@mcp.tool()
async def research(
    topic: str,
    max_results: int = core.DEFAULT_RESULTS,
    fetch_full: bool = True,
    max_chars: int | None = None,
    concurrency: int | None = None,
) -> dict:
    """Research a topic end-to-end (open-source-Firecrawl style): search -> fetch -> merge.

    1) Searches the topic via AgentCore Web Search (up to `max_results` sources).
    2) If `fetch_full`, concurrently fetches each source's full text from origin.
    3) Returns one structured corpus; each source keeps its search snippet, citation
       (url/title/publishedDate) and (when fetched) full `text`.

    Your agent/LLM then summarizes/analyzes the returned `sources`. This tool does not
    itself write the summary — it assembles grounded, cited material to reason over.

    Args:
        topic: Research topic / query (<=200 chars).
        max_results: How many sources to discover, 1-25 (default 10).
        fetch_full: Fetch each source's full text (default True). If False, returns
            search snippets only (no origin fetch).
        max_chars: Truncate each article's text to this many chars (default ~8000).
        concurrency: Max concurrent fetches (default: server's --fetch-concurrency).

    Returns: {"topic", "sourceCount", "fetched", "errorCount", "sources": [...]}.
    """
    core.validate_query(topic)
    core.validate_max_results(max_results)
    url, region, profile = _cfg()
    return await fetch.research(
        url, region, profile, topic,
        max_results=max_results,
        fetch_full=fetch_full,
        max_chars=max_chars if max_chars is not None else _CFG["max_chars"],
        fetch_concurrency=concurrency or _CFG["fetch_concurrency"],
        fetch_rate=_CFG["fetch_rate"],
        respect_robots=_CFG["respect_robots"],
    )


class _ApiKeyASGI:
    """ASGI middleware: require a valid API key on HTTP requests (OPTIONS exempt)."""

    def __init__(self, app, keys):
        self.app = app
        self.keys = keys

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        hdrs = {k.decode("latin1").lower(): v.decode("latin1")
                for k, v in scope.get("headers", [])}
        provided = auth.extract_key(lambda n: hdrs.get(n.lower()))
        if not auth.key_ok(provided, self.keys):
            body = b'{"error":"unauthorized: missing or invalid API key"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def main():
    core.load_dotenv()
    p = argparse.ArgumentParser(
        prog="scour-mcp",
        description="MCP server exposing AgentCore Web Search with concurrent fan-out.",
    )
    p.add_argument("--http", action="store_true",
                   help="serve over streamable-HTTP instead of stdio")
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"),
                   help="bind host for --http (default 127.0.0.1)")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("MCP_PORT", "8000")),
                   help="bind port for --http (default 8000)")
    p.add_argument("--gateway-url", default=os.environ.get("AGENTCORE_GATEWAY_URL"))
    p.add_argument("--region",
                   default=os.environ.get("AWS_REGION")
                   or os.environ.get("AWS_DEFAULT_REGION"))
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument("--rate", type=float, default=core.DEFAULT_RATE_PER_SEC,
                   help=f"max upstream searches/sec (default {core.DEFAULT_RATE_PER_SEC}; "
                        "0 disables; AgentCore default quota is 10 TPS)")
    p.add_argument("--concurrency", type=int, default=core.DEFAULT_CONCURRENCY,
                   help=f"max concurrent upstream searches (default "
                        f"{core.DEFAULT_CONCURRENCY})")
    p.add_argument("--fetch-concurrency", type=int,
                   default=fetch.DEFAULT_FETCH_CONCURRENCY,
                   help=f"max concurrent article fetches (default "
                        f"{fetch.DEFAULT_FETCH_CONCURRENCY})")
    p.add_argument("--fetch-rate", type=float, default=fetch.DEFAULT_FETCH_RATE,
                   help=f"max article fetches/sec (default {fetch.DEFAULT_FETCH_RATE}; "
                        "be polite to origin sites)")
    p.add_argument("--max-chars", type=int, default=fetch.DEFAULT_MAX_CHARS,
                   help=f"truncate each fetched article to N chars (default "
                        f"{fetch.DEFAULT_MAX_CHARS})")
    p.add_argument("--no-robots", action="store_true",
                   help="do NOT honor robots.txt when fetching (default: honor it)")
    p.add_argument("--api-key", action="append", default=None, metavar="KEY",
                   help="require this API key on --http requests (repeatable; or set "
                        "SCOUR_API_KEYS). Sent as 'Authorization: Bearer <key>'.")
    p.add_argument("--insecure", action="store_true",
                   help="allow --http on a non-loopback host with NO API key (unsafe).")
    p.add_argument("--gen-key", action="store_true",
                   help="print a fresh strong API key and exit.")
    args = p.parse_args()

    if args.gen_key:
        print(auth.generate_key())
        return

    # Validate config up front so misconfiguration fails before the server starts.
    try:
        url, region, profile = core.resolve_config(
            args.gateway_url, args.region, args.profile
        )
    except core.WebSearchError as e:
        p.error(str(e))
    if args.rate < 0:
        p.error("--rate must be >= 0")
    if args.concurrency < 1:
        p.error("--concurrency must be >= 1")
    if args.fetch_concurrency < 1:
        p.error("--fetch-concurrency must be >= 1")

    _CFG.update(url=url, region=region, profile=profile,
                rate=args.rate, concurrency=args.concurrency,
                fetch_concurrency=args.fetch_concurrency,
                fetch_rate=args.fetch_rate,
                max_chars=args.max_chars,
                respect_robots=not args.no_robots)

    if args.http:
        keys = auth.resolve_keys(args.api_key)
        auth.require_or_refuse(args.host, keys, args.insecure, p.error)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if keys:
            # Gate the streamable-HTTP ASGI app with API-key auth, served by uvicorn.
            import uvicorn
            app = _ApiKeyASGI(mcp.streamable_http_app(), keys)
            sys.stderr.write(
                f"[scour-mcp] streamable-HTTP on {args.host}:{args.port}/mcp "
                f"[API-key auth ON, {len(keys)} key(s)]\n")
            sys.stderr.flush()
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        else:
            mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
