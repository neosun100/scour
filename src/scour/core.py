"""Shared core for AgentCore Web Search — connection, single & concurrent search.

Both the CLI (`cli.py`) and the MCP server (`mcp_server.py`) build on this module so
there is a single source of truth for SigV4/MCP transport, gateway-URL validation,
tool-name resolution, result parsing, rate limiting and concurrent fan-out.

Design notes:
  * Real runtime dependencies (`mcp_proxy_for_aws`, `boto3`, the `mcp` SDK) are
    imported lazily inside the functions that need them, so `import core` stays cheap
    and unit tests can import and exercise the pure-Python helpers (URL validation,
    rate limiter, aggregation) without those packages installed.
  * All signing/transport lives in AWS's `mcp-proxy-for-aws` library — there is no
    hand-rolled SigV4 here and no subprocess/proxy to spawn.
  * Concurrency respects the AgentCore quotas (see AGENTS.md): Web Search default
    rate is 10 TPS and the gateway allows up to 1000 concurrent connections, both
    adjustable via Service Quotas. The RateLimiter + semaphore keep us inside them.
"""
import asyncio
import json
import os
import pathlib
import re
import time
import urllib.parse

SERVICE = "bedrock-agentcore"
TOOL_NAME = "WebSearch"

# AgentCore Web Search hard limits (from the AWS docs — keep in sync):
MAX_QUERY_LEN = 200          # query must be <= 200 characters
MIN_RESULTS = 1
MAX_RESULTS = 25             # maxResults valid range 1-25 (tool default 10)
DEFAULT_RESULTS = 10

# Default client-side guards, sized to the default AgentCore quotas (adjustable):
DEFAULT_RATE_PER_SEC = 10.0  # "Rate of Web Search Tool requests": 10 TPS
DEFAULT_CONCURRENCY = 10     # gateway allows up to 1000 concurrent connections

# The gateway endpoint must be an HTTPS AgentCore Gateway host. Validate before
# connecting so a malformed/unexpected value fails fast and can't point the client
# at an unintended host or a non-https scheme.
GATEWAY_HOST_RE = re.compile(
    r"^[a-z0-9-]+\.gateway\.bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com$"
)


class WebSearchError(RuntimeError):
    """Raised for configuration / validation / connection problems."""


# --------------------------------------------------------------------------- #
# Pure helpers (no external deps — safe to unit test directly)
# --------------------------------------------------------------------------- #
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


def validate_query(query):
    """Validate a single query string against the tool's hard limits."""
    if not query or not query.strip():
        raise WebSearchError("query is empty")
    if len(query) > MAX_QUERY_LEN:
        raise WebSearchError(
            f"query must be <= {MAX_QUERY_LEN} characters (got {len(query)})"
        )
    return query


def validate_max_results(max_results):
    """Validate / normalize maxResults into the tool's 1-25 range."""
    if max_results is None:
        return None
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise WebSearchError("max_results must be an integer")
    if not MIN_RESULTS <= max_results <= MAX_RESULTS:
        raise WebSearchError(
            f"max_results must be between {MIN_RESULTS} and {MAX_RESULTS}"
        )
    return max_results


def result_payload(call_result):
    """Pull the structured JSON payload ({"results": [...]}) out of an MCP result.

    Prefer the SDK's structured field; else parse the text content blocks (the
    Web Search Tool returns the JSON both as structuredContent and as a text block).
    """
    structured = getattr(call_result, "structuredContent", None)
    if isinstance(structured, dict) and "results" in structured:
        return structured
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


def aggregate(per_query_payloads, dedupe=True):
    """Merge many per-query payloads into one deduped result set.

    `per_query_payloads` is a list of (query, payload_dict, error_or_None).
    Dedupe is by source URL — preserves first-seen order. Source citations
    (url/title/publishedDate) are retained per the AgentCore acceptable-use policy.
    """
    per_query = []
    merged = []
    seen = set()
    for query, payload, err in per_query_payloads:
        results = (payload or {}).get("results", []) or []
        per_query.append({"query": query, "count": len(results), "error": err})
        for r in results:
            url = r.get("url") if isinstance(r, dict) else None
            if dedupe and url and url in seen:
                continue
            if url:
                seen.add(url)
            merged.append(r)
    return {
        "queries": per_query,
        "results": merged,
        "total": len(merged),
        "queryCount": len(per_query),
        "errorCount": sum(1 for q in per_query if q["error"]),
    }


