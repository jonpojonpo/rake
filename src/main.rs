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

/// Parse the YAML frontmatter from a SKILL.md file.
///
/// Frontmatter is a block delimited by `---` on its own line at the very start
/// of the file.  We extract only the `name:` and `description:` fields — no
/// full YAML parser required.
fn parse_skill_frontmatter(content: &str) -> (String, String) {
    let mut name = String::new();
    let mut description = String::new();

    // Must start with "---\n" (or "---\r\n")
    let body = if let Some(rest) = content.strip_prefix("---\n").or_else(|| content.strip_prefix("---\r\n")) {
        rest
    } else {
        return (name, description);
    };

    // Find the closing "---"
    let fm_end = body
        .find("\n---\n")
        .or_else(|| body.find("\n---\r\n"))
        .or_else(|| body.find("\n---")) // end-of-file variant
        .unwrap_or(body.len());

    for line in body[..fm_end].lines() {
        if let Some(v) = line.strip_prefix("name:") {
            name = v.trim().trim_matches('"').trim_matches('\'').to_string();
        } else if let Some(v) = line.strip_prefix("description:") {
            description = v.trim().trim_matches('"').trim_matches('\'').to_string();
        }
    }

    (name, description)
}

/// Recursively mount every file under `dir` into the sandbox at the VFS prefix
/// `vfs_prefix/<relative-path>`.
fn mount_dir_recursive(
    sandbox: &mut Sandbox,
    dir: &std::path::Path,
    vfs_prefix: &str,
) -> Result<usize> {
    let mut count = 0;
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            let sub = format!(
                "{vfs_prefix}/{}",
                path.file_name().unwrap().to_string_lossy()
            );
            count += mount_dir_recursive(sandbox, &path, &sub)?;
        } else {
            let bytes = fs::read(&path)
                .with_context(|| format!("reading {}", path.display()))?;
            let vfs_path = PathBuf::from(format!(
                "{vfs_prefix}/{}",
                path.file_name().unwrap().to_string_lossy()
            ));
            sandbox.mount(vfs_path, bytes);
            count += 1;
        }
    }
    Ok(count)
}

/// Implement the Agent Skills (agentskills.io) standard.
///
/// Each skill is a sub-directory of `skills_dir` containing a `SKILL.md` file
/// with YAML frontmatter (`name`, `description`) followed by Markdown
/// instructions.  Additional assets and scripts in the sub-directory are
/// mounted alongside it.
///
/// Progressive disclosure layout inside the sandbox:
///
///   /skills/manifest.json          ← discovery: name + description for all skills
///   /skills/<name>/SKILL.md        ← activation: full instructions
///   /skills/<name>/scripts/…       ← on-demand: helper scripts / assets
///
/// The agent discovers skills by reading manifest.json, then calls
/// `use_skill(name)` to load the full SKILL.md into context before following
/// its instructions.
fn mount_skills(sandbox: &mut Sandbox, skills_dir: &std::path::Path) -> Result<()> {
    if !skills_dir.is_dir() {
        anyhow::bail!("skills path is not a directory: {}", skills_dir.display());
    }

    let mut manifest: Vec<serde_json::Value> = Vec::new();
    let mut skill_count = 0usize;

    for entry in fs::read_dir(skills_dir)? {
        let entry = entry?;
        let path = entry.path();

        if !path.is_dir() {
            continue; // top-level files are ignored; skills must be directories
        }

        let dir_name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_string();

        let skill_md_path = path.join("SKILL.md");
        if !skill_md_path.exists() {
            // Not a skill directory — skip silently.
            continue;
        }

        let skill_md = fs::read_to_string(&skill_md_path)
            .with_context(|| format!("reading {}", skill_md_path.display()))?;

        let (fm_name, fm_description) = parse_skill_frontmatter(&skill_md);

        // The canonical skill name is the frontmatter `name` field; fall back to
        // the directory name if the frontmatter is missing or malformed.
        let skill_name = if fm_name.is_empty() { dir_name.clone() } else { fm_name };

        // Mount every file in the skill directory recursively.
        let vfs_prefix = format!("/skills/{skill_name}");
        mount_dir_recursive(sandbox, &path, &vfs_prefix)?;
        skill_count += 1;

        manifest.push(serde_json::json!({
            "name":        skill_name,
            "description": fm_description,
            "skill_md":    format!("/skills/{skill_name}/SKILL.md"),
            "usage":       format!("use_skill(name=\"{skill_name}\")"),
        }));
    }

    manifest.sort_by(|a, b| {
        a["name"].as_str().unwrap_or("").cmp(b["name"].as_str().unwrap_or(""))
    });

    let manifest_json = serde_json::to_string_pretty(&serde_json::json!({
        "skills": manifest,
        "count":  skill_count,
        "how_to_use": [
            "1. Read this manifest to discover available skills.",
            "2. Call use_skill(name) to load a skill's full instructions into context.",
            "3. Follow the skill's instructions. Access companion scripts/assets via read_file."
        ],
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
