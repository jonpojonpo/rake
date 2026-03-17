# rake

Secure LLM agent sandbox. Mount files, let an AI analyse them — WASM-isolated so nothing escapes.

```
rake src/**/*.rs
rake --goal "find all SQL queries" src/
rake --llm ollama --model mistral mydata.csv
rake --system @audit_prompt.txt --llm claude app.py config.json
```

## How it works

Files are mounted into an in-memory virtual filesystem inside a [wasmtime](https://wasmtime.dev/) WASM sandbox. The LLM agent loop runs on the host and calls analysis tools — it can only see what you gave it.

The agent iteratively calls tools until it calls `done` with a Markdown summary. The full trajectory (think steps, tool calls, timing, token counts) is emitted as JSON to stdout.

## Install

```bash
cargo install rake
```

Or build from source:

```bash
git clone https://github.com/jonpojonpo/rake
cd rake
cargo build --release
```

## Usage

```
rake [OPTIONS] <files>...

Options:
  --llm <LLM>          Backend: claude, openai, ollama, noop [default: claude]
  --model <MODEL>      Model name [default: claude-sonnet-4-6]
  --api-key <KEY>      API key (overrides ANTHROPIC_API_KEY / OPENAI_API_KEY)
  --base-url <URL>     Base URL for OpenAI-compatible endpoint
  --model-path <PATH>  Path to GGUF file (--llm llamacpp only)
  --goal <GOAL>        Analysis goal [default: thorough analysis]
  --system <PROMPT>    Override system prompt (use @file.txt to read from file)
  --agent <WASM>       Run a WASM agent directly (skips LLM loop)
  --max-mem <MB>       Sandbox memory limit [default: 40]
  --tools <LIST>       Enabled tools: read,write,grep,exec [default: read,grep]
```

## Backends

| Flag | Env var needed | Notes |
|---|---|---|
| `--llm claude` | `ANTHROPIC_API_KEY` | Default. Uses claude-sonnet-4-6 |
| `--llm openai` | `OPENAI_API_KEY` | Any OpenAI-compatible API |
| `--llm ollama` | — | Defaults to llama3.2 on localhost:11434 |
| `--llm llamacpp` | — | Requires `--features llamacpp` + `--model-path` |
| `--llm noop` | — | Immediately calls `done`, useful for testing |

## Agent tools

| Tool | Description |
|---|---|
| `list_files` | List all mounted files |
| `read_file` | Read full file contents |
| `head` | Read first N lines |
| `grep_files` | Regex search across files |
| `file_info` | Size, line count, MIME type |
| `csv_stats` | Column stats, types, min/max/mean, sample rows |
| `json_query` | JSON Pointer queries, root structure summary |
| `write_file` | Write to sandbox scratch space |
| `done` | Signal completion with Markdown summary |

## Custom system prompts

```bash
# Inline
rake --system "You are a security auditor. Focus only on auth and input validation." app.py

# From file
rake --system @prompts/security_audit.txt src/
```

## Native llama.cpp (optional)

```bash
cargo build --release --features llamacpp
./target/release/rake --llm llamacpp --model-path ~/models/mistral-7b.gguf src/main.rs
```

## License

MIT
