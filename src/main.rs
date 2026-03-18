mod agent;
mod llm;
mod sandbox;

use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use base64::Engine as _;
use clap::{Parser, ValueEnum};
use sandbox::{Sandbox, SandboxConfig, ToolSet};

#[derive(Debug, Clone, ValueEnum)]
enum LlmChoice {
    Claude,
    Openai,
    Ollama,
    #[cfg(feature = "llamacpp")]
    Llamacpp,
    Noop,
}

#[derive(Parser)]
#[command(name = "rake", about = "Secure LLM agent sandbox")]
struct Cli {
    /// LLM backend
    #[arg(long, default_value = "claude")]
    llm: LlmChoice,

    /// API key (overrides ANTHROPIC_API_KEY / OPENAI_API_KEY env vars)
    #[arg(long)]
    api_key: Option<String>,

    /// Model name
    #[arg(long, default_value = "claude-sonnet-4-6")]
    model: String,

    /// Base URL for OpenAI-compatible endpoint (overrides default)
    /// e.g. http://localhost:11434/v1  for Ollama
    ///      http://localhost:8080/v1   for llama-server
    #[arg(long)]
    base_url: Option<String>,

    /// Path to a GGUF model file (required for --llm llamacpp)
    #[arg(long)]
    model_path: Option<PathBuf>,

    /// What to analyse (goal passed to the agent)
    #[arg(
        long,
        default_value = "Analyse these files thoroughly. Find bugs, security issues, TODOs, and anything noteworthy."
    )]
    goal: String,

    /// Override the agent system prompt (inline text).
    /// Prefix with @ to read from a file, e.g. --system @prompt.txt
    #[arg(long)]
    system: Option<String>,

    /// Path to agent WASM module — skips LLM loop, runs WASM directly
    #[arg(long)]
    agent: Option<PathBuf>,

    /// Sandbox memory limit in MB
    #[arg(long, default_value = "40")]
    max_mem: u64,

    /// Enabled tools: read,write,grep,exec (comma-separated)
    #[arg(long, default_value = "read,grep")]
    tools: String,

    /// Directory of WASM skill modules to mount at /skills/ inside the sandbox.
    /// Each <name>.wasm becomes callable via run_skill(name, input).
    /// Companion <name>.json files (with a "description" field) are used to
    /// build /skills/manifest.json so the agent knows what skills are available.
    #[arg(long)]
    skills: Option<PathBuf>,

    /// Files to mount into the sandbox
    #[arg(required = true)]
    files: Vec<PathBuf>,
}

fn parse_tools(s: &str) -> Result<ToolSet> {
    let mut tools = ToolSet::empty();
    for part in s.split(',') {
        match part.trim() {
            "read"  => tools |= ToolSet::READ,
            "write" => tools |= ToolSet::WRITE,
            "grep"  => tools |= ToolSet::GREP,
            "exec"  => tools |= ToolSet::EXEC,
            other   => anyhow::bail!("unknown tool: {other}"),
        }
    }
    Ok(tools)
}

// Minimal WASM: (module (func (export "_start")))
const NOOP_AGENT_WASM: &[u8] = &[
    0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00,
    0x01, 0x04, 0x01, 0x60, 0x00, 0x00,
    0x03, 0x02, 0x01, 0x00,
    0x07, 0x0a, 0x01, 0x06, 0x5f, 0x73, 0x74, 0x61,
    0x72, 0x74, 0x00, 0x00,
    0x0a, 0x04, 0x01, 0x02, 0x00, 0x0b,
];

