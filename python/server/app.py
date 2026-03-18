"""
rake FastAPI server — S3-backed document & data analysis.

Endpoints:
  POST /analyse          — analyse files (base64 or multipart upload)
  POST /analyse/s3       — analyse a file already in S3
  GET  /jobs/{job_id}    — fetch result for a completed job
  GET  /health           — liveness check

How output files work:
  1. During analysis the LLM calls write_file() to produce reports,
     extracted CSVs, edited documents, etc.
  2. After the run, those files are pulled from the rake sandbox's
     scratch space and uploaded to S3.
  3. The response includes a `downloads` list with presigned S3 URLs
     that are valid for S3_PRESIGN_TTL_SECONDS (default 1 hour).

Environment variables (see .env.example):
  ANTHROPIC_API_KEY / OPENAI_API_KEY
  RAKE_LLM, RAKE_MODEL, RAKE_TIMEOUT
  S3_RESULTS_BUCKET, S3_RESULTS_PREFIX, S3_PRESIGN_TTL_SECONDS
  AWS_DEFAULT_REGION, AWS_ENDPOINT_URL (LocalStack / MinIO)
"""
from __future__ import annotations

import base64
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Allow running from repo root or docker
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from rake_sdk import RakeClient, RakeConfig
from rake_sdk.preprocessors import preprocess_files

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rake.server")

app = FastAPI(
    title="rake Document Analysis API",
    description=(
        "Analyse documents and data files with a secure LLM agent. "
        "The agent can produce output files (reports, CSVs, edited docs) "
        "that are uploaded to S3 and returned as presigned download URLs."
    ),
    version="0.2.0",
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class FilePayload(BaseModel):
    name: str
    content: str = Field(..., description="Base64-encoded file content")


class AnalyseRequest(BaseModel):
    files: list[FilePayload] = Field(..., description="Files to analyse")
    goal: Optional[str] = Field(None, description="What to do with the files")
    llm: Optional[str] = Field(None, description="claude | openai | ollama | noop")
    model: Optional[str] = Field(None, description="Model name override")
    tools: Optional[list[str]] = Field(None, description='e.g. ["read","grep","write"]')
    preprocess: bool = Field(True, description="Auto-index docs and extract tables")
    job_id: Optional[str] = None


class S3AnalyseRequest(BaseModel):
    bucket: str
    key: str
    goal: Optional[str] = None
    llm: Optional[str] = None
    preprocess: bool = True
    job_id: Optional[str] = None


class DownloadInfo(BaseModel):
    filename: str
    s3_key: str
    download_url: str
    size_bytes: int
    content_type: str
    expires_in_seconds: int


class AnalyseResponse(BaseModel):
    job_id: str
    summary: str
    downloads: list[DownloadInfo] = Field(
        default_factory=list,
        description="Presigned S3 URLs for files written by the LLM"
    )
    stats: dict
    duration_ms: int


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "rake-api"}


# ── Analyse: base64 JSON body ──────────────────────────────────────────────────

@app.post("/analyse", response_model=AnalyseResponse)
async def analyse(req: AnalyseRequest):
    """
    Analyse files supplied as base64 in the request body.

    The LLM can produce output files by calling write_file() during analysis.
    These are uploaded to S3 and returned as presigned download URLs.
    """
    named_files = _decode_files(req.files)
    if not named_files:
        raise HTTPException(400, "No files provided")

    job_id = req.job_id or str(uuid.uuid4())
    return await _run(named_files, job_id, req.goal, req.llm, req.model, req.tools, req.preprocess)


# ── Analyse: multipart file upload ────────────────────────────────────────────

@app.post("/upload", response_model=AnalyseResponse)
async def upload(
    files: list[UploadFile] = File(...),
    goal: str = Form(""),
    llm: str = Form(""),
    preprocess: bool = Form(True),
):
    """
    Upload raw files for analysis — no base64 encoding needed.
    Ideal for interactive frontends and CLI tooling.
    """
    named_files: dict[str, bytes] = {}
    for f in files:
        named_files[f.filename or "upload"] = await f.read()
    if not named_files:
        raise HTTPException(400, "No files uploaded")

    job_id = str(uuid.uuid4())
    return await _run(named_files, job_id, goal or None, llm or None, None, None, preprocess)


