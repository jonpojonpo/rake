"""
Azure Blob Storage pipeline example.

Shows a complete event-driven pattern:
  1. Upload source files to Azure Blob Storage (simulated)
  2. Download them with the Azure SDK
  3. Run rake security audit
  4. Upload structured results back to Blob Storage
  5. Emit alert if critical findings

In production this logic is triggered by an Azure Blob trigger in
services/security_audit/__init__.py — this script shows the same
pipeline wired together manually.

Prerequisites:
  pip install azure-storage-blob
  export AZURE_STORAGE_CONNECTION_STRING="..."
  export ANTHROPIC_API_KEY="..."

Run:
  python examples/03_azure_blob_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rake_sdk import RakeClient, RakeConfig


async def main():
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        print("AZURE_STORAGE_CONNECTION_STRING not set — using mock mode")
        await run_mock()
        return

    from azure.storage.blob import BlobServiceClient

    svc = BlobServiceClient.from_connection_string(conn_str)

    # Ensure containers exist
    for container in ("uploads", "rake-results"):
        try:
            svc.create_container(container)
        except Exception:
            pass  # already exists

    # Upload a test file
    job_id = str(uuid.uuid4())[:8]
    source_name = f"uploads/test_app_{job_id}.py"
    test_content = b"""
import os
SECRET = "hardcoded-secret-value"
def cmd(x): os.system(f"ls {x}")  # command injection
"""
    svc.get_blob_client("uploads", f"test_app_{job_id}.py").upload_blob(test_content, overwrite=True)
    print(f"Uploaded test file to blob: {source_name}")

    # Download it
    blob_client = svc.get_blob_client("uploads", f"test_app_{job_id}.py")
    content = blob_client.download_blob().readall()

    # Run rake audit
    config = RakeConfig(llm="claude", timeout=120)
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files={f"test_app_{job_id}.py": content},
            goal="Security audit — find all vulnerabilities. Label each [CRITICAL], [HIGH], [MEDIUM], [LOW], [INFO].",
        )

    # Upload result
    result_json = json.dumps(result.to_dict(), indent=2)
    result_blob = f"rake-results/test_app_{job_id}/audit.json"
    svc.get_blob_client("rake-results", f"test_app_{job_id}/audit.json").upload_blob(
        result_json.encode(), overwrite=True,
    )
    print(f"Uploaded results to: {result_blob}")
    print(f"Findings: {len(result.findings)} ({len(result.critical_findings)} critical, {len(result.high_findings)} high)")

    if result.has_critical_issues:
        print("\n⚠️  CRITICAL issues found — would emit to security-alerts queue")


async def run_mock():
    """Demo without Azure Storage."""
    config = RakeConfig(llm="noop", timeout=30)
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files={"demo.py": b"SECRET='abc'\ndef f(x): import os; os.system(x)"},
            goal="Find security issues.",
        )
    print(f"[Mock] Summary: {result.summary or '(noop backend)'}")
    print("[Mock] Pipeline would: download blob → analyze → upload results → alert if critical")


if __name__ == "__main__":
    asyncio.run(main())
