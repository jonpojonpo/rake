"""
FastAPI dev server — local mirror of the rake AWS Lambda endpoints.

Endpoints:
  POST /api/doc-analysis     — analyse documents (md, docx, xlsx, pptx, pdf, zip)
  POST /api/data-analysis    — profile data files (csv, json, jsonl)
  POST /upload               — multipart file upload, no base64 needed
  GET  /health               — health check

The preprocessor runs automatically:
  - Generates _index.md with section TOC and line ranges
  - Extracts markdown tables as .csv files
  - Converts DOCX/XLSX/PPTX/PDF to text

Run:
  uvicorn local_server:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
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
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, "/app")

from rake_sdk import RakeClient, RakeConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="rake Document Analysis — Local Dev",
    description="Local mirror of the rake AWS Lambda document analysis services",
    version="0.2.0",
)

_PROMPTS_DIR = Path(__file__).parent.parent / "python" / "prompts"


class FilePayload(BaseModel):
    name: str
    content: str  # base64


class AnalysisRequest(BaseModel):
    files: list[FilePayload]
    goal: Optional[str] = None
    llm: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None   # "document_analysis" | "annual_report" | inline text
    job_id: Optional[str] = None


def _decode_files(payloads: list[FilePayload]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for f in payloads:
        try:
            out[f.name] = base64.b64decode(f.content)
        except Exception:
            out[f.name] = f.content.encode("utf-8")
    return out


def _load_system(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    # Named prompt file
    p = _PROMPTS_DIR / f"{name}.txt"
    if p.exists():
        return p.read_text()
    # Inline text
    if len(name) > 20:
        return name
    return None


def _make_config(req: AnalysisRequest) -> RakeConfig:
    return RakeConfig(
        llm=req.llm or os.environ.get("RAKE_LLM", "claude"),
        model=req.model or os.environ.get("RAKE_MODEL"),
        api_key=(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        ),
        tools=["read", "grep", "write"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "300")),
        preprocess=True,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "rake-doc-analysis-local"}


@app.post("/api/doc-analysis")
async def doc_analysis(req: AnalysisRequest):
    """
    Analyse documents — markdown, DOCX, XLSX, PPTX, PDF, ZIP.
    Files are auto-preprocessed: _index.md with section line ranges,
    tables extracted as CSV, office formats converted to text.
    """
    job_id = req.job_id or str(uuid.uuid4())
    named_files = _decode_files(req.files)
    if not named_files:
        raise HTTPException(400, "No files provided")

    goal = req.goal or (
        "Analyse these documents. "
        "Check _index.md files first to get section line ranges. "
        "Use read_section() to read specific sections. "
        "Use csv_stats() on extracted table CSV files. "
        "Provide a detailed summary of the content, key data, and any notable findings."
    )
    system = _load_system(req.system_prompt)

    config = _make_config(req)
    t0 = time.monotonic()
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files=named_files,
            goal=goal,
            system=system,
        )

    out = result.to_dict()
    out["job_id"] = job_id
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return JSONResponse(out)


@app.post("/api/data-analysis")
async def data_analysis(req: AnalysisRequest):
    """Profile CSV, JSON, JSONL data files."""
    job_id = req.job_id or str(uuid.uuid4())
    named_files = _decode_files(req.files)
    if not named_files:
        raise HTTPException(400, "No files provided")

    goal = req.goal or (
        "Profile these data files. For each file report: "
        "schema (column names, types, sample values), "
        "shape (row/column count), "
        "null rates per column, "
        "numeric distributions (min, max, mean, p95), "
        "top categorical values, "
        "anomalies and data quality issues, "
        "and recommendations for cleaning. "
        "Use csv_stats() for all CSV files — never read_file() on data files."
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


@app.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    goal: str = Form(""),
    mode: str = Form("doc-analysis"),
    system_prompt: str = Form("document_analysis"),
):
    """
    Convenience endpoint — upload raw files (no base64 encoding needed).
    mode: "doc-analysis" | "data-analysis"
    system_prompt: "document_analysis" | "annual_report" | inline text
    """
    named_files: dict[str, bytes] = {}
    for f in files:
        named_files[f.filename or "upload"] = await f.read()

    config = RakeConfig(
        llm=os.environ.get("RAKE_LLM", "claude"),
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        tools=["read", "grep", "write"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "300")),
        preprocess=True,
    )

    effective_goal = goal or (
        "Analyse these documents thoroughly. Use _index.md to navigate large docs. "
        "Use read_section() for sections. Use csv_stats() for all CSV/data files."
    )
    system = _load_system(system_prompt)

    t0 = time.monotonic()
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files=named_files,
            goal=effective_goal,
            system=system,
        )

    out = result.to_dict()
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return JSONResponse(out)


@app.post("/api/s3-analyse")
async def s3_analyse(
    bucket: str = Query(..., description="S3 bucket"),
    key: str = Query(..., description="S3 object key"),
    goal: Optional[str] = Query(None),
):
    """
    Download a file from S3 and analyse it.
    Requires AWS credentials in environment (or IRSA/instance role).
    """
    import boto3, botocore  # type: ignore

    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    s3 = boto3.client("s3", endpoint_url=endpoint)

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        content = obj["Body"].read()
    except botocore.exceptions.ClientError as exc:
        raise HTTPException(400, f"S3 error: {exc}")

    filename = key.split("/")[-1]
    config = RakeConfig(
        llm=os.environ.get("RAKE_LLM", "claude"),
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        tools=["read", "grep", "write"],
        timeout=300,
        preprocess=True,
    )
    effective_goal = goal or "Analyse this document. Use _index.md for navigation."
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files={filename: content},
            goal=effective_goal,
            system=_load_system("document_analysis"),
        )

    out = result.to_dict()
    out["source"] = f"s3://{bucket}/{key}"
    return JSONResponse(out)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
