"""Unit + integration tests for Scour's API-key auth (auth.py + scour-http gate).

Run:  python -m pytest tests/ -q
"""
import asyncio
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scour import auth, core, http_bridge  # noqa: E402


# --------------------------------------------------------------------------- #
# auth helpers
# --------------------------------------------------------------------------- #
def test_resolve_keys_cli_and_env(monkeypatch):
    monkeypatch.delenv("SCOUR_API_KEYS", raising=False)
    assert auth.resolve_keys(["a", "b", " "]) == {"a", "b"}
    monkeypatch.setenv("SCOUR_API_KEYS", "x, y ,")
    assert auth.resolve_keys(["a"]) == {"a", "x", "y"}
    monkeypatch.delenv("SCOUR_API_KEYS", raising=False)
    assert auth.resolve_keys(None) == set()


def test_extract_key_bearer_and_xapikey():
    assert auth.extract_key({"Authorization": "Bearer abc123"}.get) == "abc123"
    assert auth.extract_key({"authorization": "bearer xyz"}.get) == "xyz"
    assert auth.extract_key({"X-API-Key": "k9"}.get) == "k9"
    assert auth.extract_key({"x-api-key": "k8"}.get) == "k8"
    assert auth.extract_key({}.get) == ""


def test_key_ok():
    keys = {"good1", "good2"}
    assert auth.key_ok("good1", keys) is True
    assert auth.key_ok("good2", keys) is True
    assert auth.key_ok("bad", keys) is False
    assert auth.key_ok("", keys) is False
    assert auth.key_ok("good1", set()) is False


def test_is_loopback():
    for h in ("127.0.0.1", "localhost", "::1", ""):
        assert auth.is_loopback(h)
    assert not auth.is_loopback("0.0.0.0")
    assert not auth.is_loopback("10.0.0.5")


def test_require_or_refuse():
    calls = []
    err = lambda m: calls.append(m)  # noqa: E731
    # loopback + no keys -> allowed (no error)
    auth.require_or_refuse("127.0.0.1", set(), False, err)
    # non-loopback + keys -> allowed
    auth.require_or_refuse("0.0.0.0", {"k"}, False, err)
    # non-loopback + insecure -> allowed
    auth.require_or_refuse("0.0.0.0", set(), True, err)
    assert calls == []
    # non-loopback + no keys + not insecure -> refuses (error called)
    auth.require_or_refuse("0.0.0.0", set(), False, err)
    assert len(calls) == 1 and "refusing" in calls[0]


def test_generate_key():
    k = auth.generate_key()
    assert k.startswith("scour_") and len(k) > 20
    assert auth.generate_key() != auth.generate_key()  # random


# --------------------------------------------------------------------------- #
# Integration: scour-http gate returns 401 without key, 200 with key
# --------------------------------------------------------------------------- #
def _start_bridge(monkeypatch, keys):
    """Start the bridge HTTP server on an ephemeral port with search mocked."""
    monkeypatch.setattr(core, "resolve_config",
                        lambda *a, **k: ("https://x.gateway.bedrock-agentcore."
                                         "us-east-1.amazonaws.com/mcp", "us-east-1", None))

    async def fake_search(url, region, profile, query, max_results):
        return {"results": [{"url": "https://a.com/1", "title": "T",
                             "text": "snippet"}]}
    monkeypatch.setattr(core, "search_one", fake_search)
    http_bridge._API_KEYS = set(keys)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), http_bridge.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _get(port, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/search?q=hello",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_bridge_requires_key(monkeypatch):
    httpd, port = _start_bridge(monkeypatch, {"secret-key"})
    try:
        # no key -> 401
        code, body = _get(port)
        assert code == 401 and "unauthorized" in body["error"]
        # wrong key -> 401
        code, _ = _get(port, {"Authorization": "Bearer nope"})
        assert code == 401
        # correct key (Bearer) -> 200 with results
        code, body = _get(port, {"Authorization": "Bearer secret-key"})
        assert code == 200 and body["total"] == 1
        # correct key (X-API-Key) -> 200
        code, body = _get(port, {"X-API-Key": "secret-key"})
        assert code == 200 and body["results"][0]["url"] == "https://a.com/1"
    finally:
        httpd.shutdown()


def test_bridge_open_when_no_keys(monkeypatch):
    httpd, port = _start_bridge(monkeypatch, set())  # no keys -> open
    try:
        code, body = _get(port)
        assert code == 200 and body["total"] == 1
    finally:
        httpd.shutdown()
        http_bridge._API_KEYS = set()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
