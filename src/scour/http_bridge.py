#!/usr/bin/env python3
"""
scour-http — a tiny local REST bridge so "custom search" plugins (which speak plain
HTTP, not MCP) can use Scour / AgentCore Web Search.

    plugin (HTTP) ──▶ scour-http (this) ──▶ Scour core ──SigV4──▶ AgentCore Web Search

Why: many chat clients have a "custom search" option that just takes a URL and POSTs
a query, expecting JSON back. That is NOT the MCP protocol `scour-mcp` serves. This
bridge accepts those plain HTTP requests, runs the search via AgentCore, and returns
a widely-compatible JSON shape.

It is deliberately permissive about the REQUEST shape (so it works with many clients)
and logs every incoming request to stderr so you can see the exact contract your
client uses and tune from there.

Run (reads AGENTCORE_GATEWAY_URL / AWS_PROFILE from env or .env, same as the CLI):
    scour-http                       # 127.0.0.1:3000 ; endpoint accepts any path
    scour-http --port 3000 --host 127.0.0.1

Request (any of these works):
    GET  /search?q=hello              GET  /search?query=hello
    POST /search   {"query": "hello", "maxResults": 10, "exclude": ["a.com"]}
    POST /search   (form) q=hello

Response JSON (superset for broad client compatibility):
    {"query": "...", "total": N, "results": [
        {"title","url","content","snippet","text","publishedDate"} ...]}

Limits (AgentCore): maxResults is capped at 25 per query (the service hard limit),
so a client asking for 150 receives up to 25. `exclude` domains are post-filtered.
Local bind only by default; do not expose without adding auth.
"""
import argparse
import asyncio
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import core

_QUERY_KEYS = ("q", "query", "search", "keyword", "text", "input")
_MAX_KEYS = ("maxResults", "max_results", "count", "limit", "num", "n")
_EXCLUDE_KEYS = ("exclude", "excludeDomains", "exclude_domains", "blacklist")


def _first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return None


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).replace(",", " ").split() if s.strip()]


def _extract(qs_params, body_obj):
    """Pull query / maxResults / exclude from query-string params or a JSON/form body."""
    flat_qs = {k: v[0] for k, v in qs_params.items() if v}
    src = {}
    src.update(flat_qs)
    if isinstance(body_obj, dict):
        src.update({k: v for k, v in body_obj.items() if v not in (None, "")})

    query = _first(src, _QUERY_KEYS)
    raw_max = _first(src, _MAX_KEYS)
    try:
        max_results = int(raw_max) if raw_max is not None else core.DEFAULT_RESULTS
    except (TypeError, ValueError):
        max_results = core.DEFAULT_RESULTS
    # Clamp to AgentCore's hard 1-25 range.
    max_results = max(core.MIN_RESULTS, min(core.MAX_RESULTS, max_results))
    exclude = _as_list(_first(src, _EXCLUDE_KEYS))
    return query, max_results, exclude


def _search(query, max_results, exclude):
    url, region, profile = core.resolve_config()
    payload = asyncio.run(core.search_one(url, region, profile, query, max_results))
    results = payload.get("results", []) or []
    if exclude:
        def blocked(r):
            u = (r.get("url") or "").lower()
            return any(dom.lower() in u for dom in exclude)
        results = [r for r in results if not blocked(r)]
    # Emit a superset shape so different clients each find the field they expect.
    # Skip results without a URL (knowledge-graph entity rows) — useless to a
    # "search results" list and observed as noise during live testing.
    out = []
    for r in results:
        u = r.get("url") or ""
        if not u:
            continue
        text = r.get("text") or ""
        out.append({
            "title": r.get("title") or "",
            "url": u,
            "content": text,
            "snippet": text,
            "text": text,
            "publishedDate": r.get("publishedDate") or "",
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "scour-http/1.0"

    def log_message(self, fmt, *args):  # quieter default access log
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # local testing convenience
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _handle(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        body_obj = None
        raw = b""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").lower()
            try:
                if "json" in ctype:
                    body_obj = json.loads(raw.decode("utf-8"))
                elif "form" in ctype:
                    body_obj = {k: v[0] for k, v in
                                parse_qs(raw.decode("utf-8")).items() if v}
                else:  # best effort: try JSON anyway
                    body_obj = json.loads(raw.decode("utf-8"))
            except Exception:
                body_obj = None

        # MEASURE-FIRST: log the exact request the client sent, so we can match it.
        sys.stderr.write(
            f"[scour-http] {self.command} {self.path}\n"
            f"             query-params={dict((k, v[0]) for k, v in qs.items())}\n"
            f"             content-type={self.headers.get('Content-Type')!r}\n"
            f"             body={raw[:500].decode('utf-8', 'replace')!r}\n"
        )
        sys.stderr.flush()

        query, max_results, exclude = _extract(qs, body_obj)
        if not query:
            self._send(400, {"error": "no query found; send ?q=... or JSON {\"query\":...}",
                             "results": []})
            return
        try:
            results = _search(query, max_results, exclude)
        except core.WebSearchError as e:
            self._send(502, {"error": str(e), "results": []})
            return
        except Exception as e:
            self._send(502, {"error": core.format_error(e), "results": []})
            return
        self._send(200, {"query": query, "total": len(results), "results": results})

    do_GET = _handle
    do_POST = _handle


def main():
    core.load_dotenv()
    p = argparse.ArgumentParser(
        prog="scour-http",
        description="Local REST bridge: 'custom search' plugins -> Scour -> AgentCore.",
    )
    p.add_argument("--host", default=os.environ.get("SCOUR_HTTP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("SCOUR_HTTP_PORT", "3000")))
    args = p.parse_args()

    # Validate config up front (fail fast if the gateway URL/creds are missing).
    try:
        core.resolve_config()
    except core.WebSearchError as e:
        p.error(str(e))

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        f"[scour-http] listening on http://{args.host}:{args.port}  "
        f"(GET/POST any path; ?q= or JSON {{\"query\":...}})\n"
        f"[scour-http] each request is logged below so you can match your client.\n"
    )
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
