"""
Analyse in-memory content — no files on disk needed.

Useful when you have file content from:
  - Azure Blob Storage downloads
  - GitHub API responses
  - Database blobs
  - HTTP responses

Run:
  ANTHROPIC_API_KEY=sk-ant-... python examples/02_analyze_bytes.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rake_sdk import RakeClient, RakeConfig

EXAMPLE_CODE = b"""
import os
import sqlite3

SECRET_KEY = "super-secret-hardcoded-key-123"

def get_user(username):
    conn = sqlite3.connect('users.db')
    # SQL injection vulnerability
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor = conn.execute(query)
    return cursor.fetchone()

def run_report(report_name):
    # Command injection vulnerability
    os.system(f"generate_report.sh {report_name}")

def process_data(data):
    # Pickle deserialization of untrusted data
    import pickle
    return pickle.loads(data)
"""

EXAMPLE_CONFIG = b"""
{
  "database": {
    "host": "prod-db.internal",
    "port": 5432,
    "password": "P@ssw0rd!",
    "ssl": false
  },
  "api": {
    "secret": "sk-prod-abc123xyz",
    "debug": true,
    "allowed_hosts": ["*"]
  }
}
"""


async def main():
    config = RakeConfig(llm="claude", timeout=120)

    named_files = {
        "api_handler.py": EXAMPLE_CODE,
        "config.json": EXAMPLE_CONFIG,
    }

    async with RakeClient(config) as client:
        print("Running security audit on in-memory files…")
        result = await client.analyze_bytes(
            named_files=named_files,
            goal=(
                "Find ALL security vulnerabilities. "
                "Label each finding [CRITICAL], [HIGH], [MEDIUM], [LOW], or [INFO]."
            ),
        )

    print(f"\nSummary:\n{result.summary}\n")
    print(f"Found {len(result.findings)} findings:")
    for f in result.findings:
        print(f"  [{f.severity.value.upper()}] {f.title}")

    if result.has_critical_issues:
        print("\n⚠️  CRITICAL issues found — build should fail!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
