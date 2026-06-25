"""Offline unit tests for scour.core.

No AWS / network / mcp-proxy-for-aws needed: the upstream connection and the mcp
ClientSession are faked via monkeypatch, so these exercise URL validation, argument
validation, result parsing, the token-bucket RateLimiter, and the concurrent
search_batch aggregation/de-duplication path entirely in-process.

Run:  python -m pytest tests/ -q     (or: python tests/test_core.py)
"""
import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

# Make `import scour.core` work from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scour import core  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
GOOD_URL = "https://abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def test_validate_url_accepts_gateway_host():
    assert core.validate_url(GOOD_URL) == GOOD_URL


@pytest.mark.parametrize("bad", [
    "",
    "http://abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",  # not https
    "https://evil.example.com/mcp",                                          # wrong host
    "https://gateway.bedrock-agentcore.amazonaws.com/mcp",                   # no id/region
])
def test_validate_url_rejects_bad(bad):
    with pytest.raises(core.WebSearchError):
        core.validate_url(bad)


def test_region_from_url():
    assert core.region_from_url(GOOD_URL) == "us-east-1"
    assert core.region_from_url("https://x.gateway.bedrock-agentcore.eu-west-1.amazonaws.com/mcp") == "eu-west-1"
    assert core.region_from_url("https://nope.example.com") is None


def test_resolve_tool_name():
    assert core.resolve_tool_name(["web-search-tool___WebSearch"]) == "web-search-tool___WebSearch"
    assert core.resolve_tool_name(["WebSearch"]) == "WebSearch"
    assert core.resolve_tool_name(["foo", "bar___WebSearch"]) == "bar___WebSearch"
    assert core.resolve_tool_name(["MyWebSearchThing"]) == "MyWebSearchThing"  # fuzzy
    with pytest.raises(core.WebSearchError):
        core.resolve_tool_name([])


def test_validate_query():
    assert core.validate_query("hello") == "hello"
    with pytest.raises(core.WebSearchError):
        core.validate_query("")
    with pytest.raises(core.WebSearchError):
        core.validate_query("   ")
    with pytest.raises(core.WebSearchError):
        core.validate_query("x" * (core.MAX_QUERY_LEN + 1))


def test_validate_max_results():
    assert core.validate_max_results(None) is None
    assert core.validate_max_results(1) == 1
    assert core.validate_max_results(25) == 25
    for bad in (0, 26, -1, "5", True):
        with pytest.raises(core.WebSearchError):
            core.validate_max_results(bad)


# --------------------------------------------------------------------------- #
# result_payload — structured field, text block, and miss
# --------------------------------------------------------------------------- #
class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, structured=None, content=None):
        self.structuredContent = structured
        self.content = content or []


def test_result_payload_prefers_structured():
    r = _Result(structured={"results": [{"url": "u"}]})
    assert core.result_payload(r) == {"results": [{"url": "u"}]}


def test_result_payload_parses_text_block():
    r = _Result(content=[_Block('{"results": [{"url": "u2"}]}')])
    assert core.result_payload(r) == {"results": [{"url": "u2"}]}


def test_result_payload_empty_on_miss():
    assert core.result_payload(_Result(content=[_Block("not json")])) == {}
    assert core.result_payload(_Result()) == {}


# --------------------------------------------------------------------------- #
# aggregate — merge + dedupe by URL + error counting
# --------------------------------------------------------------------------- #
def test_aggregate_dedupes_by_url():
    payloads = [
        ("q1", {"results": [{"url": "a", "title": "A"}, {"url": "b"}]}, None),
        ("q2", {"results": [{"url": "b"}, {"url": "c"}]}, None),         # b is dup
        ("q3", {}, "Boom: failed"),
    ]
    out = core.aggregate(payloads)
    urls = [r["url"] for r in out["results"]]
    assert urls == ["a", "b", "c"]          # b deduped, first-seen order kept
    assert out["total"] == 3
    assert out["queryCount"] == 3
    assert out["errorCount"] == 1
    assert out["queries"][2]["error"] == "Boom: failed"


def test_aggregate_keeps_dupes_when_dedupe_false():
    payloads = [
        ("q1", {"results": [{"url": "a"}]}, None),
        ("q2", {"results": [{"url": "a"}]}, None),
    ]
    out = core.aggregate(payloads, dedupe=False)
    assert out["total"] == 2


# --------------------------------------------------------------------------- #
# RateLimiter — disabled passthrough + actual throttling
# --------------------------------------------------------------------------- #
def test_ratelimiter_disabled_is_instant():
    async def go():
        rl = core.RateLimiter(0)
        t0 = time.monotonic()
        for _ in range(100):
            await rl.acquire()
        return time.monotonic() - t0
    assert asyncio.run(go()) < 0.2


