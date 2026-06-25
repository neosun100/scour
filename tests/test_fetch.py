"""Offline unit tests for scour.fetch (the research / Firecrawl layer).

httpx is faked via an injected client (fetch._make_client) and AgentCore search is
monkeypatched, so these run with no network. Covers main-text extraction, concurrent
fetching (bounds, dedupe, errors, robots.txt), and the research() merge.

Run:  python -m pytest tests/ -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scour import core, fetch  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake httpx
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, url, status=200, ctype="text/html; charset=utf-8", body=b""):
        self.status_code = status
        self._url = url
        self.headers = {"content-type": ctype}
        self.content = body
        self.encoding = "utf-8"

    @property
    def url(self):
        return self._url

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class FakeClient:
    def __init__(self, routes, raise_for=None, robots=None):
        self.routes = routes            # url -> FakeResp
        self.raise_for = raise_for or set()
        self.robots = robots or {}      # "scheme://host" -> robots.txt text
        self.concurrent = 0
        self.max_concurrent = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None):
        if url.endswith("/robots.txt"):
            base = url[: -len("/robots.txt")]
            if base in self.robots:
                return FakeResp(url, status=200,
                                body=self.robots[base].encode())
            return FakeResp(url, status=404, body=b"")
        if url in self.raise_for:
            raise RuntimeError("boom-net")
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(0.01)
        self.concurrent -= 1
        return self.routes.get(url) or FakeResp(url, status=404, body=b"")


def _inject(monkeypatch, client):
    monkeypatch.setattr(fetch, "_make_client", lambda headers, timeout: client)


HTML = b"""
<html><head><title>My Title</title></head>
<body>
  <script>evilTracker();</script>
  <nav>menu menu menu</nav>
  <article>
    <h1>Headline</h1>
    <p>Hello world, first paragraph.</p>
    <p>Second paragraph here.</p>
  </article>
  <footer>copyright</footer>
