"""
Azure Blob Storage helpers for rake microservices.

Supports reading source files from Blob Storage and writing analysis
results back — enabling event-driven pipelines where a blob upload
triggers a rake analysis.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse


def _get_blob_client(container: str, blob_name: str):
    """Create an Azure BlobClient from environment config."""
    from azure.storage.blob import BlobServiceClient  # type: ignore

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")

    if conn_str:
        svc = BlobServiceClient.from_connection_string(conn_str)
    elif account_url:
        from azure.identity import DefaultAzureCredential  # type: ignore
        svc = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    else:
        raise EnvironmentError(
            "Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL"
        )

    return svc.get_blob_client(container=container, blob=blob_name)


async def download_blob_bytes(container: str, blob_name: str) -> bytes:
    """Download a blob and return its raw bytes."""
    client = _get_blob_client(container, blob_name)
    stream = client.download_blob()
    return stream.readall()


async def upload_result(
    container: str,
    blob_name: str,
    result_dict: dict,
    metadata: Optional[dict] = None,
) -> str:
    """
    Upload a rake result dict as JSON to Blob Storage.

    Returns the blob URL.
    """
    client = _get_blob_client(container, blob_name)
    payload = json.dumps(result_dict, indent=2, default=str)
    client.upload_blob(
        payload.encode("utf-8"),
        overwrite=True,
        content_settings=_json_content_settings(),
        metadata=metadata or {},
    )
    return client.url


def _json_content_settings():
    from azure.storage.blob import ContentSettings  # type: ignore
    return ContentSettings(content_type="application/json")


def result_blob_name(source_blob: str, job_id: str) -> str:
    """
    Derive the output blob name from the source blob name and a job ID.

    e.g. "uploads/app.py" + "abc123" → "results/app.py/abc123.json"
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"results/{source_blob}/{ts}_{job_id}.json"