fn build_backend(cli: &Cli) -> Result<Box<dyn llm::LlmBackend>> {
    let backend: Box<dyn llm::LlmBackend> = match &cli.llm {
        LlmChoice::Noop => Box::new(llm::NoopBackend),

        LlmChoice::Claude => {
            let key = cli.api_key.clone()
                .or_else(|| std::env::var("ANTHROPIC_API_KEY").ok())
                .ok_or_else(|| {
                    anyhow::anyhow!(
                        "Claude backend requires an API key.\n\
                         Set ANTHROPIC_API_KEY or pass --api-key <key>"
                    )
                })?;
            Box::new(llm::claude::ClaudeBackend::new(key).with_model(&cli.model))
        }

        LlmChoice::Openai => {
            let key = cli.api_key.clone()
                .or_else(|| std::env::var("OPENAI_API_KEY").ok())
                .ok_or_else(|| {
                    anyhow::anyhow!(
                        "OpenAI backend requires an API key.\n\
                         Set OPENAI_API_KEY or pass --api-key <key>"
                    )
                })?;
            let mut b = llm::openai::OpenAiBackend::new(key, &cli.model);
            if let Some(url) = &cli.base_url {
                b = b.with_base_url(url);
            }
            Box::new(b)
        }

        LlmChoice::Ollama => {
            let model = if cli.model == "claude-sonnet-4-6" {
                // User hasn't overridden the model — pick a sensible Ollama default.
                "llama3.2".to_string()
            } else {
                cli.model.clone()
            };
            let mut b = llm::openai::OpenAiBackend::ollama(model);
            if let Some(url) = &cli.base_url {
                b = b.with_base_url(url);
            }
            Box::new(b)
        }

        #[cfg(feature = "llamacpp")]
        LlmChoice::Llamacpp => {
            let path = cli.model_path.as_ref().ok_or_else(|| {
                anyhow::anyhow!(
                    "--llm llamacpp requires --model-path /path/to/model.gguf"
                )
            })?;
            Box::new(llm::llamacpp::LlamaCppBackend::from_gguf(path)?)
        }
    };
    Ok(backend)
}

fn resolve_system(opt: &Option<String>) -> Result<String> {
    match opt {
        None => Ok(agent::Agent::DEFAULT_SYSTEM.to_string()),
        Some(s) if s.starts_with('@') => {
            let path = &s[1..];
            fs::read_to_string(path)
                .with_context(|| format!("reading system prompt from {path}"))
        }
        Some(s) => Ok(s.clone()),
    }
}