# ── Analyse: pull from S3 ─────────────────────────────────────────────────────

@app.post("/analyse/s3", response_model=AnalyseResponse)
async def analyse_s3(req: S3AnalyseRequest):
    """
    Download a file from S3 and analyse it.
    Results and output files are written back to S3.
    """
    try:
        from server.s3 import download_file
        content = download_file(req.bucket, req.key)
    except Exception as exc:
        raise HTTPException(400, f"Could not fetch s3://{req.bucket}/{req.key}: {exc}")

    filename = req.key.split("/")[-1]
    job_id = req.job_id or str(uuid.uuid4())
    return await _run({filename: content}, job_id, req.goal, req.llm, None, None, req.preprocess)


# ── Job result retrieval ──────────────────────────────────────────────────────

@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """
    Retrieve the stored result for a completed job from S3.
    Returns 404 if not found, 200 with the result JSON if found.
    """
    try:
        from server.s3 import _s3, _bucket, _prefix
        import json
        key = f"{_prefix()}{job_id}/result.json"
        obj = _s3().get_object(Bucket=_bucket(), Key=key)
        return JSONResponse(json.loads(obj["Body"].read()))
    except Exception as exc:
        raise HTTPException(404, f"Job {job_id} not found: {exc}")


# ── Core analysis logic ───────────────────────────────────────────────────────

async def _run(
    named_files: dict[str, bytes],
    job_id: str,
    goal: Optional[str],
    llm: Optional[str],
    model: Optional[str],
    tools: Optional[list[str]],
    do_preprocess: bool,
) -> AnalyseResponse:
    effective_goal = goal or (
        "Analyse these documents and produce useful output files.\n\n"
        "INSTRUCTIONS:\n"
        "1. Check _index.md files first — they list section line ranges.\n"
        "2. Use read_section() for large documents, never read_file() on huge files.\n"
        "3. Use csv_stats() on all CSV and spreadsheet files.\n"
        "4. Use write_file() to produce output artefacts for the user:\n"
        "   - A markdown report (e.g. report.md) summarising your findings\n"
        "   - Any extracted tables as CSV (e.g. financial_summary.csv)\n"
        "   - Any other useful structured output\n"
        "5. Call done() with a concise summary when finished.\n"
    )

    config = RakeConfig(
        llm=llm or os.environ.get("RAKE_LLM", "claude"),
        model=model or os.environ.get("RAKE_MODEL"),
        api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        tools=tools or ["read", "grep", "write"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "300")),
        preprocess=do_preprocess,
    )

    t0 = time.monotonic()
    try:
        async with RakeClient(config) as client:
            result = await client.analyze_bytes(named_files=named_files, goal=effective_goal)
    except Exception as exc:
        logger.exception("rake failed for job %s", job_id)
        raise HTTPException(500, f"Analysis failed: {exc}")

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Upload output files to S3 (if configured)
    downloads: list[dict] = []
    s3_enabled = bool(os.environ.get("S3_RESULTS_BUCKET"))
    if s3_enabled and result.output_files:
        try:
            from server.s3 import upload_all_output_files, upload_result
            downloads = upload_all_output_files(job_id, result.output_files)
            upload_result(job_id, {
                "job_id": job_id,
                "summary": result.summary,
                "stats": result.to_dict()["stats"],
                "downloads": downloads,
            })
            logger.info("job %s: uploaded %d output files to S3", job_id, len(downloads))
        except Exception as exc:
            logger.warning("S3 upload failed for job %s: %s", job_id, exc)

    return AnalyseResponse(
        job_id=job_id,
        summary=result.summary,
        downloads=[DownloadInfo(**d) for d in downloads],
        stats={
            **result.to_dict()["stats"],
            "output_files": [{"filename": n, "size_bytes": len(b)}
                             for n, b in result.output_files.items()],
        },
        duration_ms=duration_ms,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode_files(payloads: list[FilePayload]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for f in payloads:
        try:
            out[f.name] = base64.b64decode(f.content)
        except Exception:
            out[f.name] = f.content.encode("utf-8")
    return out


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