def test_ratelimiter_throttles_when_depleted():
    async def go():
        rl = core.RateLimiter(10)            # 10 tokens/sec, bucket starts full (10)
        t0 = time.monotonic()
        for _ in range(15):                  # 10 instant, 5 more at 10/s => ~0.5s
            await rl.acquire()
        return time.monotonic() - t0
    elapsed = asyncio.run(go())
    assert 0.4 <= elapsed < 2.0


# --------------------------------------------------------------------------- #
# search_batch — fake the upstream connection + mcp session
# --------------------------------------------------------------------------- #
class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeToolsResult:
    def __init__(self, names):
        self.tools = [_FakeTool(n) for n in names]


class _FakeSession:
    """Minimal stand-in for mcp.ClientSession used by core.search_batch/search_one."""
    max_in_flight = 0
    in_flight = 0

    def __init__(self, read, write):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        return _FakeToolsResult(["web-search-tool___WebSearch"])

    async def call_tool(self, name, args):
        # Track concurrency to prove the semaphore bounds in-flight calls.
        type(self).in_flight += 1
        type(self).max_in_flight = max(type(self).max_in_flight, type(self).in_flight)
        await asyncio.sleep(0.02)
        q = args["query"]
        if q == "boom":
            type(self).in_flight -= 1
            raise RuntimeError("simulated failure")
        # Each query returns one unique URL plus a shared duplicate URL.
        payload = {"results": [
            {"url": f"https://ex/{q}", "title": q, "text": f"snippet {q}"},
            {"url": "https://ex/shared", "title": "shared"},
        ]}
        type(self).in_flight -= 1
        return _Result(structured=payload)


class _FakeConnCM:
    async def __aenter__(self):
        return ("read", "write", "sid")

    async def __aexit__(self, *exc):
        return False


def _install_fakes(monkeypatch):
    monkeypatch.setattr(core, "connect", lambda url, region, profile: _FakeConnCM())
    monkeypatch.setattr(core, "_client_session", lambda: _FakeSession)
    _FakeSession.max_in_flight = 0
    _FakeSession.in_flight = 0


def test_search_batch_aggregates_and_dedupes(monkeypatch):
    _install_fakes(monkeypatch)
    out = asyncio.run(core.search_batch(
        GOOD_URL, "us-east-1", None,
        queries=["alpha", "beta", "gamma"],
        max_results=5, concurrency=2, rate_per_sec=0,
    ))
    # 3 unique per-query URLs + 1 shared (deduped) = 4 total.
    assert out["total"] == 4
    assert out["queryCount"] == 3
    assert out["errorCount"] == 0
    urls = {r["url"] for r in out["results"]}
    assert "https://ex/shared" in urls
    assert sum(1 for r in out["results"] if r["url"] == "https://ex/shared") == 1


def test_search_batch_concurrency_is_bounded(monkeypatch):
    _install_fakes(monkeypatch)
    asyncio.run(core.search_batch(
        GOOD_URL, "us-east-1", None,
        queries=[f"q{i}" for i in range(10)],
        concurrency=3, rate_per_sec=0,
    ))
    assert _FakeSession.max_in_flight <= 3


def test_search_batch_captures_per_query_errors(monkeypatch):
    _install_fakes(monkeypatch)
    out = asyncio.run(core.search_batch(
        GOOD_URL, "us-east-1", None,
        queries=["ok", "boom", "fine"],
        concurrency=3, rate_per_sec=0,
    ))
    assert out["errorCount"] == 1
    errored = [q for q in out["queries"] if q["error"]]
    assert errored[0]["query"] == "boom"
    assert "simulated failure" in errored[0]["error"]
    # The two good queries still produced results.
    assert out["total"] >= 2


def test_search_batch_rejects_empty(monkeypatch):
    _install_fakes(monkeypatch)
    with pytest.raises(core.WebSearchError):
        asyncio.run(core.search_batch(GOOD_URL, "us-east-1", None, queries=[]))


def test_search_one_returns_payload(monkeypatch):
    _install_fakes(monkeypatch)
    out = asyncio.run(core.search_one(GOOD_URL, "us-east-1", None, "hello", 5))
    assert out["results"][0]["title"] == "hello"


# --------------------------------------------------------------------------- #
# resolve_config
# --------------------------------------------------------------------------- #
def test_resolve_config_from_env(monkeypatch):
    monkeypatch.setenv("AGENTCORE_GATEWAY_URL", GOOD_URL)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    url, region, profile = core.resolve_config()
    assert url == GOOD_URL
    assert region == "us-east-1"          # parsed from URL
    assert profile is None


def test_resolve_config_requires_url(monkeypatch):
    monkeypatch.delenv("AGENTCORE_GATEWAY_URL", raising=False)
    with pytest.raises(core.WebSearchError):
        core.resolve_config()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