# --------------------------------------------------------------------------- #
# RateLimiter — token bucket, async-safe
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Simple async token-bucket limiter.

    `rate_per_sec <= 0` disables limiting. acquire() blocks until a token is free,
    so concurrent callers are smoothed to at most `rate_per_sec` calls/second.
    """

    def __init__(self, rate_per_sec):
        self.rate = float(rate_per_sec)
        self._tokens = self.rate
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        if self.rate <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.rate, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                # Sleep just long enough for the next token to accrue.
                await asyncio.sleep((1 - self._tokens) / self.rate)


# --------------------------------------------------------------------------- #
# Credentials + connection (lazy heavy imports)
# --------------------------------------------------------------------------- #
def resolve_credentials(profile):
    """Resolve AWS credentials via boto3, return a frozen Credentials object.

    We resolve here and pass `credentials=` to aws_iam_streamablehttp_client rather
    than handing it `aws_profile`: the proxy's profile handling mis-signs for some
    profile names (e.g. names containing '+'), yielding a 403, whereas explicit
    credentials sign correctly. profile=None uses the default chain (env vars,
    AWS_PROFILE, shared config, SSO, instance role, ...).
    """
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - only when deps missing
        raise WebSearchError(
            "boto3 is required (installed via `pip install .` / mcp-proxy-for-aws)"
        ) from e
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    creds = session.get_credentials()
    if creds is None:
        raise WebSearchError(
            "No AWS credentials found. Set AWS_PROFILE, run `aws configure`, or "
            "pass a profile."
        )
    return creds.get_frozen_credentials()


def _bypass_proxy_for(host):
    """Make the gateway connection bypass a local/system HTTP proxy by default.

    httpx honors the OS proxy settings (trust_env) — common on macOS with Clash/Surge
    (e.g. 127.0.0.1:7890). A flaky local proxy then breaks the TLS handshake to the
    AgentCore gateway with an opaque ConnectError. The gateway is always an AWS
    endpoint that's reachable directly, so we append its host to NO_PROXY unless the
    user explicitly opts in to proxying it via SCOUR_GATEWAY_USE_PROXY=1.
    """
    if not host or os.environ.get("SCOUR_GATEWAY_USE_PROXY") == "1":
        return
    for var in ("NO_PROXY", "no_proxy"):
        items = [x.strip() for x in os.environ.get(var, "").split(",") if x.strip()]
        if host not in items:
            items.append(host)
            os.environ[var] = ",".join(items)


def format_error(exc):
    """Flatten an ExceptionGroup (anyio/TaskGroup) to a readable root cause string.

    The streamable-HTTP transport wraps real errors in an ExceptionGroup, so a bare
    str(exc) yields the useless 'unhandled errors in a TaskGroup'. Surface the first
    leaf exception's type+message instead.
    """
    seen = exc
    for _ in range(5):
        subs = getattr(seen, "exceptions", None)
        if not subs:
            break
        seen = subs[0]
    return f"{type(seen).__name__}: {seen}"


def connect(url, region, profile):
    """Return an async context manager for a SigV4-signed MCP transport.

    aws_iam_streamablehttp_client SigV4-signs every request as `bedrock-agentcore`
    using the resolved credentials (or the default chain).
    """
    try:
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    except ImportError as e:
        raise WebSearchError(
            "mcp-proxy-for-aws is required. Install with `pip install .` "
            "(or `pip install mcp-proxy-for-aws`)."
        ) from e
    parsed = urllib.parse.urlparse(url)
    _bypass_proxy_for(parsed.hostname)
    return aws_iam_streamablehttp_client(
        endpoint=url,
        aws_service=SERVICE,
        aws_region=region,
        credentials=resolve_credentials(profile),
    )


def _client_session():
    """Lazily import the mcp SDK's ClientSession."""
    try:
        from mcp import ClientSession
    except ImportError as e:  # pragma: no cover
        raise WebSearchError("the `mcp` SDK is required (pip install mcp).") from e
    return ClientSession


