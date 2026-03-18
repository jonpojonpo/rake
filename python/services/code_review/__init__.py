"""
Azure Function: Code Review Service
====================================

Triggers:
  HTTP POST /api/code-review          — direct file upload (multipart or JSON body)
  Azure Service Bus queue "code-review-jobs"  — async job processing

Request body (JSON):
  {
    "files": [
      {"name": "app.py", "content": "<base64-encoded>"},
      ...
    ],
    "goal": "optional custom goal string",
    "llm": "claude",               // optional, default: claude
    "model": "claude-sonnet-4-6",  // optional
    "pr_url": "https://...",       // optional, attached to result metadata
    "job_id": "uuid"               // optional
  }

Response (JSON):
  {
    "job_id": "...",
    "summary": "...",
    "findings": [...],
    "stats": {...},
    "blob_url": "..."   // if RESULTS_CONTAINER configured
  }
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from typing import Optional

import azure.functions as func  # type: ignore

# Lazy imports so the function still loads if SDK extras aren't installed
try:
    from rake_sdk import RakeClient, RakeConfig
    from services.shared.telemetry import track_analysis
    from services.shared.storage import upload_result, result_blob_name
    _SDK_AVAILABLE = True
except ImportError as _e:
    logging.warning("rake_sdk import failed: %s", _e)
    _SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Azure Function entry point ────────────────────────────────────────────────

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(route="code-review", methods=["POST"])
async def code_review_http(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP trigger: synchronous code review."""
    try:
        body = req.get_json()
    except ValueError:
        return _error("Request body must be valid JSON", 400)

    job_id = body.get("job_id") or str(uuid.uuid4())
    result_dict = await _run_review(body, job_id)

    if "error" in result_dict:
        return _error(result_dict["error"], result_dict.get("status", 500))

    return func.HttpResponse(
        json.dumps(result_dict),
        status_code=200,
        mimetype="application/json",
    )


@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="code-review-jobs",
    connection="SERVICE_BUS_CONNECTION",
)
async def code_review_queue(msg: func.ServiceBusMessage) -> None:
    """Service Bus trigger: async code review from queue message."""
    body = json.loads(msg.get_body().decode("utf-8"))
    job_id = body.get("job_id") or str(uuid.uuid4())
    logger.info("Processing code-review job %s", job_id)

    result_dict = await _run_review(body, job_id)

    # Write result to blob regardless of success/failure
    container = os.environ.get("RESULTS_CONTAINER", "rake-results")
    blob_name = result_blob_name(f"code-review/{job_id}", job_id)
    await upload_result(container, blob_name, result_dict, metadata={"job_id": job_id})
    logger.info("Wrote result to %s/%s", container, blob_name)


# ── Core analysis logic ───────────────────────────────────────────────────────

async def _run_review(body: dict, job_id: str) -> dict:
    if not _SDK_AVAILABLE:
        return {"error": "rake_sdk not available", "status": 503}

    named_files = _decode_files(body.get("files", []))
    if not named_files:
        return {"error": "No files provided", "status": 400}

    goal = body.get("goal") or (
        "Review this code for correctness, bugs, error handling, performance, "
        "and code quality. For each issue label it [HIGH], [MEDIUM], or [LOW]. "
        "Include filename and line number where relevant."
    )

    config = RakeConfig(
        llm=body.get("llm", os.environ.get("RAKE_LLM", "claude")),
        model=body.get("model") or os.environ.get("RAKE_MODEL"),
        api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("RAKE_BASE_URL"),
        tools=["read", "grep"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "180")),
    )

    t0 = time.monotonic()
    try:
        async with RakeClient(config) as client:
            result = await client.analyze_bytes(named_files=named_files, goal=goal)
    except Exception as exc:
        logger.exception("rake failed for job %s", job_id)
        return {"error": str(exc), "job_id": job_id, "status": 500}

    duration_ms = int((time.monotonic() - t0) * 1000)

    track_analysis(
        result,
        service="code-review",
        llm=config.llm,
        model=config.model or "default",
        duration_ms=duration_ms,
        job_id=job_id,
        extra={"pr_url": body.get("pr_url", "")},
    )

    out = result.to_dict()
    out["job_id"] = job_id
    out["duration_ms"] = duration_ms
    return out


def _decode_files(file_list: list[dict]) -> dict[str, bytes]:
    """Decode base64-encoded file payloads from the request body."""
    out: dict[str, bytes] = {}
    for item in file_list:
        name = item.get("name", "file.txt")
        content = item.get("content", "")
        if isinstance(content, str):
            try:
                out[name] = base64.b64decode(content)
            except Exception:
                out[name] = content.encode("utf-8")
        elif isinstance(content, bytes):
            out[name] = content
    return out


def _error(msg: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": msg}),
        status_code=status,
        mimetype="application/json",
    )
