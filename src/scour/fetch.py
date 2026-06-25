"""Concurrent full-text article fetching + research orchestration.

This module is the "open-source Firecrawl" layer on top of AgentCore Web Search:

  * `fetch_articles(urls, ...)` concurrently GETs many public web pages from their
    origin servers and extracts the main article text (politely: robots.txt-aware,
    rate-limited, bounded, with per-URL error capture). This uses the local host's
    own network/CPU — unlike Web Search, which runs entirely inside AWS.
  * `research(topic, ...)` ties it together: discover URLs via AgentCore Web Search,
    then fetch their full text, and merge into one structured corpus the caller's
    agent can summarize/analyze. Search snippets and source citations are retained.

Compliance note: fetching a public URL from its origin is ordinary web access,
governed by that site's robots.txt / terms / copyright — NOT by AgentCore's
acceptable-use policy (which concerns Web Search index content). Respect robots.txt
(on by default), don't hammer hosts, and don't redistribute full text in bulk.

Heavy deps (httpx, bs4) are imported lazily so the module imports cheaply for tests.
"""
import asyncio
import time
from urllib.parse import urlsplit

from . import core

# Politeness / safety defaults (all overridable):
DEFAULT_FETCH_CONCURRENCY = 10
DEFAULT_FETCH_RATE = 5.0          # GETs/sec across the batch (be a good citizen)
DEFAULT_TIMEOUT = 20.0            # seconds per request
DEFAULT_MAX_BYTES = 2_000_000     # cap per page (don't download huge blobs)
DEFAULT_MAX_CHARS = 8000          # truncate extracted text per article
DEFAULT_USER_AGENT = (
    "scour-research/1.0 (+https://github.com/; respectful fetcher)"
)
_TEXT_CTYPES = ("text/html", "application/xhtml", "text/plain")


# --------------------------------------------------------------------------- #
# Main-text extraction (trafilatura if available, else bs4, else stdlib)
# --------------------------------------------------------------------------- #
def extract_main_text(html, url=None, max_chars=DEFAULT_MAX_CHARS):
    """Return (title, text) extracted from an HTML string.

    Prefers `trafilatura` (best quality) when installed; otherwise uses BeautifulSoup
    + lxml with a sensible noise-stripping heuristic; falls back to a stdlib stripper.
    """
    title, text = None, ""

    # 1) trafilatura — optional, best quality.
    try:
        import trafilatura  # type: ignore
        extracted = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=False
        )
        if extracted:
            text = extracted.strip()
            meta = trafilatura.extract_metadata(html)
            if meta and getattr(meta, "title", None):
                title = meta.title
    except ImportError:
        pass
    except Exception:
        pass

    # 2) BeautifulSoup + lxml.
    if not text:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            if soup.title and soup.title.string:
                title = title or soup.title.string.strip()
            og = soup.find("meta", attrs={"property": "og:title"})
            if not title and og and og.get("content"):
                title = og["content"].strip()
            if not title and soup.find("h1"):
                title = soup.find("h1").get_text(" ", strip=True)
            for tag in soup(
                ["script", "style", "noscript", "header", "footer", "nav",
                 "aside", "form", "svg", "iframe", "button"]
            ):
                tag.decompose()
            root = soup.find("article") or soup.find("main") or soup.body or soup
            parts = []
            for el in root.find_all(
                ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre"]
            ):
                t = el.get_text(" ", strip=True)
                if t:
                    parts.append(t)
            text = "\n".join(parts).strip() or root.get_text("\n", strip=True)
        except ImportError:
            pass
        except Exception:
            pass

    # 3) stdlib fallback — strip tags crudely.
    if not text:
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", html or "")
        text = _re.sub(r"\s+", " ", text).strip()

    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return title, text


# --------------------------------------------------------------------------- #
# robots.txt (async, cached per host)
# --------------------------------------------------------------------------- #
async def _robots_allowed(client, url, user_agent, cache, lock):
    """True if `user_agent` may fetch `url` per the host's robots.txt (cached)."""
    from urllib.robotparser import RobotFileParser
    parts = urlsplit(url)
    host_key = (parts.scheme, parts.netloc)
    async with lock:
        rp = cache.get(host_key)
        if rp is None:
            rp = RobotFileParser()
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            try:
                r = await client.get(robots_url, timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                rp.parse([])  # unreachable robots => allow (fail-open, but polite-ish)
            cache[host_key] = rp
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _make_client(headers, timeout):
    """Build the async HTTP client. Indirected so tests can inject a fake."""
    import httpx
    return httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout)


