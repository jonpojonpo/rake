"""Exceptions raised by the rake Python SDK."""


class RakeError(Exception):
    """Base exception for all rake errors."""

    def __init__(self, message: str, stderr: str = "", returncode: int = -1):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class RakeTimeoutError(RakeError):
    """Raised when rake exceeds the configured timeout."""


class RakeBinaryNotFoundError(RakeError):
    """Raised when the rake binary cannot be located."""


class RakeParseError(RakeError):
    """Raised when trajectory JSON cannot be parsed."""
