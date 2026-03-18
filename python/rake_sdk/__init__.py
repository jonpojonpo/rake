"""
rake-sdk: Python client for the rake secure LLM agent sandbox.

Usage:
    from rake_sdk import RakeClient, RakeResult

    async with RakeClient() as client:
        result = await client.analyze(
            files=["app.py", "config.json"],
            goal="Find security vulnerabilities",
            llm="claude",
        )
        print(result.summary)
        print(result.findings)
"""

from .client import RakeClient, RakeConfig
from .models import (
    RakeResult,
    TrajectoryStep,
    ThinkStep,
    LlmTurnStep,
    ToolCallStep,
    ToolResultStep,
    DoneStep,
    Finding,
    FindingSeverity,
)
from .exceptions import RakeError, RakeTimeoutError, RakeBinaryNotFoundError

__all__ = [
    "RakeClient",
    "RakeConfig",
    "RakeResult",
    "TrajectoryStep",
    "ThinkStep",
    "LlmTurnStep",
    "ToolCallStep",
    "ToolResultStep",
    "DoneStep",
    "Finding",
    "FindingSeverity",
    "RakeError",
    "RakeTimeoutError",
    "RakeBinaryNotFoundError",
]