async def fetch_articles(
    urls,
    concurrency=DEFAULT_FETCH_CONCURRENCY,
    rate_per_sec=DEFAULT_FETCH_RATE,
    timeout=DEFAULT_TIMEOUT,
    max_bytes=DEFAULT_MAX_BYTES,
    max_chars=DEFAULT_MAX_CHARS,
    respect_robots=True,
    user_agent=DEFAULT_USER_AGENT,
):
    """Concurrently fetch + extract main text from many URLs.

    Bounded by `concurrency` (semaphore) and `rate_per_sec` (token bucket). Each
    result: {url, finalUrl, status, title, text, chars, fetchedAt, error}. Per-URL
    failures are captured, never raised. Returns
    {results, total, errorCount, requested}.
    """
    urls = _dedupe([u.strip() for u in (urls or []) if u and u.strip()])
    if not urls:
        raise core.WebSearchError("no URLs to fetch")
    for u in urls:
        if not u.lower().startswith(("http://", "https://")):
            raise core.WebSearchError(f"only http(s) URLs are supported: {u}")
    if concurrency < 1:
        raise core.WebSearchError("concurrency must be >= 1")

    limiter = core.RateLimiter(rate_per_sec)
    sem = asyncio.Semaphore(concurrency)
    robots_cache, robots_lock = {}, asyncio.Lock()
    headers = {"User-Agent": user_agent}

    # httpx imported lazily; raise a clear error if it's somehow unavailable.
    try:
        import httpx  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise core.WebSearchError("httpx is required (pip install .)") from e

    async with _make_client(headers, timeout) as client:

        async def one(url):
            rec = {"url": url, "finalUrl": None, "status": None, "title": None,
                   "text": "", "chars": 0, "fetchedAt": None, "error": None}
            async with sem:
                if respect_robots and not await _robots_allowed(
                    client, url, user_agent, robots_cache, robots_lock
                ):
                    rec["error"] = "blocked by robots.txt"
                    return rec
                await limiter.acquire()
                try:
                    resp = await client.get(url)
                    rec["status"] = resp.status_code
                    rec["finalUrl"] = str(resp.url)
                    rec["fetchedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime())
                    if resp.status_code >= 400:
                        rec["error"] = f"HTTP {resp.status_code}"
                        return rec
                    ctype = resp.headers.get("content-type", "").lower()
                    if ctype and not any(c in ctype for c in _TEXT_CTYPES):
                        rec["error"] = f"unsupported content-type: {ctype}"
                        return rec
                    body = resp.content[:max_bytes]
                    html = body.decode(resp.encoding or "utf-8", errors="replace")
                    title, text = extract_main_text(html, url=str(resp.url),
                                                     max_chars=max_chars)
                    rec["title"] = title
                    rec["text"] = text
                    rec["chars"] = len(text)
                except Exception as e:
                    rec["error"] = core.format_error(e)
            return rec

        results = await asyncio.gather(*(one(u) for u in urls))

    return {
        "results": results,
        "total": len(results),
        "errorCount": sum(1 for r in results if r["error"]),
        "requested": len(urls),
    }


# --------------------------------------------------------------------------- #
# research — search a topic, fetch the results' full text, merge for analysis
# --------------------------------------------------------------------------- #
async def research(
    url,
    region,
    profile,
    topic,
    max_results=core.DEFAULT_RESULTS,
    fetch_full=True,
    max_chars=DEFAULT_MAX_CHARS,
    fetch_concurrency=DEFAULT_FETCH_CONCURRENCY,
    fetch_rate=DEFAULT_FETCH_RATE,
    respect_robots=True,
):
    """Open-source-Firecrawl-style research macro.

    1) Discover sources via AgentCore Web Search (`topic`, up to `max_results`).
    2) If `fetch_full`, concurrently fetch each source's full text from origin.
    3) Merge into one structured corpus: each source keeps its search snippet,
       citation (url/title/publishedDate) and (when fetched) full `text`.

    Returns {topic, sourceCount, fetched, sources:[...], errorCount}. The caller's
    agent/LLM performs the actual summary/analysis over `sources`.
    """
    core.validate_query(topic)
    core.validate_max_results(max_results)
    payload = await core.search_one(url, region, profile, topic, max_results)
    search_results = payload.get("results", []) or []

    by_url = {}
    sources = []
    for r in search_results:
        u = r.get("url")
        src = {
            "url": u,
            "title": r.get("title"),
            "publishedDate": r.get("publishedDate"),
            "snippet": r.get("text"),     # the Web Search semantic snippet
            "text": None,                 # full text, filled below if fetched
            "fetchError": None,
        }
        sources.append(src)
        if u:
            by_url[u] = src

    fetched = 0
    err = 0
    if fetch_full and by_url:
        fr = await fetch_articles(
            list(by_url.keys()),
            concurrency=fetch_concurrency,
            rate_per_sec=fetch_rate,
            max_chars=max_chars,
            respect_robots=respect_robots,
        )
        for item in fr["results"]:
            src = by_url.get(item["url"])
            if not src:
                continue
            if item["error"]:
                src["fetchError"] = item["error"]
                err += 1
            else:
                src["text"] = item["text"]
                if item.get("title") and not src["title"]:
                    src["title"] = item["title"]
                fetched += 1

    return {
        "topic": topic,
        "sourceCount": len(sources),
        "fetched": fetched,
        "errorCount": err,
        "sources": sources,
    }
