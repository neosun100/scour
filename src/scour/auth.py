"""Downstream API-key authentication for Scour's HTTP services.

Both the REST bridge (`scour-http`) and the MCP server (`scour-mcp --http`) use this
to gate access with a shared secret, so you can host Scour centrally (holding the AWS
identity) and let any service call it with just a key — no AWS credentials needed
downstream.

Keys come from `--api-key` (repeatable) and/or the `SCOUR_API_KEYS` env var
(comma-separated). Callers present a key as either header:
    Authorization: Bearer <key>
    X-API-Key: <key>

Secure-by-default: a caller binding to a non-loopback address with NO keys configured
should refuse to start unless they explicitly pass --insecure (see require_or_refuse).
"""
import hmac
import os
import secrets

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1", ""}


def resolve_keys(cli_keys=None):
    """Collect API keys from CLI args + the SCOUR_API_KEYS env var into a set."""
    keys = {k.strip() for k in (cli_keys or []) if k and k.strip()}
    for k in os.environ.get("SCOUR_API_KEYS", "").split(","):
        if k.strip():
            keys.add(k.strip())
    return keys


def extract_key(get):
    """Pull the presented key from a header accessor `get(name)->value|None`.

    Accepts `Authorization: Bearer <key>` or `X-API-Key: <key>` (case-insensitive).
    """
    auth = (get("Authorization") or get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (get("X-API-Key") or get("x-api-key") or "").strip()


def key_ok(provided, keys):
    """Constant-time check that `provided` matches one of `keys`."""
    if not provided or not keys:
        return False
    return any(hmac.compare_digest(provided, k) for k in keys)


def is_loopback(host):
    """True if `host` is a loopback/local bind address."""
    return host in _LOOPBACK


def require_or_refuse(host, keys, insecure, error):
    """Enforce secure-by-default at startup.

    Refuse to expose an unauthenticated service on a non-loopback address. `error`
    is a callable (e.g. argparse parser.error) used to abort with a message.
    """
    if keys:
        return
    if is_loopback(host):
        return  # local-only without keys is fine (dev convenience)
    if insecure:
        return  # explicit opt-in
    error(
        f"refusing to bind {host} with no API keys (that would be an open proxy "
        "anyone could use on your AWS bill). Pass --api-key KEY (repeatable) or set "
        "SCOUR_API_KEYS, or pass --insecure to override."
    )


def generate_key(nbytes=32):
    """Generate a strong URL-safe API key (handy for `scour-* --gen-key`)."""
    return "scour_" + secrets.token_urlsafe(nbytes)
