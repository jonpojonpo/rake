"""
Azure Function: Data Analysis Service
=======================================

Triggers:
  HTTP POST /api/data-analysis
  Blob trigger: new CSV/JSON files in "data-uploads" container

Use cases:
  - Profile CSV datasets: column types, distributions, null rates, anomalies
  - Analyse JSON configs and API responses for structure and data quality
  - Generate human-readable data quality reports

HTTP request body (JSON):
  {
    "files": [{"name": "sales.csv", "content": "<base64>"}],
    "goal": "optional analysis goal",
    "output_format": "summary|detailed",    // default: detailed
    "job_id": "uuid"
  }

Blob trigger: monitors "data-uploads/{name}" — results go to
  "data-results/{name}/{timestamp}_{job_id}.json"
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid

import azure.functions as func  # type: ignore

try:
    from rake_sdk import RakeClient, RakeConfig
    from services.shared.telemetry import track_analysis
    from services.shared.storage import upload_result, result_blob_name
    _SDK_AVAILABLE = True
except ImportError as _e:
    logging.warning("rake_sdk import failed: %s", _e)
    _SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# ── HTTP trigger ──────────────────────────────────────────────────────────────

@app.route(route="data-analysis", methods=["POST"])
async def data_analysis_http(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _error("Request body must be valid JSON", 400)

    job_id = body.get("job_id") or str(uuid.uuid4())
    result_dict = await _run_analysis(body, job_id)

    if "error" in result_dict and result_dict.get("status", 200) >= 400:
        return _error(result_dict["error"], result_dict["status"])

    return func.HttpResponse(
        json.dumps(result_dict),
        status_code=200,
        mimetype="application/json",
    )


# ── Blob trigger: auto-profile on upload ─────────────────────────────────────

@app.blob_trigger(
    arg_name="blob",
    path="data-uploads/{name}",
    connection="AZURE_STORAGE_CONNECTION_STRING",
)
async def data_analysis_blob(blob: func.InputStream) -> None:
    """Automatically profile any CSV/JSON file dropped in data-uploads."""
    blob_name = blob.name
    job_id = str(uuid.uuid4())
    file_name = blob_name.split("/")[-1]

    # Only process data files
    if not any(file_name.endswith(ext) for ext in (".csv", ".json", ".jsonl", ".tsv")):
        logger.info("Skipping non-data file: %s", file_name)
        return

    logger.info("Auto-profiling '%s' (job %s)", blob_name, job_id)
    content = blob.read()

    body = {
        "files": [{"name": file_name, "content": base64.b64encode(content).decode()}],
        "job_id": job_id,
    }

    result_dict = await _run_analysis(body, job_id)

    container = os.environ.get("DATA_RESULTS_CONTAINER", "data-results")
    out_blob = result_blob_name(blob_name, job_id)
    await upload_result(container, out_blob, result_dict, metadata={"source_blob": blob_name})
    logger.info("Wrote data profile to %s/%s", container, out_blob)


# ── Core logic ────────────────────────────────────────────────────────────────

async def _run_analysis(body: dict, job_id: str) -> dict:
    if not _SDK_AVAILABLE:
        return {"error": "rake_sdk not available", "status": 503}

    named_files = _decode_files(body.get("files", []))
    if not named_files:
        return {"error": "No files provided", "status": 400}

    output_format = body.get("output_format", "detailed")
    detail_instruction = (
        "Provide exhaustive statistics for every column."
        if output_format == "detailed"
        else "Provide a concise executive summary."
    )

    goal = body.get("goal") or (
        f"Profile these data files and produce a data quality report. "
        f"For each file report: "
        f"(1) Schema — column names, inferred types, sample values. "
        f"(2) Shape — row count, column count, estimated memory. "
        f"(3) Quality — null/empty rates per column, duplicate rows, outliers. "
        f"(4) Distributions — for numeric columns: min, max, mean, std, percentiles (p25/p50/p75/p95). "
        f"(5) Categorical — top 10 values and their frequencies for string columns. "
        f"(6) Anomalies — unexpected values, format violations, schema inconsistencies. "
        f"(7) Recommendations — what cleaning or transformation steps are needed. "
        f"{detail_instruction}"
    )

    config = RakeConfig(
        llm=body.get("llm", os.environ.get("RAKE_LLM", "claude")),
        model=body.get("model") or os.environ.get("RAKE_MODEL"),
        api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("RAKE_BASE_URL"),
        # csv_stats and json_query require read tool
        tools=["read", "grep"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "180")),
    )

    t0 = time.monotonic()
    try:
        async with RakeClient(config) as client:
            result = await client.analyze_bytes(named_files=named_files, goal=goal)
    except Exception as exc:
        logger.exception("rake data analysis failed for job %s", job_id)
        return {"error": str(exc), "job_id": job_id, "status": 500}

    duration_ms = int((time.monotonic() - t0) * 1000)

    track_analysis(
        result,
        service="data-analysis",
        llm=config.llm,
        model=config.model or "default",
        duration_ms=duration_ms,
        job_id=job_id,
    )

    out = result.to_dict()
    out["job_id"] = job_id
    out["duration_ms"] = duration_ms
    # Strip findings for data analysis — not relevant for data profiling
    out.pop("findings", None)
    return out


def _decode_files(file_list: list[dict]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for item in file_list:
        name = item.get("name", "data.csv")
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
