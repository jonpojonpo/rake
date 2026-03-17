"""
Azure Function: Security Audit Service
========================================

Triggers:
  HTTP POST /api/security-audit
  Blob trigger: new files uploaded to "uploads" container
  Service Bus queue "security-audit-jobs"

Blob trigger pattern: uploads/{name}

When a file lands in the "uploads" container, this function automatically
runs a security audit and writes findings to "results/{name}/audit.json".
High or Critical findings optionally emit an alert to an output queue
"security-alerts" for downstream alerting pipelines.

HTTP request body (JSON):
  {
    "files": [{"name": "app.py", "content": "<base64>"}],
    "goal": "optional override",
    "severity_threshold": "high",   // min severity to flag: critical|high|medium
    "notify_on_critical": true,
    "job_id": "uuid"
  }
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
    from rake_sdk.models import FindingSeverity
    from services.shared.telemetry import track_analysis
    from services.shared.storage import (
        download_blob_bytes,
        upload_result,
        result_blob_name,
    )
    _SDK_AVAILABLE = True
except ImportError as _e:
    logging.warning("rake_sdk import failed: %s", _e)
    _SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = [
    FindingSeverity.INFO,
    FindingSeverity.LOW,
    FindingSeverity.MEDIUM,
    FindingSeverity.HIGH,
    FindingSeverity.CRITICAL,
]

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# ── HTTP trigger ──────────────────────────────────────────────────────────────

@app.route(route="security-audit", methods=["POST"])
async def security_audit_http(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _error("Request body must be valid JSON", 400)

    job_id = body.get("job_id") or str(uuid.uuid4())
    result_dict = await _run_audit(body, job_id)

    if "error" in result_dict and result_dict.get("status", 200) >= 400:
        return _error(result_dict["error"], result_dict["status"])

    return func.HttpResponse(
        json.dumps(result_dict),
        status_code=200,
        mimetype="application/json",
    )


# ── Blob trigger: auto-audit on upload ────────────────────────────────────────

@app.blob_trigger(
    arg_name="blob",
    path="uploads/{name}",
    connection="AZURE_STORAGE_CONNECTION_STRING",
)
@app.queue_output(
    arg_name="alerts",
    queue_name="security-alerts",
    connection="AZURE_STORAGE_CONNECTION_STRING",
)
async def security_audit_blob(blob: func.InputStream, alerts: func.Out[str]) -> None:
    """
    Automatically audit any file uploaded to the 'uploads' container.
    Emits to 'security-alerts' queue when critical/high findings are found.
    """
    blob_name = blob.name
    job_id = str(uuid.uuid4())
    logger.info("Auto-auditing blob '%s' (job %s)", blob_name, job_id)

    content = blob.read()
    file_name = blob_name.split("/")[-1]

    body = {
        "files": [{"name": file_name, "content": base64.b64encode(content).decode()}],
        "notify_on_critical": True,
        "job_id": job_id,
    }

    result_dict = await _run_audit(body, job_id)

    # Write result to blob storage
    container = os.environ.get("RESULTS_CONTAINER", "rake-results")
    out_blob = result_blob_name(blob_name, job_id)
    await upload_result(container, out_blob, result_dict, metadata={"source_blob": blob_name})

    # Emit alert if high/critical findings
    findings = result_dict.get("findings", [])
    critical = [f for f in findings if f.get("severity") in ("critical", "high")]
    if critical:
        alert_payload = json.dumps({
            "job_id": job_id,
            "source_blob": blob_name,
            "critical_count": len([f for f in findings if f.get("severity") == "critical"]),
            "high_count": len([f for f in findings if f.get("severity") == "high"]),
            "top_findings": critical[:5],
            "result_blob": out_blob,
        })
        alerts.set(alert_payload)
        logger.warning(
            "SECURITY ALERT: %d critical/high findings in '%s'",
            len(critical), blob_name
        )


# ── Service Bus trigger ───────────────────────────────────────────────────────

@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="security-audit-jobs",
    connection="SERVICE_BUS_CONNECTION",
)
async def security_audit_queue(msg: func.ServiceBusMessage) -> None:
    body = json.loads(msg.get_body().decode("utf-8"))
    job_id = body.get("job_id") or str(uuid.uuid4())
    result_dict = await _run_audit(body, job_id)
    container = os.environ.get("RESULTS_CONTAINER", "rake-results")
    blob_name = result_blob_name(f"security-audit/{job_id}", job_id)
    await upload_result(container, blob_name, result_dict, metadata={"job_id": job_id})


# ── Core logic ────────────────────────────────────────────────────────────────

async def _run_audit(body: dict, job_id: str) -> dict:
    if not _SDK_AVAILABLE:
        return {"error": "rake_sdk not available", "status": 503}

    named_files = _decode_files(body.get("files", []))
    if not named_files:
        return {"error": "No files provided", "status": 400}

    goal = body.get("goal") or (
        "Perform a thorough security audit. Identify ALL security vulnerabilities including: "
        "hardcoded credentials/secrets/tokens, SQL injection, command injection, "
        "path traversal, XSS, insecure deserialization, broken authentication, "
        "sensitive data exposure, cryptographic weaknesses, and OWASP Top 10 issues. "
        "For every finding output a line starting with '- [CRITICAL]', '- [HIGH]', "
        "'- [MEDIUM]', '- [LOW]', or '- [INFO]' followed by the issue title and description. "
        "Include filename and line number where known."
    )

    config = RakeConfig(
        llm=body.get("llm", os.environ.get("RAKE_LLM", "claude")),
        model=body.get("model") or os.environ.get("RAKE_MODEL"),
        api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("RAKE_BASE_URL"),
        tools=["read", "grep"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "240")),
    )

    t0 = time.monotonic()
    try:
        async with RakeClient(config) as client:
            result = await client.analyze_bytes(named_files=named_files, goal=goal)
    except Exception as exc:
        logger.exception("rake security audit failed for job %s", job_id)
        return {"error": str(exc), "job_id": job_id, "status": 500}

    duration_ms = int((time.monotonic() - t0) * 1000)

    track_analysis(
        result,
        service="security-audit",
        llm=config.llm,
        model=config.model or "default",
        duration_ms=duration_ms,
        job_id=job_id,
    )

    out = result.to_dict()
    out["job_id"] = job_id
    out["duration_ms"] = duration_ms
    out["has_critical_issues"] = result.has_critical_issues

    # Apply severity threshold filtering if requested
    threshold_str = body.get("severity_threshold", "info")
    try:
        threshold = FindingSeverity(threshold_str.lower())
        threshold_idx = _SEVERITY_ORDER.index(threshold)
        out["findings"] = [
            f for f in out["findings"]
            if _SEVERITY_ORDER.index(FindingSeverity(f["severity"])) >= threshold_idx
        ]
    except (ValueError, KeyError):
        pass  # Keep all findings on bad threshold value

    return out


def _decode_files(file_list: list[dict]) -> dict[str, bytes]:
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
