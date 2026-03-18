"""
AWS Lambda: Document Analysis
==============================

Triggers:
  S3 Event: any file uploaded to UPLOAD_BUCKET triggers analysis
  API Gateway POST /doc-analysis: synchronous HTTP endpoint
  SQS queue "doc-analysis-jobs": async batch processing

Supported document types:
  .md .txt .rst         — markdown / plain text (indexed, tables → CSV)
  .docx .doc            — Word documents (converted to markdown)
  .xlsx .xls            — Excel workbooks (one CSV per sheet)
  .pptx .ppt            — PowerPoint (slide-by-slide markdown)
  .pdf                  — PDF (page-by-page text)
  .zip                  — Archives (extracted, each file preprocessed)

All documents get an _index.md with section line ranges so the LLM
navigates with read_section() rather than read_file() on huge docs.

Environment variables:
  ANTHROPIC_API_KEY     API key
  RAKE_LLM              Backend (default: claude)
  RAKE_MODEL            Model name
  RAKE_TIMEOUT          Subprocess timeout in seconds (default: 300)
  UPLOAD_BUCKET         S3 bucket to monitor
  RESULTS_BUCKET        S3 bucket for results (default: same as UPLOAD_BUCKET)
  RESULTS_PREFIX        S3 key prefix for results (default: rake-results/)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import Any

import boto3  # type: ignore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


# ── Lambda handler ─────────────────────────────────────────────────────────────

def handler(event: dict, context: Any) -> dict:
    """Unified Lambda handler — detects trigger type and dispatches."""

    # S3 trigger
    if "Records" in event and event["Records"][0].get("eventSource") == "aws:s3":
        return _handle_s3(event)

    # SQS trigger
    if "Records" in event and event["Records"][0].get("eventSource") == "aws:sqs":
        return _handle_sqs(event)

    # API Gateway (REST or HTTP API)
    if "httpMethod" in event or "requestContext" in event:
        return _handle_http(event)

    logger.warning("Unknown event shape: %s", list(event.keys()))
    return {"statusCode": 400, "body": json.dumps({"error": "Unknown trigger type"})}


# ── S3 trigger ────────────────────────────────────────────────────────────────

def _handle_s3(event: dict) -> dict:
    results = []
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        job_id = str(uuid.uuid4())

        logger.info("S3 trigger: s3://%s/%s  job=%s", bucket, key, job_id)

        # Download file from S3
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            content = obj["Body"].read()
        except Exception as exc:
            logger.exception("Failed to download s3://%s/%s", bucket, key)
            results.append({"key": key, "error": str(exc)})
            continue

        filename = key.split("/")[-1]
        result_dict = _run_analysis(
            named_files={filename: content},
            job_id=job_id,
            goal=None,
        )

        # Write result to S3
        results_bucket = os.environ.get("RESULTS_BUCKET", bucket)
        results_prefix = os.environ.get("RESULTS_PREFIX", "rake-results/")
        result_key = f"{results_prefix}{key}/{job_id}.json"
        s3.put_object(
            Bucket=results_bucket,
            Key=result_key,
            Body=json.dumps(result_dict, indent=2, default=str).encode(),
            ContentType="application/json",
            Metadata={"source_key": key, "job_id": job_id},
        )
        logger.info("Result written to s3://%s/%s", results_bucket, result_key)

        # Publish SNS alert if FINDINGS_TOPIC_ARN is set
        _maybe_publish_alert(result_dict, source_key=key, result_key=result_key)

        results.append({"key": key, "job_id": job_id, "result_key": result_key})

    return {"processed": results}


# ── SQS trigger ───────────────────────────────────────────────────────────────

def _handle_sqs(event: dict) -> dict:
    for record in event["Records"]:
        body = json.loads(record["body"])
        job_id = body.get("job_id") or str(uuid.uuid4())
        named_files = _decode_files(body.get("files", []))
        if not named_files:
            logger.warning("SQS message %s has no files", job_id)
            continue

        result_dict = _run_analysis(
            named_files=named_files,
            job_id=job_id,
            goal=body.get("goal"),
        )
        _write_result_to_s3(result_dict, job_id)

    return {"batchItemFailures": []}


# ── HTTP / API Gateway trigger ─────────────────────────────────────────────────

def _handle_http(event: dict) -> dict:
    try:
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body = json.loads(raw_body)
    except (json.JSONDecodeError, Exception) as exc:
        return _http_error(f"Invalid JSON body: {exc}", 400)

    named_files = _decode_files(body.get("files", []))
    if not named_files:
        return _http_error("No files provided", 400)

    job_id = body.get("job_id") or str(uuid.uuid4())
    result_dict = _run_analysis(
        named_files=named_files,
        job_id=job_id,
        goal=body.get("goal"),
    )

    return {
        "statusCode": 200 if "error" not in result_dict else 500,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(result_dict),
    }


# ── Core analysis ──────────────────────────────────────────────────────────────

def _run_analysis(
    named_files: dict[str, bytes],
    job_id: str,
    goal: str | None,
) -> dict:
    import asyncio, sys, pathlib

    # Allow importing the SDK when deployed as a Lambda layer
    sys.path.insert(0, "/opt/python")
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

    try:
        from rake_sdk import RakeClient, RakeConfig
    except ImportError as exc:
        return {"error": f"rake_sdk not available: {exc}", "job_id": job_id}

    effective_goal = goal or (
        "Analyse these documents thoroughly.\n\n"
        "IMPORTANT INSTRUCTIONS:\n"
        "1. Always check for _index.md files first — they list section line ranges.\n"
        "2. Use read_section() to read specific sections, never read_file() on large docs.\n"
        "3. Use csv_stats() on all .csv files (including extracted tables).\n"
        "4. Use grep_files() to search for specific terms across all documents.\n"
        "5. Summarise key findings, data statistics, and notable content.\n"
        "6. For annual reports / financial documents, extract: revenue, profit, key ratios, "
        "risk factors, chairman's statement highlights, and any notable disclosures.\n"
    )

    config = RakeConfig(
        llm=os.environ.get("RAKE_LLM", "claude"),
        model=os.environ.get("RAKE_MODEL"),
        api_key=(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        ),
        base_url=os.environ.get("RAKE_BASE_URL"),
        tools=["read", "grep", "write"],
        timeout=int(os.environ.get("RAKE_TIMEOUT", "300")),
        preprocess=True,  # always preprocess documents
    )

    t0 = time.monotonic()
    try:
        result = asyncio.run(
            RakeClient(config).analyze_bytes(
                named_files=named_files,
                goal=effective_goal,
            )
        )
    except Exception as exc:
        logger.exception("rake analysis failed for job %s", job_id)
        return {"error": str(exc), "job_id": job_id}

    out = result.to_dict()
    out["job_id"] = job_id
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _write_result_to_s3(result_dict: dict, job_id: str) -> None:
    bucket = os.environ.get("RESULTS_BUCKET")
    if not bucket:
        return
    prefix = os.environ.get("RESULTS_PREFIX", "rake-results/")
    key = f"{prefix}{job_id}.json"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(result_dict, indent=2, default=str).encode(),
        ContentType="application/json",
    )


def _maybe_publish_alert(result_dict: dict, source_key: str, result_key: str) -> None:
    topic_arn = os.environ.get("FINDINGS_TOPIC_ARN")
    if not topic_arn:
        return
    findings = result_dict.get("findings", [])
    if not findings:
        return
    sns = boto3.client("sns")
    sns.publish(
        TopicArn=topic_arn,
        Subject=f"rake analysis complete: {source_key}",
        Message=json.dumps({
            "source_key": source_key,
            "result_key": result_key,
            "job_id": result_dict.get("job_id"),
            "findings_count": len(findings),
            "summary_preview": result_dict.get("summary", "")[:500],
        }),
    )


def _http_error(msg: str, status: int) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": msg}),
    }
