"""
RakeClient — async Python wrapper around the rake CLI binary.

Supports:
  - Claude, OpenAI, Ollama backends
  - Temporary file management (analyze in-memory bytes without disk clutter)
  - Custom system prompts
  - Configurable tool sets and memory limits
  - Structured result parsing with finding extraction
  - Document preprocessing: auto-generates _index.md files with line ranges,
    extracts markdown tables as CSVs, converts DOCX/XLSX/PPTX/PDF/ZIP
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .exceptions import (
    RakeBinaryNotFoundError,
    RakeError,
    RakeParseError,
    RakeTimeoutError,
)
from .models import RakeResult


@dataclass
class RakeConfig:
    """Configuration for the rake client."""

    # Path to rake binary — auto-detected from PATH if None
    binary: Optional[str] = None

    # LLM backend: "claude" | "openai" | "ollama" | "noop"
    llm: str = "claude"

    # Model name (None = backend default)
    model: Optional[str] = None

    # API key — falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY env vars
    api_key: Optional[str] = None

    # Base URL for OpenAI-compatible endpoints (Ollama, Azure OpenAI, proxies)
    base_url: Optional[str] = None

    # Sandbox memory limit in MB
    max_mem: int = 40

    # Enabled tool set: subset of {"read", "write", "grep", "exec"}
    tools: list[str] = field(default_factory=lambda: ["read", "grep"])

    # Max time (seconds) to wait for rake to finish
    timeout: int = 300

    # Automatically preprocess documents before mounting (generates index files,
    # converts DOCX/XLSX/PPTX/ZIP, extracts markdown tables as CSV)
    preprocess: bool = True

    # Additional environment variables forwarded to rake process
    extra_env: dict[str, str] = field(default_factory=dict)


class RakeClient:
    """
    Async client for the rake secure LLM analysis sandbox.

    Can be used as an async context manager:

        async with RakeClient() as client:
            result = await client.analyze(files=["app.py"], goal="find bugs")

    Or directly:

        client = RakeClient()
        result = await client.analyze(files=["app.py"])
    """

    def __init__(self, config: Optional[RakeConfig] = None):
        self.config = config or RakeConfig()
        self._binary: Optional[str] = None

    async def __aenter__(self) -> "RakeClient":
        return self

    async def __aexit__(self, *_) -> None:
        pass

    # ── Public API ────────────────────────────────────────────────────────────

    async def analyze(
        self,
        files: list[Union[str, Path]],
        *,
        goal: str = "Analyse these files thoroughly. Find bugs, security issues, TODOs, and anything noteworthy.",
        system: Optional[str] = None,
        system_file: Optional[Union[str, Path]] = None,
        llm: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[list[str]] = None,
        max_mem: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> RakeResult:
        """
        Run rake on the given files and return a structured result.

        Args:
            files: Paths to files to mount in the sandbox.
            goal: Natural-language analysis goal.
            system: Inline system prompt override.
            system_file: Path to a file containing the system prompt.
            llm: Backend override ("claude", "openai", "ollama", "noop").
            model: Model name override.
            tools: Tool list override (e.g. ["read", "grep", "write"]).
            max_mem: Sandbox memory override (MB).
            timeout: Execution timeout override (seconds).

        Returns:
            RakeResult with summary, findings, trajectory, and token stats.
        """
        binary = await self._resolve_binary()
        file_paths = [str(Path(f).resolve()) for f in files]

        cmd = self._build_command(
            binary=binary,
            files=file_paths,
            goal=goal,
            system=system,
            system_file=system_file,
            llm=llm or self.config.llm,
            model=model or self.config.model,
            tools=tools or self.config.tools,
            max_mem=max_mem or self.config.max_mem,
        )

        env = self._build_env()
        tout = timeout or self.config.timeout

        stdout, stderr = await self._run(cmd, env=env, timeout=tout)

        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RakeParseError(
                f"Failed to parse rake output as JSON: {exc}",
                stderr=stderr,
            ) from exc

        return RakeResult.from_trajectory(raw, files=file_paths)

    async def analyze_bytes(
        self,
        named_files: dict[str, bytes],
        *,
        goal: str = "Analyse these files thoroughly.",
        preprocess: Optional[bool] = None,
        **kwargs,
    ) -> RakeResult:
        """
        Analyse in-memory file content without requiring files on disk.

        Automatically preprocesses documents before mounting:
        - Generates _index.md with section TOC and line ranges
        - Extracts markdown tables as .csv files
        - Converts DOCX/XLSX/PPTX/PDF/ZIP to text equivalents

        Args:
            named_files: Mapping of filename → file bytes.
            goal: Analysis goal.
            preprocess: Override config.preprocess for this call.
            **kwargs: Forwarded to `analyze()`.

        Returns:
            RakeResult
        """
        should_preprocess = preprocess if preprocess is not None else self.config.preprocess
        if should_preprocess:
            from .preprocessors import preprocess_files
            named_files = preprocess_files(named_files)

        with tempfile.TemporaryDirectory(prefix="rake_") as tmpdir:
            paths: list[Path] = []
            for name, content in named_files.items():
                p = Path(tmpdir) / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
                paths.append(p)
            return await self.analyze(files=paths, goal=goal, **kwargs)

    # ── Convenience helpers ───────────────────────────────────────────────────

    async def security_audit(self, files: list[Union[str, Path]], **kwargs) -> RakeResult:
        """Run a security-focused analysis."""
        goal = (
            "Perform a thorough security audit. Identify: hardcoded credentials, "
            "injection vulnerabilities (SQL, command, path traversal), insecure "
            "deserialization, authentication/authorization flaws, cryptographic "
            "weaknesses, and any other OWASP Top 10 issues. "
            "Rate each finding as CRITICAL, HIGH, MEDIUM, LOW, or INFO."
        )
        return await self.analyze(files=files, goal=goal, **kwargs)

    async def code_review(self, files: list[Union[str, Path]], **kwargs) -> RakeResult:
        """Run a code-quality review."""
        goal = (
            "Review this code for quality and correctness. Look for: bugs, "
            "logic errors, missing error handling, performance issues, dead code, "
            "unclear naming, missing tests, and architectural concerns. "
            "For each issue label it [HIGH], [MEDIUM], or [LOW]."
        )
        return await self.analyze(files=files, goal=goal, **kwargs)

    async def data_profile(self, files: list[Union[str, Path]], **kwargs) -> RakeResult:
        """Profile CSV/JSON data files."""
        goal = (
            "Profile these data files. For each file report: row count, column "
            "names and types, null rates, numeric distributions (min/max/mean), "
            "categorical value frequency, any anomalies or data quality issues, "
            "and schema structure."
        )
        return await self.analyze(files=files, goal=goal, **kwargs)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_command(
        self,
        *,
        binary: str,
        files: list[str],
        goal: str,
        system: Optional[str],
        system_file: Optional[Union[str, Path]],
        llm: str,
        model: Optional[str],
        tools: list[str],
        max_mem: int,
    ) -> list[str]:
        cmd = [binary, "--llm", llm]

        if model:
            cmd += ["--model", model]
        if self.config.api_key:
            cmd += ["--api-key", self.config.api_key]
        if self.config.base_url:
            cmd += ["--base-url", self.config.base_url]

        cmd += ["--goal", goal]
        cmd += ["--tools", ",".join(tools)]
        cmd += ["--max-mem", str(max_mem)]

        if system:
            cmd += ["--system", system]
        elif system_file:
            cmd += ["--system", f"@{system_file}"]

        cmd += files
        return cmd

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.config.extra_env)
        if self.config.api_key:
            # Ensure key is set for whichever backend is active
            env.setdefault("ANTHROPIC_API_KEY", self.config.api_key)
            env.setdefault("OPENAI_API_KEY", self.config.api_key)
        return env

    async def _resolve_binary(self) -> str:
        if self._binary:
            return self._binary

        # 1. Explicit path in config
        if self.config.binary:
            if not Path(self.config.binary).is_file():
                raise RakeBinaryNotFoundError(
                    f"Configured rake binary not found: {self.config.binary}"
                )
            self._binary = self.config.binary
            return self._binary

        # 2. RAKE_BINARY env var
        env_path = os.environ.get("RAKE_BINARY")
        if env_path and Path(env_path).is_file():
            self._binary = env_path
            return self._binary

        # 3. PATH lookup
        found = shutil.which("rake")
        if found:
            self._binary = found
            return self._binary

        # 4. Common relative paths (useful in Docker images)
        for candidate in ["/usr/local/bin/rake", "/app/rake", "./rake", "./target/release/rake"]:
            if Path(candidate).is_file():
                self._binary = candidate
                return self._binary

        raise RakeBinaryNotFoundError(
            "rake binary not found. Install via 'cargo install rake-sandbox' or "
            "set the RAKE_BINARY environment variable."
        )

    async def _run(
        self,
        cmd: list[str],
        env: dict[str, str],
        timeout: int,
    ) -> tuple[str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise RakeTimeoutError(
                f"rake timed out after {timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise RakeBinaryNotFoundError(
                f"rake binary not found: {cmd[0]}"
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise RakeError(
                f"rake exited with code {proc.returncode}",
                stderr=stderr,
                returncode=proc.returncode,
            )

        return stdout, stderr
