#!/usr/bin/env python3
"""Scour demo — showcases single search, concurrent batch, and full-text research.

Run (needs AGENTCORE_GATEWAY_URL + AWS creds, e.g. a .env in the repo root):

    python examples/demo.py

Each step makes real calls to AgentCore Web Search and prints a timed summary.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from scour import core, fetch  # noqa: E402

C = "\033[36m"; G = "\033[32m"; Y = "\033[33m"; D = "\033[90m"; B = "\033[1m"; R = "\033[0m"


def banner(n, text):
    print(f"\n{C}{B}[{n}]{R} {B}{text}{R}")


def main():
    core.load_dotenv()
    url, region, profile = core.resolve_config()
    print(f"{B}Scour{R} — AWS Bedrock AgentCore Web Search  {D}(region {region}){R}")

    # 1) single search
    banner(1, 'single search:  scour "AWS Lambda pricing"')
    t = time.monotonic()
    one = asyncio.run(core.search_one(url, region, profile, "AWS Lambda pricing", 3))
    dt = time.monotonic() - t
    print(f"    {G}{len(one['results'])} results in {dt:.1f}s{R}")
    for r in one["results"][:2]:
        print(f"    {D}-{R} {r.get('title','')[:48]}")

    # 2) concurrent batch
    qs = ["AWS Lambda pricing", "Amazon S3 storage classes", "AWS re:Invent 2026",
          "Amazon Bedrock AgentCore", "AWS Lambda pricing"]   # last one duplicates
    banner(2, f"concurrent batch:  {len(qs)} queries at once")
    t = time.monotonic()
    batch = asyncio.run(core.search_batch(url, region, profile, qs, max_results=5,
                                          concurrency=5, rate_per_sec=10))
    dt = time.monotonic() - t
    raw = sum(q["count"] for q in batch["queries"])
    print(f"    {G}{raw} results → {batch['total']} after URL de-dup, in {dt:.1f}s{R}"
          f"  {D}(errors: {batch['errorCount']}){R}")

    # 3) research: search + fetch full text
    topic = "Amazon Bedrock AgentCore Web Search"
    banner(3, f'research:  "{topic}"  → search + fetch full text')
    t = time.monotonic()
    res = asyncio.run(fetch.research(url, region, profile, topic, max_results=5,
                                     fetch_full=True, max_chars=4000))
    dt = time.monotonic() - t
    print(f"    {G}{res['sourceCount']} sources, {res['fetched']} full-text fetched, "
          f"in {dt:.1f}s{R}")
    for s in res["sources"]:
        if s.get("text"):
            print(f"    {D}-{R} {(s.get('title') or s['url'])[:42]}  "
                  f"{Y}({len(s['text'])} chars){R}")

    print(f"\n{G}{B}✓ done{R}  {D}— cite url/title; concurrency capped to AgentCore quota{R}")


if __name__ == "__main__":
    main()