# --------------------------------------------------------------------------- #
# Async operations
# --------------------------------------------------------------------------- #
async def list_tools(url, region, profile):
    ClientSession = _client_session()
    async with await _await_cm(connect(url, region, profile)) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def _await_cm(maybe_awaitable):
    """aws_iam_streamablehttp_client may return a context manager or a coroutine
    yielding one; normalize so callers can `async with await _await_cm(...)`."""
    if asyncio.iscoroutine(maybe_awaitable):
        return await maybe_awaitable
    return maybe_awaitable


async def search_one(url, region, profile, query, max_results=DEFAULT_RESULTS):
    """Run a single web search, returning the parsed payload ({"results": [...]})."""
    validate_query(query)
    validate_max_results(max_results)
    ClientSession = _client_session()
    async with await _await_cm(connect(url, region, profile)) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_name = resolve_tool_name([t.name for t in tools.tools])
            args = {"query": query}
            if max_results is not None:
                args["maxResults"] = max_results
            result = await session.call_tool(tool_name, args)
            return result_payload(result)


async def search_batch(
    url,
    region,
    profile,
    queries,
    max_results=DEFAULT_RESULTS,
    concurrency=DEFAULT_CONCURRENCY,
    rate_per_sec=DEFAULT_RATE_PER_SEC,
    dedupe=True,
):
    """Run many web searches concurrently over ONE gateway session, then aggregate.

    A semaphore caps in-flight calls (`concurrency`) and a token-bucket RateLimiter
    smooths the call rate (`rate_per_sec`) to stay within AgentCore quotas. Per-query
    failures are captured (not raised) so one bad query never sinks the batch.
    Returns the aggregate dict from `aggregate()`.
    """
    if not queries:
        raise WebSearchError("queries list is empty")
    for q in queries:
        validate_query(q)
    validate_max_results(max_results)
    if concurrency < 1:
        raise WebSearchError("concurrency must be >= 1")

    limiter = RateLimiter(rate_per_sec)
    sem = asyncio.Semaphore(concurrency)
    ClientSession = _client_session()

    async with await _await_cm(connect(url, region, profile)) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_name = resolve_tool_name([t.name for t in tools.tools])

            async def one(query):
                async with sem:
                    await limiter.acquire()
                    args = {"query": query}
                    if max_results is not None:
                        args["maxResults"] = max_results
                    try:
                        res = await session.call_tool(tool_name, args)
                        return query, result_payload(res), None
                    except Exception as e:  # capture, don't sink the batch
                        return query, {}, format_error(e)

            done = await asyncio.gather(*(one(q) for q in queries))
    return aggregate(done, dedupe=dedupe)


# --------------------------------------------------------------------------- #
# Config / .env
# --------------------------------------------------------------------------- #
def load_dotenv():
    """Load KEY=VALUE lines from a local .env (cwd, then this file's dir) into the
    environment without overriding existing values. Keeps the packaged tools
    self-sufficient — no shell wrapper needed. Lines starting with # are ignored."""
    seen = set()
    for base in (pathlib.Path.cwd(), pathlib.Path(__file__).resolve().parent):
        env_path = base / ".env"
        if env_path in seen or not env_path.is_file():
            continue
        seen.add(env_path)
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def resolve_config(gateway_url=None, region=None, profile=None):
    """Resolve (url, region, profile) from explicit args then env, validating the URL.

    Resolution (highest first):
      url:     arg > AGENTCORE_GATEWAY_URL                (required)
      region:  arg > region parsed from URL > AWS_REGION > AWS_DEFAULT_REGION > us-east-1
      profile: arg > AWS_PROFILE > default credential chain (None)
    """
    url = gateway_url or os.environ.get("AGENTCORE_GATEWAY_URL")
    if not url:
        raise WebSearchError(
            "no gateway URL: set AGENTCORE_GATEWAY_URL or pass --gateway-url "
            "(e.g. https://<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp)"
        )
    url = validate_url(url)
    region = (
        region
        or region_from_url(url)
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    profile = profile or os.environ.get("AWS_PROFILE")
    return url, region, profile
