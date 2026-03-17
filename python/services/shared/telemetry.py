"""
Azure Application Insights telemetry helpers for rake microservices.

Emits:
  - Custom events for each analysis run
  - Dependency telemetry for the rake subprocess call
  - Structured properties: model, backend, token counts, finding severity counts
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from rake_sdk.models import RakeResult

logger = logging.getLogger(__name__)


def _get_tc():
    """Return an opencensus/applicationinsights TelemetryClient if configured."""
    ikey = os.environ.get("APPINSIGHTS_INSTRUMENTATIONKEY")
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not ikey and not conn:
        return None
    try:
        from applicationinsights import TelemetryClient  # type: ignore
        client = TelemetryClient(ikey or _parse_ikey(conn))
        return client
    except ImportError:
        logger.debug("applicationinsights package not installed — telemetry disabled")
        return None


def _parse_ikey(conn_str: str) -> str:
    for part in conn_str.split(";"):
        if part.startswith("InstrumentationKey="):
            return part[len("InstrumentationKey="):]
    return conn_str


def track_analysis(
    result: "RakeResult",
    *,
    service: str,
    llm: str,
    model: str,
    duration_ms: int,
    job_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """
    Emit a custom 'RakeAnalysis' event to Application Insights.
    Also logs a summary line at INFO level regardless of telemetry availability.
    """
    props = {
        "service": service,
        "llm": llm,
        "model": model,
        "job_id": job_id or "unknown",
        "files_analyzed": str(len(result.files_analyzed)),
        "tool_calls": str(result.tool_calls),
        "findings_total": str(len(result.findings)),
        "findings_critical": str(len(result.critical_findings)),
        "findings_high": str(len(result.high_findings)),
        **(extra or {}),
    }
    metrics = {
        "input_tokens": result.total_input_tokens,
        "output_tokens": result.total_output_tokens,
        "llm_ms": result.total_llm_ms,
        "duration_ms": duration_ms,
    }

    logger.info(
        "rake analysis complete | service=%s llm=%s model=%s files=%d "
        "findings=%d critical=%d tokens_in=%d tokens_out=%d ms=%d",
        service, llm, model,
        len(result.files_analyzed),
        len(result.findings),
        len(result.critical_findings),
        result.total_input_tokens,
        result.total_output_tokens,
        duration_ms,
    )

    tc = _get_tc()
    if tc:
        tc.track_event("RakeAnalysis", properties=props, measurements=metrics)
        tc.flush()


@contextmanager
def timed_span(name: str):
    """Simple context manager to measure wall-clock ms."""
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.debug("%s took %dms", name, elapsed_ms)
