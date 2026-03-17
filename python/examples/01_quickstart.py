"""
Quickstart: Analyse a local file with the rake Python SDK.

Run:
  ANTHROPIC_API_KEY=sk-ant-... python examples/01_quickstart.py
"""

import asyncio
import sys
from pathlib import Path

# Allow running from the python/ directory
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rake_sdk import RakeClient, RakeConfig


async def main():
    # Point at the fixture app.py that ships with rake
    target = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "sample" / "app.py"
    if not target.exists():
        print(f"File not found: {target}")
        sys.exit(1)

    config = RakeConfig(llm="claude", timeout=120)

    async with RakeClient(config) as client:
        print(f"Analysing {target.name} …")
        result = await client.security_audit(files=[target])

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(result.summary)

    print("\n" + "=" * 60)
    print(f"FINDINGS ({len(result.findings)} total)")
    print("=" * 60)
    for f in result.findings:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}.get(f.severity.value, "•")
        loc = f" [{f.file}:{f.line}]" if f.file else ""
        print(f"  {icon} [{f.severity.value.upper()}]{loc} {f.title}")

    print(f"\nTokens in/out: {result.total_input_tokens}/{result.total_output_tokens}")
    print(f"Tool calls: {result.tool_calls}")
    print(f"LLM time: {result.total_llm_ms}ms")

    sys.exit(1 if result.has_critical_issues else 0)


if __name__ == "__main__":
    asyncio.run(main())