/// Scan `skills_dir` for `.wasm` files, mount them at `/skills/<name>.wasm`,
/// mount any companion `.json` metadata files, and generate a
/// `/skills/manifest.json` index so the agent can discover what's available.
fn mount_skills(sandbox: &mut Sandbox, skills_dir: &std::path::Path) -> Result<()> {
    use std::collections::BTreeMap;

    if !skills_dir.is_dir() {
        anyhow::bail!("skills path is not a directory: {}", skills_dir.display());
    }

    // skill name → description (populated from companion JSON if present)
    let mut manifest: Vec<serde_json::Value> = Vec::new();
    // Gather metadata first so we can include it in the manifest even if the
    // WASM file comes later in directory order.
    let mut descriptions: BTreeMap<String, String> = BTreeMap::new();

    for entry in fs::read_dir(skills_dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) == Some("json") {
            if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                if let Ok(raw) = fs::read_to_string(&path) {
                    if let Ok(meta) = serde_json::from_str::<serde_json::Value>(&raw) {
                        let desc = meta["description"]
                            .as_str()
                            .unwrap_or("")
                            .to_string();
                        descriptions.insert(stem.to_string(), desc);
                    }
                }
            }
        }
    }

    let mut skill_count = 0usize;
    for entry in fs::read_dir(skills_dir)? {
        let entry = entry?;
        let path = entry.path();

        let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string();

        match ext {
            "wasm" => {
                let bytes = fs::read(&path)
                    .with_context(|| format!("reading skill {}", path.display()))?;
                let vfs_path = PathBuf::from(format!("/skills/{stem}.wasm"));
                sandbox.mount(vfs_path, bytes);

                let desc = descriptions.get(&stem).cloned().unwrap_or_default();
                manifest.push(serde_json::json!({
                    "name": stem,
                    "description": desc,
                    "wasm": format!("/skills/{stem}.wasm"),
                    "usage": format!("run_skill(name=\"{stem}\", input=\"<your input text>\")"),
                }));
                skill_count += 1;
            }
            "json" => {
                // Mount metadata alongside the WASM for completeness.
                let bytes = fs::read(&path)?;
                sandbox.mount(PathBuf::from(format!("/skills/{stem}.json")), bytes);
            }
            // Ignore other file types (READMEs, etc.) — don't clutter the VFS.
            _ => {}
        }
    }

    // Always write the manifest even if zero skills (empty list is informative).
    manifest.sort_by(|a, b| {
        a["name"].as_str().unwrap_or("").cmp(b["name"].as_str().unwrap_or(""))
    });
    let manifest_json = serde_json::to_string_pretty(&serde_json::json!({
        "skills": manifest,
        "count": skill_count,
        "usage": "Call run_skill(name, input) to execute a skill. Input text is passed as /input.txt inside the skill's environment.",
    }))?;
    sandbox.mount(
        PathBuf::from("/skills/manifest.json"),
        manifest_json.into_bytes(),
    );

    eprintln!("[skills] mounted {skill_count} skill(s) from {}", skills_dir.display());
    Ok(())
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    let config = SandboxConfig {
        max_memory_bytes: cli.max_mem * 1024 * 1024,
        tools: parse_tools(&cli.tools)?,
    };

    let mut sandbox = Sandbox::new(config)?;
    for path in &cli.files {
        let bytes = fs::read(path)
            .with_context(|| format!("reading {}", path.display()))?;
        sandbox.mount(path.clone(), bytes);
    }

    // ── Mount skills directory at /skills/ ───────────────────────────────────
    if let Some(skills_dir) = &cli.skills {
        mount_skills(&mut sandbox, skills_dir)
            .with_context(|| format!("mounting skills from {}", skills_dir.display()))?;
    }

    // ── WASM mode: skip LLM loop, run agent binary directly ──────────────────
    if let Some(agent_path) = &cli.agent {
        let wasm_bytes = fs::read(agent_path)
            .with_context(|| format!("reading agent {}", agent_path.display()))?;
        let output = sandbox.run(&wasm_bytes)?;
        let json = serde_json::json!({
            "stdout": String::from_utf8_lossy(&output.stdout),
            "result": output.result,
        });
        println!("{}", serde_json::to_string_pretty(&json)?);
        return Ok(());
    }

    // Smoke-check the sandbox is alive.
    sandbox.run(NOOP_AGENT_WASM)?;

    // ── LLM agent loop ────────────────────────────────────────────────────────
    let model_label = match &cli.llm {
        LlmChoice::Ollama => format!("ollama/{}", cli.model),
        #[cfg(feature = "llamacpp")]
        LlmChoice::Llamacpp => format!(
            "llamacpp/{}",
            cli.model_path
                .as_ref()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_else(|| "model.gguf".to_string())
        ),
        _ => cli.model.clone(),
    };

    let system = resolve_system(&cli.system)?;

    let backend = build_backend(&cli)?;
    let mut ag = agent::Agent::new(sandbox, backend, &model_label);

    if let Err(e) = ag.run(&cli.goal, &system) {
        eprintln!("\x1b[31m[FATAL] {e}\x1b[0m");
    }

    // Collect any files the LLM wrote during the run.
    // These are the agent's output artefacts — reports, edited docs, CSVs, etc.
    let output_files: std::collections::HashMap<String, String> = ag
        .sandbox
        .list_scratch()
        .into_iter()
        .filter_map(|name| {
            ag.sandbox.read_scratch(&name).map(|bytes| {
                (name, base64::encode(&bytes))
            })
        })
        .collect();

    // Emit trajectory + output files as a single JSON object to stdout.
    let steps: Vec<_> = ag
        .trajectory
        .0
        .iter()
        .map(|s| match s {
            agent::Step::Think(t) =>
                serde_json::json!({"type":"think","text":t}),
            agent::Step::LlmTurn { ms, usage } =>
                serde_json::json!({"type":"llm_turn","ms":ms,"input_tokens":usage.input_tokens,"output_tokens":usage.output_tokens}),
            agent::Step::ToolCall { name, input } =>
                serde_json::json!({"type":"call","tool":name,"input":input}),
            agent::Step::ToolResult { name, output, tool_ms } =>
                serde_json::json!({"type":"result","tool":name,"tool_ms":tool_ms,"output":output}),
            agent::Step::Done(s) =>
                serde_json::json!({"type":"done","summary":s}),
        })
        .collect();

    let out = serde_json::json!({
        "trajectory": steps,
        "output_files": output_files,
    });
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}
