"""
Document analysis demo — shows how rake indexes and navigates large docs.

Demonstrates:
  1. A synthetic 'annual report' markdown document with sections and tables
  2. The preprocessor generating _index.md and extracting tables as CSV
  3. The LLM using read_section() to navigate without loading the full file
  4. csv_stats() on extracted tables

Run:
  ANTHROPIC_API_KEY=sk-ant-... python examples/06_document_analysis.py
  RAKE_LLM=noop python examples/06_document_analysis.py   # dry run
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rake_sdk import RakeClient, RakeConfig
from rake_sdk.preprocessors import preprocess_files


# ── Synthetic annual report (500 lines) ──────────────────────────────────────

ANNUAL_REPORT_MD = b"""# Annual Report 2024 — Acme Corporation

## Chairman's Statement

Dear Shareholders,

This has been a transformative year for Acme Corporation. Despite challenging
macroeconomic conditions, we delivered record revenue and continued to invest
in our long-term growth initiatives.

Our core business grew 18% year-on-year, driven by strong demand in our
Cloud Services division. We completed the acquisition of DataSoft Ltd in Q2,
which is already contributing meaningfully to our AI product suite.

Looking ahead to 2025, we are confident in our ability to sustain double-digit
growth while improving operating margins through operational efficiency programmes.

I would like to thank our 12,000 employees, customers, and shareholders for
their continued trust and support.

*Sir Jonathan Blake*
*Chairman*

## Chief Executive's Review

### Market Position

We entered 2024 as the #3 player in our core market and ended the year having
gained two percentage points of market share to reach 14.2%.

### Strategic Priorities

1. Accelerate Cloud Services growth
2. Expand AI capabilities through acquisition and R&D
3. Improve operational efficiency (target: 200 bps margin improvement)
4. Geographic expansion into Southeast Asia

## Financial Highlights

| Metric | 2024 | 2023 | Change |
|--------|------|------|--------|
| Revenue ($m) | 2,847 | 2,415 | +17.9% |
| Gross Profit ($m) | 1,423 | 1,185 | +20.1% |
| EBITDA ($m) | 712 | 580 | +22.8% |
| Net Profit ($m) | 498 | 389 | +28.0% |
| EPS (cents) | 124.5 | 97.3 | +28.0% |
| Dividend (cents) | 35.0 | 28.0 | +25.0% |
| Free Cash Flow ($m) | 523 | 401 | +30.4% |

## Segment Results

### Cloud Services

Cloud Services revenue grew 34% to $1,421m, representing 50% of group revenue
(2023: 44%). EBITDA margin expanded to 28.4% (2023: 25.1%).

| Segment | Revenue 2024 | Revenue 2023 | Growth |
|---------|-------------|-------------|--------|
| Cloud Services | 1,421 | 1,061 | +34.0% |
| Enterprise Software | 876 | 823 | +6.4% |
| Professional Services | 550 | 531 | +3.6% |
| Group Total | 2,847 | 2,415 | +17.9% |

### Enterprise Software

Enterprise Software delivered steady growth of 6.4%, with renewal rates
improving to 94.2% (2023: 91.8%).

### Professional Services

Professional Services grew 3.6%, constrained by consultant capacity.
We are actively hiring to address this bottleneck.

## Balance Sheet Summary

| Item | Dec 2024 ($m) | Dec 2023 ($m) |
|------|--------------|--------------|
| Total Assets | 4,218 | 3,512 |
| Cash & Equivalents | 892 | 623 |
| Total Debt | 1,245 | 980 |
| Net Debt | 353 | 357 |
| Total Equity | 2,134 | 1,821 |

Net debt to EBITDA improved to 0.50x (2023: 0.62x).

## Risk Factors

### Principal Risks

1. **Cybersecurity risk** — We hold sensitive customer data. A breach could
   cause financial loss, regulatory penalties, and reputational damage.
   Mitigation: ISO 27001 certified, third-party penetration testing quarterly.

2. **Talent retention** — Competition for engineering and AI talent is intense.
   Mitigation: Competitive compensation, equity schemes, and flexible working.

3. **Regulatory / AI compliance** — New AI regulations in EU and US may require
   product changes. Mitigation: Dedicated regulatory affairs team established.

4. **Customer concentration** — Top 10 customers represent 28% of revenue.
   Mitigation: Active diversification programme; no single customer > 4%.

5. **Integration risk** — DataSoft acquisition integration is complex.
   Mitigation: Dedicated PMO with monthly board review.

## Auditor's Report

### Independent Auditor's Report to the Members of Acme Corporation

**Opinion**

In our opinion, the financial statements give a true and fair view of the
state of the group's affairs and of its profit for the year then ended.

**Key Audit Matters**

1. Goodwill impairment assessment ($892m)
2. Revenue recognition on multi-element arrangements
3. Acquisition accounting for DataSoft Ltd

No emphasis of matter paragraphs required.

*Ernst & Young LLP*
*Statutory Auditor*
"""


async def main():
    print("=" * 60)
    print("rake Document Analysis Demo")
    print("=" * 60)

    # ── Show what the preprocessor generates ─────────────────────────────────
    print("\n1. Running document preprocessor on synthetic annual report...")

    raw_files = {"annual_report_2024.md": ANNUAL_REPORT_MD}
    processed = preprocess_files(raw_files)

    print(f"\n   Input files: {list(raw_files.keys())}")
    print(f"   After preprocessing: {list(processed.keys())}")

    for name, content in processed.items():
        size = len(content)
        if name.endswith("._index.md"):
            print(f"\n   --- {name} ({size} bytes) ---")
            print(content.decode("utf-8"))
        elif name.endswith(".csv"):
            print(f"\n   --- {name} ({size} bytes, first 200 chars) ---")
            print(content.decode("utf-8")[:200])

    # ── Run rake analysis ──────────────────────────────────────────────────────
    print("\n2. Running rake analysis...")
    prompt_path = Path(__file__).parent.parent / "prompts" / "annual_report.txt"

    config = RakeConfig(
        llm=os.environ.get("RAKE_LLM", "claude"),
        model=os.environ.get("RAKE_MODEL"),
        tools=["read", "grep", "write"],
        timeout=300,
        preprocess=True,
    )

    system = prompt_path.read_text() if prompt_path.exists() else None

    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files=raw_files,  # client preprocesses automatically
            goal=(
                "Analyse this annual report. Use the _index.md to navigate sections. "
                "Extract key financial metrics, segment performance, and risk factors. "
                "Use csv_stats() on the extracted table CSVs. "
                "Do NOT call read_file() on the main document."
            ),
            system=system,
        )

    print("\n" + "=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)
    print(f"\nSummary:\n{result.summary}")
    print(f"\nStats: {result.tool_calls} tool calls, "
          f"{result.total_input_tokens}↑ / {result.total_output_tokens}↓ tokens, "
          f"{result.total_llm_ms}ms LLM time")


if __name__ == "__main__":
    asyncio.run(main())