</body></html>
"""


# --------------------------------------------------------------------------- #
# extract_main_text
# --------------------------------------------------------------------------- #
def test_extract_main_text_basic():
    title, text = fetch.extract_main_text(HTML.decode(), url="https://x/a")
    assert title == "My Title"
    assert "Hello world" in text
    assert "Second paragraph" in text
    assert "evilTracker" not in text       # script stripped
    assert "menu menu" not in text         # nav stripped


def test_extract_main_text_truncates():
    big = "<html><body><article><p>" + "x" * 5000 + "</p></article></body></html>"
    _title, text = fetch.extract_main_text(big, max_chars=100)
    assert len(text) <= 101                 # 100 + ellipsis char
    assert text.endswith("…")


# --------------------------------------------------------------------------- #
# fetch_articles
# --------------------------------------------------------------------------- #
def test_fetch_articles_extracts(monkeypatch):
    routes = {
        "https://a.com/1": FakeResp("https://a.com/1", body=HTML),
        "https://b.com/2": FakeResp("https://b.com/2", body=HTML),
    }
    _inject(monkeypatch, FakeClient(routes))
    out = asyncio.run(fetch.fetch_articles(
        list(routes), respect_robots=False, rate_per_sec=0))
    assert out["total"] == 2
    assert out["errorCount"] == 0
    assert all("Hello world" in r["text"] for r in out["results"])
    assert all(r["title"] == "My Title" for r in out["results"])


def test_fetch_articles_dedupe_and_concurrency(monkeypatch):
    urls = [f"https://h.com/{i}" for i in range(8)]
    routes = {u: FakeResp(u, body=HTML) for u in urls}
    client = FakeClient(routes)
    _inject(monkeypatch, client)
    out = asyncio.run(fetch.fetch_articles(
        urls + urls,                         # duplicates
        concurrency=3, respect_robots=False, rate_per_sec=0))
    assert out["requested"] == 8             # deduped
    assert client.max_concurrent <= 3        # semaphore honored


def test_fetch_articles_http_and_ctype_errors(monkeypatch):
    routes = {
        "https://a.com/404": FakeResp("https://a.com/404", status=404, body=b""),
        "https://a.com/pdf": FakeResp("https://a.com/pdf",
                                      ctype="application/pdf", body=b"%PDF"),
        "https://a.com/ok": FakeResp("https://a.com/ok", body=HTML),
    }
    _inject(monkeypatch, FakeClient(routes))
    out = asyncio.run(fetch.fetch_articles(
        list(routes), respect_robots=False, rate_per_sec=0))
    by = {r["url"]: r for r in out["results"]}
    assert by["https://a.com/404"]["error"] == "HTTP 404"
    assert "unsupported content-type" in by["https://a.com/pdf"]["error"]
    assert by["https://a.com/ok"]["error"] is None
    assert out["errorCount"] == 2


def test_fetch_articles_exception_captured(monkeypatch):
    routes = {"https://a.com/ok": FakeResp("https://a.com/ok", body=HTML)}
    client = FakeClient(routes, raise_for={"https://a.com/boom"})
    _inject(monkeypatch, client)
    out = asyncio.run(fetch.fetch_articles(
        ["https://a.com/ok", "https://a.com/boom"],
        respect_robots=False, rate_per_sec=0))
    by = {r["url"]: r for r in out["results"]}
    assert "boom-net" in by["https://a.com/boom"]["error"]
    assert by["https://a.com/ok"]["error"] is None


def test_fetch_articles_respects_robots(monkeypatch):
    robots = {"https://a.com": "User-agent: *\nDisallow: /private"}
    routes = {
        "https://a.com/public": FakeResp("https://a.com/public", body=HTML),
        "https://a.com/private/x": FakeResp("https://a.com/private/x", body=HTML),
    }
    _inject(monkeypatch, FakeClient(routes, robots=robots))
    out = asyncio.run(fetch.fetch_articles(
        list(routes), respect_robots=True, rate_per_sec=0))
    by = {r["url"]: r for r in out["results"]}
    assert by["https://a.com/private/x"]["error"] == "blocked by robots.txt"
    assert by["https://a.com/public"]["error"] is None


def test_fetch_articles_rejects_non_http(monkeypatch):
    _inject(monkeypatch, FakeClient({}))
    with pytest.raises(core.WebSearchError):
        asyncio.run(fetch.fetch_articles(["ftp://x/y"], respect_robots=False))


def test_fetch_articles_rejects_empty(monkeypatch):
    _inject(monkeypatch, FakeClient({}))
    with pytest.raises(core.WebSearchError):
        asyncio.run(fetch.fetch_articles([], respect_robots=False))


# --------------------------------------------------------------------------- #
# research — search + fetch merge
# --------------------------------------------------------------------------- #
def test_research_merges_snippet_and_fulltext(monkeypatch):
    async def fake_search(url, region, profile, topic, max_results):
        return {"results": [
            {"url": "https://a.com/1", "title": "T1",
             "publishedDate": "2026-01-01", "text": "snippet one"},
            {"url": "https://b.com/2", "title": "T2",
             "publishedDate": "2026-02-02", "text": "snippet two"},
        ]}

    async def fake_fetch(urls, **kw):
        return {"results": [
            {"url": "https://a.com/1", "title": "T1", "text": "FULL ONE",
             "error": None},
            {"url": "https://b.com/2", "title": "T2", "text": "",
             "error": "HTTP 403"},
        ], "total": 2, "errorCount": 1, "requested": 2}

    monkeypatch.setattr(core, "search_one", fake_search)
    monkeypatch.setattr(fetch, "fetch_articles", fake_fetch)

    out = asyncio.run(fetch.research(
        "https://x.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "us-east-1", None, topic="quantum widgets", max_results=10))

    assert out["topic"] == "quantum widgets"
    assert out["sourceCount"] == 2
    assert out["fetched"] == 1
    assert out["errorCount"] == 1
    s1 = next(s for s in out["sources"] if s["url"] == "https://a.com/1")
    s2 = next(s for s in out["sources"] if s["url"] == "https://b.com/2")
    assert s1["snippet"] == "snippet one"        # search snippet retained
    assert s1["text"] == "FULL ONE"              # full text merged in
    assert s1["fetchError"] is None
    assert s2["text"] is None                    # failed fetch -> no full text
    assert s2["fetchError"] == "HTTP 403"
    assert s2["snippet"] == "snippet two"        # snippet still there


def test_research_no_fetch(monkeypatch):
    async def fake_search(url, region, profile, topic, max_results):
        return {"results": [{"url": "https://a.com/1", "title": "T1",
                             "text": "snip"}]}

    def _should_not_call(*a, **k):
        raise AssertionError("fetch_articles must not be called when fetch_full=False")

    monkeypatch.setattr(core, "search_one", fake_search)
    monkeypatch.setattr(fetch, "fetch_articles", _should_not_call)

    out = asyncio.run(fetch.research(
        "https://x.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "us-east-1", None, topic="t", fetch_full=False))
    assert out["fetched"] == 0
    assert out["sources"][0]["snippet"] == "snip"
    assert out["sources"][0]["text"] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
