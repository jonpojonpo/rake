"""
FastAPI dev server that mirrors the Azure Function endpoints for local testing.

Endpoints:
  POST /api/code-review       — code quality review
  POST /api/security-audit    — security vulnerability scan
  POST /api/data-analysis     — data profiling
  GET  /health                — health check

Run:
  uvicorn local_server:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Allow importing the SDK from the repo
sys.path.insert(0, str(Path(__file__).parent.parent))

from rake_sdk import RakeClient, RakeConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="rake Local Dev Server",
    description="Local mirror of the rake Azure Function microservices",
    version="0.1.0",
)


# ── Request/Response models ───────────────────────────────────────────────────

class FilePayload(BaseModel):
    name: str
    content: str  # base64-encoded


class AnalysisRequest(BaseModel):
    files: list[FilePayload]
    goal: Optional[str] = None
    llm: Optional[str] = None
    model: Optional[str] = None
    job_id: Optional[str] = None


class SecurityAuditRequest(AnalysisRequest):
    severity_threshold: str = "info"
    notify_on_critical: bool = False


class DataAnalysisRequest(AnalysisRequest):
    output_format: str = "detailed"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _decode_files(file_list: list[FilePayload]) -> dict[str, bytes]:
    out = {}
    for f in file_list:
        try:
            out[f.name] = base64.b64decode(f.content)
        except Exception:
            out[f.name] = f.content.encode("utf-8")
    return out


def _make_config(req: AnalysisRequest) -> RakeConfig:
    return RakeConfig(
        llm=req.llm or os.environ.get("RAKE_LLM", "claude"),
        model=req.model or os.environ.get("RAKE_MODEL"),
        api_key=(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        ),
        base_url=os.environ.get("RAKE_BASE_URL"),
        tools=["read", "grep"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "240")),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "rake-local-dev"}


@app.post("/api/code-review")
async def code_review(req: AnalysisRequest):
    job_id = req.job_id or str(uuid.uuid4())
    named_files = _decode_files(req.files)
    if not named_files:
        raise HTTPException(400, "No files provided")

    goal = req.goal or (
        "Review this code for correctness, bugs, error handling, performance, "
        "and code quality. For each issue label it [HIGH], [MEDIUM], or [LOW]. "
        "Include filename and line number where relevant."
    )

    config = _make_config(req)
    t0 = time.monotonic()
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(named_files=named_files, goal=goal)

    out = result.to_dict()
    out["job_id"] = job_id
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return JSONResponse(out)


@app.post("/api/security-audit")
async def security_audit(req: SecurityAuditRequest):
    job_id = req.job_id or str(uuid.uuid4())
    named_files = _decode_files(req.files)
    if not named_files:
        raise HTTPException(400, "No files provided")

    goal = req.goal or (
        "Perform a thorough security audit. Identify ALL security vulnerabilities including: "
        "hardcoded credentials/secrets/tokens, SQL injection, command injection, "
        "path traversal, XSS, insecure deserialization, broken authentication, "
        "sensitive data exposure, cryptographic weaknesses, and OWASP Top 10 issues. "
        "For every finding output a line starting with '- [CRITICAL]', '- [HIGH]', "
        "'- [MEDIUM]', '- [LOW]', or '- [INFO]' followed by the issue title and description."
    )

    config = _make_config(req)
    t0 = time.monotonic()
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(named_files=named_files, goal=goal)

    out = result.to_dict()
    out["job_id"] = job_id
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    out["has_critical_issues"] = result.has_critical_issues
    return JSONResponse(out)


@app.post("/api/data-analysis")
async def data_analysis(req: DataAnalysisRequest):
    job_id = req.job_id or str(uuid.uuid4())
    named_files = _decode_files(req.files)
    if not named_files:
        raise HTTPException(400, "No files provided")

    goal = req.goal or (
        "Profile these data files and produce a data quality report. "
        "For each file report: schema, shape, null rates, numeric distributions "
        "(min/max/mean/p95), top categorical values, anomalies, and recommendations."
    )

    config = _make_config(req)
    t0 = time.monotonic()
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(named_files=named_files, goal=goal)

    out = result.to_dict()
    out.pop("findings", None)
    out["job_id"] = job_id
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return JSONResponse(out)


@app.post("/api/upload-and-review")
async def upload_and_review(
    files: list[UploadFile] = File(...),
    goal: str = "",
    llm: str = "claude",
):
    """Convenience endpoint: upload raw files instead of base64 JSON."""
    named_files: dict[str, bytes] = {}
    for f in files:
        content = await f.read()
        named_files[f.filename or "upload.txt"] = content

    config = RakeConfig(
        llm=llm,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        tools=["read", "grep"],
        timeout=240,
    )
    effective_goal = goal or "Analyse these files. Find bugs and security issues."
    t0 = time.monotonic()
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(named_files=named_files, goal=effective_goal)

    out = result.to_dict()
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return JSONResponse(out)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
