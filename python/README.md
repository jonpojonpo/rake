# rake Python SDK & Azure Microservices

Python integration for **[rake](https://github.com/jonpojonpo/rake)** — the secure LLM agent sandbox.

Deploy AI-powered code review, security auditing, and data profiling as production-grade Azure microservices.

---

## Architecture

```
                          ┌─────────────────────────────────────┐
                          │        Azure Container Apps          │
  GitHub PR ──────────►  │  ┌──────────────────────────────┐   │
  Blob upload ────────►  │  │  rake-code-review  (FastAPI)  │   │
  HTTP client ────────►  │  │  rake-security-audit          │   │
  Service Bus queue ──►  │  │  rake-data-analysis           │   │
                          │  └──────────────────────────────┘   │
                          └─────────────┬───────────────────────┘
                                        │  subprocess
                                        ▼
                          ┌─────────────────────────────┐
                          │   rake binary (Rust/WASM)   │
                          │   ├── files in virtual FS   │
                          │   ├── LLM agent loop        │
                          │   │   └── Claude/OpenAI     │
                          │   └── JSON trajectory out   │
                          └─────────────────────────────┘
                                        │
                          ┌─────────────▼───────────────┐
                          │    Azure Blob Storage        │
                          │    App Insights telemetry    │
                          │    Service Bus alerts        │
                          └─────────────────────────────┘
```

---

## Quick Start

### 1. Install rake

```bash
cargo install rake-sandbox
```

### 2. Install the Python SDK

```bash
pip install -e python/          # from this repo
pip install rake-sdk            # from PyPI (once published)
pip install "rake-sdk[azure]"   # with Azure dependencies
```

### 3. Analyse files

```python
import asyncio
from rake_sdk import RakeClient, RakeConfig

async def main():
    config = RakeConfig(llm="claude")  # uses ANTHROPIC_API_KEY env var

    async with RakeClient(config) as client:
        result = await client.security_audit(files=["app.py", "config.json"])

    print(result.summary)
    for finding in result.findings:
        print(f"[{finding.severity.value.upper()}] {finding.title}")

    if result.has_critical_issues:
        raise SystemExit(1)

asyncio.run(main())
```

### 4. Analyse in-memory bytes

```python
result = await client.analyze_bytes(
    named_files={
        "main.py": open("main.py", "rb").read(),
        "config.json": b'{"secret": "hardcoded"}',
    },
    goal="Find security vulnerabilities.",
)
```

---

## SDK Reference

### `RakeConfig`

| Field | Default | Description |
|---|---|---|
| `llm` | `"claude"` | Backend: `claude`, `openai`, `ollama`, `noop` |
| `model` | None | Model name (backend default if None) |
| `api_key` | None | API key (falls back to env vars) |
| `base_url` | None | Custom endpoint URL (Azure OpenAI, Ollama) |
| `max_mem` | `40` | Sandbox memory limit (MB) |
| `tools` | `["read","grep"]` | Enabled tools |
| `timeout` | `300` | Subprocess timeout (seconds) |
| `extra_env` | `{}` | Extra env vars forwarded to rake |

### `RakeClient` methods

```python
# Full control
result = await client.analyze(files=[...], goal="...", system="...")

# In-memory content
result = await client.analyze_bytes(named_files={"file.py": b"..."}, goal="...")

# Convenience shortcuts
result = await client.security_audit(files=[...])   # OWASP-focused
result = await client.code_review(files=[...])      # quality + bugs
result = await client.data_profile(files=[...])     # CSV/JSON stats
```

### `RakeResult`

```python
result.summary               # Markdown summary from the LLM
result.findings              # List[Finding]
result.critical_findings     # List[Finding] with severity == CRITICAL
result.high_findings         # List[Finding] with severity == HIGH
result.has_critical_issues   # bool
result.total_input_tokens    # int
result.total_output_tokens   # int
result.total_llm_ms          # int
result.tool_calls            # int
result.to_dict()             # Serialise to dict
```

### `Finding`

```python
finding.title       # str
finding.description # str
finding.severity    # FindingSeverity enum: CRITICAL|HIGH|MEDIUM|LOW|INFO
finding.file        # Optional[str]
finding.line        # Optional[int]
finding.to_dict()   # dict
```

---

## Azure Microservices

Three Azure Functions (v2 Python programming model) are provided:

### `services/code_review/`

**HTTP POST** `/api/code-review`

```bash
curl -X POST https://<func>.azurewebsites.net/api/code-review \
  -H "Content-Type: application/json" \
  -d '{
    "files": [
      {"name": "app.py", "content": "'$(base64 app.py)'"}
    ],
    "goal": "Review for bugs and code quality"
  }'
```

Also subscribes to Service Bus queue `code-review-jobs` for async processing.

### `services/security_audit/`

**HTTP POST** `/api/security-audit`

**Blob trigger** — automatically audits any file uploaded to the `uploads` container.

Emits to `security-alerts` queue if CRITICAL/HIGH findings are detected.

```bash
# Upload a file — audit runs automatically
az storage blob upload \
  --account-name <storage> \
  --container-name uploads \
  --file app.py --name app.py
```

### `services/data_analysis/`

**HTTP POST** `/api/data-analysis`

**Blob trigger** — automatically profiles any `.csv`/`.json` file in `data-uploads`.

```json
{
  "files": [{"name": "sales.csv", "content": "<base64>"}],
  "output_format": "detailed"
}
```

---

## Local Development

### Option A: FastAPI dev server (fastest)

```bash
# Build and start
docker compose -f docker/docker-compose.yml up rake-api

# Test
curl -X POST http://localhost:8000/api/security-audit \
  -H "Content-Type: application/json" \
  -d '{"files": [{"name": "test.py", "content": "'$(base64 tests/fixtures/sample/app.py)'"}]}'
```

### Option B: Azure Functions Core Tools

```bash
cd python/
cp local.settings.json.example local.settings.json
# Edit local.settings.json with your API keys
func start
```

### Option C: Direct SDK (no server)

```bash
ANTHROPIC_API_KEY=sk-ant-... python examples/01_quickstart.py
RAKE_LLM=noop python examples/02_analyze_bytes.py
```

---

## Deploy to Azure

### Prerequisites

```bash
az login
az --version    # 2.55+
```

### One-command deployment

```bash
./infra/deploy.sh \
  --suffix myunique123 \
  --api-key sk-ant-... \
  --location eastus
```

This will:
1. Create Resource Group `rg-rake`
2. Deploy all Azure resources (Storage, Service Bus, App Insights, Container Registry, Container Apps)
3. Build and push the Docker image
4. Deploy three Container Apps (code-review, security-audit, data-analysis)

### Manual deployment (Bicep)

```bash
az group create --name rg-rake --location eastus

az deployment group create \
  --resource-group rg-rake \
  --template-file infra/main.bicep \
  --parameters suffix=abc123 anthropicApiKey=sk-ant-...
```

---

## GitHub Actions CI/CD

The workflow at `.github/workflows/ci-cd.yml`:

1. **Rust build** — compiles the rake binary
2. **Python tests** — runs `pytest python/tests/` with noop LLM
3. **Docker build** — builds and pushes to Azure Container Registry
4. **Deploy** — updates Container Apps (master branch only)

### Required secrets

| Secret | Description |
|---|---|
| `ACR_LOGIN_SERVER` | `<name>.azurecr.io` |
| `ACR_USERNAME` | ACR admin username |
| `ACR_PASSWORD` | ACR admin password |
| `AZURE_CREDENTIALS` | `az ad sp create-for-rbac` JSON |
| `ACR_NAME` | Registry name (without `.azurecr.io`) |

---

## CI gate: block PRs with critical vulnerabilities

Add to your GitHub workflow:

```yaml
- name: Security gate
  run: |
    python python/examples/04_github_pr_review.py \
      --repo ${{ github.repository }} \
      --pr ${{ github.event.pull_request.number }} \
      --fail-on-critical \
      --post-comment
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude backend) |
| `OPENAI_API_KEY` | OpenAI / Azure OpenAI API key |
| `RAKE_BINARY` | Path to rake binary (auto-detected if unset) |
| `RAKE_LLM` | Default backend: `claude`, `openai`, `ollama` |
| `RAKE_MODEL` | Default model name |
| `RAKE_BASE_URL` | Custom API endpoint |
| `RAKE_TIMEOUT` | Subprocess timeout in seconds (default: 240) |
| `AZURE_STORAGE_CONNECTION_STRING` | Blob Storage connection string |
| `RESULTS_CONTAINER` | Output blob container (default: `rake-results`) |
| `SERVICE_BUS_CONNECTION` | Service Bus connection string |
| `APPINSIGHTS_INSTRUMENTATIONKEY` | App Insights key |

---

## Running Tests

```bash
cd python/
pip install -e ".[dev]"

# Unit tests (no API key needed)
RAKE_LLM=noop pytest tests/ -v

# Integration test with real LLM
ANTHROPIC_API_KEY=sk-ant-... pytest tests/ -v -k integration
```
