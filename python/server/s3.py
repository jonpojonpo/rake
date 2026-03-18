"""
S3 helpers for the rake FastAPI server.

Handles:
  - Downloading source files for analysis
  - Uploading analysis results (JSON) and LLM output files
  - Generating presigned download URLs (default 1-hour TTL)

Environment variables:
  AWS_DEFAULT_REGION          (default: us-east-1)
  AWS_ACCESS_KEY_ID           (or IRSA / instance profile)
  AWS_SECRET_ACCESS_KEY
  AWS_ENDPOINT_URL            (optional: LocalStack / MinIO endpoint)
  S3_RESULTS_BUCKET           bucket for results + output files
  S3_RESULTS_PREFIX           key prefix (default: "rake-results/")
  S3_PRESIGN_TTL_SECONDS      presigned URL TTL (default: 3600)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import boto3  # type: ignore


def _s3():
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),  # None = real AWS
    )


def _bucket() -> str:
    b = os.environ.get("S3_RESULTS_BUCKET")
    if not b:
        raise EnvironmentError("S3_RESULTS_BUCKET is not set")
    return b


def _prefix() -> str:
    return os.environ.get("S3_RESULTS_PREFIX", "rake-results/")


def _ttl() -> int:
    return int(os.environ.get("S3_PRESIGN_TTL_SECONDS", "3600"))


def upload_result(job_id: str, result_dict: dict) -> str:
    """Upload the JSON result for a job. Returns the S3 key."""
    key = f"{_prefix()}{job_id}/result.json"
    _s3().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=json.dumps(result_dict, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    return key


def upload_output_file(job_id: str, filename: str, content: bytes) -> tuple[str, str]:
    """
    Upload a single LLM output file to S3.
    Returns (s3_key, presigned_download_url).
    """
    content_type = _guess_mime(filename)
    key = f"{_prefix()}{job_id}/files/{filename}"
    _s3().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=content,
        ContentType=content_type,
        ContentDisposition=f'attachment; filename="{filename}"',
    )
    url = _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key},
        ExpiresIn=_ttl(),
    )
    return key, url


def upload_all_output_files(job_id: str, output_files: dict[str, bytes]) -> list[dict]:
    """
    Upload all LLM output files. Returns a list of download descriptors:
      [{"filename": "report.md", "s3_key": "...", "download_url": "...", "size_bytes": N}]
    """
    downloads = []
    for filename, content in output_files.items():
        key, url = upload_output_file(job_id, filename, content)
        downloads.append({
            "filename": filename,
            "s3_key": key,
            "download_url": url,
            "size_bytes": len(content),
            "content_type": _guess_mime(filename),
            "expires_in_seconds": _ttl(),
        })
    return downloads


def download_file(bucket: str, key: str) -> bytes:
    """Download a file from S3."""
    obj = _s3().get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def _guess_mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "md": "text/markdown",
        "txt": "text/plain",
        "csv": "text/csv",
        "json": "application/json",
        "html": "text/html",
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(ext, "application/octet-stream")
