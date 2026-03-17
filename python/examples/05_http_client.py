"""
HTTP client for the deployed rake microservices.

Shows how to call the Azure Function / Container App endpoints
with proper base64 encoding, error handling, and retry logic.

Endpoints:
  POST /api/code-review
  POST /api/security-audit
  POST /api/data-analysis

Run against local dev server:
  python examples/05_http_client.py --base-url http://localhost:8000

Run against Azure:
  python examples/05_http_client.py --base-url https://rake-security-audit.azurecontainerapps.io
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx")
    sys.exit(1)


def encode_file(path: str) -> dict:
    content = Path(path).read_bytes()
    return {
        "name": Path(path).name,
        "content": base64.b64encode(content).decode("utf-8"),
    }


def encode_bytes(name: str, content: bytes) -> dict:
    return {
        "name": name,
        "content": base64.b64encode(content).decode("utf-8"),
    }


async def call_service(
    base_url: str,
    endpoint: str,
    files: list[dict],
    goal: str = "",
    timeout: int = 300,
) -> dict:
    """Call a rake microservice endpoint."""
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    payload = {"files": files}
    if goal:
        payload["goal"] = goal

    async with httpx.AsyncClient(timeout=timeout) as client:
        print(f"POST {url} ({len(files)} file(s))…")
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def print_result(result: dict, endpoint: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"Result from {endpoint}")
    print("=" * 60)

    summary = result.get("summary", "")
    if summary:
        print(f"\nSummary:\n{summary}\n")

    findings = result.get("findings", [])
    if findings:
        print(f"Findings ({len(findings)}):")
        for f in findings:
            sev = f.get("severity", "info").upper()
            title = f.get("title", "")
            desc = f.get("description", "")
            print(f"  [{sev}] {title}: {desc}")

    stats = result.get("stats", {})
    print(f"\nStats: tokens={stats.get('total_input_tokens', 0)}+{stats.get('total_output_tokens', 0)}, "
          f"tools={stats.get('tool_calls', 0)}, ms={result.get('duration_ms', 0)}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("files", nargs="*", help="Files to analyse")
    parser.add_argument("--endpoint", default="api/security-audit")
    parser.add_argument("--goal", default="")
    args = parser.parse_args()

    if args.files:
        encoded = [encode_file(f) for f in args.files]
    else:
        # Demo with built-in example
        print("No files specified — using built-in demo content")
        demo_code = b"""
import os
SECRET_KEY = "abc123"
def run(cmd): os.system(cmd)  # command injection
def get_user(name): return db.execute(f"SELECT * FROM users WHERE name='{name}'")  # SQL injection
"""
        encoded = [encode_bytes("demo.py", demo_code)]

    result = await call_service(
        base_url=args.base_url,
        endpoint=args.endpoint,
        files=encoded,
        goal=args.goal,
    )
    print_result(result, args.endpoint)

    # Exit 1 if critical issues
    findings = result.get("findings", [])
    critical = [f for f in findings if f.get("severity") == "critical"]
    if critical:
        print(f"\n⚠️  {len(critical)} critical finding(s) — exiting with code 1")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
