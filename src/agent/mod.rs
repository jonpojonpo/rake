use std::time::Instant;

pub mod tools;

use anyhow::Result;
use serde_json::Value;

use crate::llm::{ContentBlock, LlmBackend, Message, StopReason, ToolDef, Usage};
use crate::sandbox::Sandbox;

// ── Trajectory types ──────────────────────────────────────────────────────────

#[derive(Debug)]
pub enum Step {
    Think(String),
    /// Fired once per LLM round-trip, immediately before the tool-call steps it produced.
    LlmTurn { ms: u64, usage: Usage },
    ToolCall { name: String, input: Value },
    ToolResult { name: String, output: String, tool_ms: u64 },
    Done(String),
}

pub struct Trajectory(pub Vec<Step>);

impl Trajectory {
    fn push(&mut self, step: Step) {
        self.0.push(step);
    }
}

// ── Display helpers (ANSI) ────────────────────────────────────────────────────

const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const CYAN: &str = "\x1b[36m";
const GREEN: &str = "\x1b[32m";
const YELLOW: &str = "\x1b[33m";
const MAGENTA: &str = "\x1b[35m";

fn print_header(n_files: usize, model: &str) {
    let line = "─".repeat(58);
    eprintln!("{BOLD}{line}{RESET}");
    eprintln!("{BOLD} rake  —  {n_files} file(s) mounted   model: {model}{RESET}");
    eprintln!("{BOLD}{line}{RESET}");
}

fn fmt_ms(ms: u64) -> String {
    if ms < 1000 {
        format!("{ms}ms")
    } else {
        format!("{:.2}s", ms as f64 / 1000.0)
    }
}

fn fmt_tokens(usage: &Usage) -> String {
    format!(
        "↑{} ↓{} tok",
        fmt_num(usage.input_tokens),
        fmt_num(usage.output_tokens),
    )
}

fn fmt_num(n: u32) -> String {
    // insert thousands separators
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().rev().enumerate() {
        if i > 0 && i % 3 == 0 { out.push(','); }
        out.push(c);
    }
    out.chars().rev().collect()
}

fn print_think(step: usize, text: &str) {
    let first_line = text.lines().next().unwrap_or("").trim();
    let truncated = if first_line.len() > 110 {
        format!("{}…", &first_line[..109])
    } else {
        first_line.to_string()
    };
    eprintln!("{DIM}[{step}] THINK  {truncated}{RESET}");
}

fn print_llm_turn(ms: u64, usage: &Usage) {
    eprintln!(
        "{DIM}       llm: {}  {}{RESET}",
        fmt_ms(ms),
        fmt_tokens(usage)
    );
}

fn print_call(step: usize, name: &str, input: &Value) {
    let args = format_input(input);
    eprintln!("{CYAN}{BOLD}[{step}] CALL   {name}({args}){RESET}");
}

fn print_result(step: usize, name: &str, output: &str, tool_ms: u64) {
    let preview = output.lines().next().unwrap_or("").trim();
    let preview = if preview.len() > 110 {
        format!("{}…", &preview[..109])
    } else {
        preview.to_string()
    };
    let lines = output.lines().count();
    let line_hint = if lines > 1 {
        format!("  {DIM}(+{} lines){RESET}", lines - 1)
    } else {
        String::new()
    };
    eprintln!(
        "{GREEN}[{step}] RESULT {name} → {preview}{line_hint}  {DIM}{}{RESET}",
        fmt_ms(tool_ms)
    );
}

fn print_done(summary: &str, elapsed_ms: u64, cumulative: &Usage) {
    let line = "─".repeat(58);
    eprintln!("\n{MAGENTA}{BOLD}{line}");
    eprintln!(" DONE");
    eprintln!("{line}{RESET}");
    for l in summary.lines() {
        eprintln!("{YELLOW} {l}{RESET}");
    }
    eprintln!(
        "\n{DIM} total: {}  {}  ({} total tok){RESET}\n",
        fmt_ms(elapsed_ms),
        fmt_tokens(cumulative),
        fmt_num(cumulative.total()),
    );
}

fn print_error(msg: &str) {
    eprintln!("\x1b[31m[ERR]  {msg}{RESET}");
}

fn format_input(v: &Value) -> String {
    match v {
        Value::Object(map) => map
            .iter()
            .map(|(k, v)| format!("{k}={}", short_val(v)))
            .collect::<Vec<_>>()
            .join(", "),
        _ => v.to_string(),
    }
}

fn short_val(v: &Value) -> String {
    match v {
        Value::String(s) => {
            if s.len() > 40 {
                format!("\"{}…\"", &s[..39])
            } else {
                format!("\"{s}\"")
            }
        }
        other => other.to_string(),
    }
}

// ── Tool definitions exposed to the LLM ──────────────────────────────────────

fn agent_tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "list_files",
            "List all files available in the sandbox.",
            serde_json::json!({ "type": "object", "properties": {} }),
        ),
        ToolDef::new(
            "read_file",
            "Read the full contents of a file.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path": { "type": "string", "description": "File path" }
                },
                "required": ["path"]
            }),
        ),
        ToolDef::new(
            "grep_files",
            "Search files with a regex pattern. Returns JSON array of {path, line_number, line}.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "pattern":     { "type": "string", "description": "Regex pattern" },
                    "path_filter": { "type": "string", "description": "Filter files by path substring; empty = all files" }
                },
                "required": ["pattern"]
            }),
        ),
        ToolDef::new(
            "write_file",
            "Write content to a file in the sandbox scratch space. \
             Use this to produce output artefacts for the user: \
             reports (report.md), extracted tables (data.csv), edited documents, summaries. \
             All files written here are returned as downloadable outputs alongside the summary. \
             Prefer structured outputs (markdown, CSV, JSON) over embedding everything in the summary.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path":    { "type": "string", "description": "Destination path (e.g. report.md, summary.csv)" },
                    "content": { "type": "string", "description": "File content" }
                },
                "required": ["path", "content"]
            }),
        ),
        ToolDef::new(
            "head",
            "Read the first N lines of a file. Use before read_file to preview large files.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path":  { "type": "string" },
                    "lines": { "type": "integer", "description": "Lines to return (default 30)" }
                },
                "required": ["path"]
            }),
        ),
        ToolDef::new(
            "read_section",
            "Read a specific line range from a file (1-indexed, inclusive). \
             ALWAYS prefer this over read_file for large documents. \
             Check the _index.md file first to find section line ranges, \
             then call read_section to fetch only the section you need. \
             Example: read_section(path='report.md', start_line=100, end_line=235) \
             reads only the Chairman's Statement without loading the full document.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path":       { "type": "string", "description": "File path" },
                    "start_line": { "type": "integer", "description": "First line to read (1-indexed)" },
                    "end_line":   { "type": "integer", "description": "Last line to read (inclusive)" }
                },
                "required": ["path", "start_line", "end_line"]
            }),
        ),
        ToolDef::new(
            "file_info",
            "Return metadata about a file: size, line count, extension, mime type.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path": { "type": "string" }
                },
                "required": ["path"]
            }),
        ),
        ToolDef::new(
            "csv_stats",
            "Parse a CSV file and return: column names, types (numeric/categorical/text), \
             stats, row count, and a sample of rows. \
             Numeric columns → min/max/mean/sum. \
             Categorical columns (short values) → unique value list. \
             Text columns (avg value length > 50 chars) → avg/max length and longest samples. \
             Always call this first when analysing CSV files — do NOT use read_file on CSVs. \
             For large text-heavy CSVs use csv_rows for sliding-window analysis after this.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path":        { "type": "string" },
                    "sample_rows": { "type": "integer", "description": "Sample rows to include (default 5)" }
                },
                "required": ["path"]
            }),
        ),
        ToolDef::new(
            "csv_rows",
            "Read a row range from a CSV file as JSON objects — the sliding-window tool \
             for large or text-heavy CSVs. \
             Call csv_stats first to learn row_count and column types, \
             then iterate: csv_rows(path, 0, 100), csv_rows(path, 100, 200), etc. \
             Rows are 0-indexed (first data row is 0, headers excluded).",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path":      { "type": "string" },
                    "start_row": { "type": "integer", "description": "First row to return (0-indexed, inclusive)" },
                    "end_row":   { "type": "integer", "description": "Last row (exclusive) — returns rows [start_row, end_row)" }
                },
                "required": ["path", "start_row", "end_row"]
            }),
        ),
        ToolDef::new(
            "json_query",
            "Query a JSON file using JSON Pointer syntax (e.g. /users/0/name) \
             or dot-path shorthand (users.0.name). \
             Pass an empty string to get a structural overview of the root.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "path":    { "type": "string" },
                    "pointer": { "type": "string", "description": "JSON Pointer or dot-path (empty = root summary)" }
                },
                "required": ["path", "pointer"]
            }),
        ),
        ToolDef::new(
            "done",
            "Signal completion. Call this when analysis is finished.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "summary": { "type": "string", "description": "Full findings summary in Markdown" }
                },
                "required": ["summary"]
            }),
        ),
    ]
}

// ── Agent ─────────────────────────────────────────────────────────────────────

pub struct Agent {
    pub sandbox: Sandbox,
    pub backend: Box<dyn LlmBackend>,
    pub model_name: String,
    pub trajectory: Trajectory,
}

impl Agent {
    pub fn new(sandbox: Sandbox, backend: Box<dyn LlmBackend>, model_name: &str) -> Self {
        Agent {
            sandbox,
            backend,
            model_name: model_name.to_string(),
            trajectory: Trajectory(Vec::new()),
        }
    }

    pub const DEFAULT_SYSTEM: &'static str = "\
You are a thorough code and data analysis agent operating inside an isolated sandbox.
You have access to tools that let you read and search files.
Analyse the provided files carefully. Look for bugs, security issues, TODOs, \
patterns, and anything noteworthy.
When you have completed a thorough analysis, call `done` with a detailed Markdown summary.";

    pub fn run(&mut self, goal: &str, system: &str) -> Result<()> {
        let files = self.sandbox.list_files();
        print_header(files.len(), &self.model_name);
        let tools = agent_tools();
        self.trajectory = Trajectory(Vec::new());

        let file_list = files
            .iter()
            .map(|p| format!("  - {p}"))
            .collect::<Vec<_>>()
            .join("\n");
        let initial_text = format!(
            "{goal}\n\nMounted files:\n{file_list}\n\nBegin your analysis."
        );

        let mut messages: Vec<Message> = vec![Message::user(initial_text)];
        let mut step = 0usize;
        let mut cumulative = Usage::default();
        let run_start = Instant::now();

        loop {
            let llm_start = Instant::now();
            let resp = self.backend.complete(system, &messages, &tools)?;
            let llm_ms = llm_start.elapsed().as_millis() as u64;

            cumulative.add(&resp.usage);

            // Record assistant turn
            messages.push(Message {
                role: crate::llm::Role::Assistant,
                content: resp.content.clone(),
            });

            let mut tool_results: Vec<ContentBlock> = Vec::new();
            let mut finished = false;
            let mut turn_printed = false; // print LLM telemetry once per turn

            for block in &resp.content {
                match block {
                    ContentBlock::Text { text } if !text.trim().is_empty() => {
                        step += 1;
                        print_think(step, text);
                        print_llm_turn(llm_ms, &resp.usage);
                        turn_printed = true;
                        self.trajectory.push(Step::Think(text.clone()));
                        self.trajectory.push(Step::LlmTurn {
                            ms: llm_ms,
                            usage: resp.usage.clone(),
                        });
                    }
                    ContentBlock::ToolUse { id, name, input } => {
                        step += 1;
                        // If this turn had no Think block, emit telemetry before first call.
                        if !turn_printed {
                            print_llm_turn(llm_ms, &resp.usage);
                            self.trajectory.push(Step::LlmTurn {
                                ms: llm_ms,
                                usage: resp.usage.clone(),
                            });
                            turn_printed = true;
                        }
                        print_call(step, name, input);
                        self.trajectory.push(Step::ToolCall {
                            name: name.clone(),
                            input: input.clone(),
                        });

                        let tool_start = Instant::now();
                        let output = match self.execute_tool(name, input) {
                            Ok(o) => o,
                            Err(e) => {
                                let msg = format!("error: {e}");
                                print_error(&msg);
                                msg
                            }
                        };
                        let tool_ms = tool_start.elapsed().as_millis() as u64;

                        if name == "done" {
                            print_done(&output, run_start.elapsed().as_millis() as u64, &cumulative);
                            self.trajectory.push(Step::Done(output.clone()));
                            finished = true;
                        } else {
                            print_result(step, name, &output, tool_ms);
                            self.trajectory.push(Step::ToolResult {
                                name: name.clone(),
                                output: output.clone(),
                                tool_ms,
                            });
                        }

                        tool_results.push(ContentBlock::tool_result(id, output));
                    }
                    _ => {}
                }
            }

            if finished || resp.stop_reason == StopReason::EndTurn {
                break;
            }

            // Feed tool results back
            messages.push(Message::tool_results(tool_results));
        }

        Ok(())
    }

    fn execute_tool(&self, name: &str, input: &Value) -> Result<String> {
        match name {
            "list_files" => {
                let files = self.sandbox.list_files();
                Ok(serde_json::to_string_pretty(&files)?)
            }
            "read_file" => {
                let path = input["path"]
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("read_file requires path"))?;
                match self.sandbox.read_file(path) {
                    Some(bytes) => Ok(String::from_utf8_lossy(&bytes).to_string()),
                    None => Err(anyhow::anyhow!("file not found: {path}")),
                }
            }
            "grep_files" => {
                let pattern = input["pattern"]
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("grep_files requires pattern"))?;
                let filter = input["path_filter"].as_str().unwrap_or("");
                let matches = self.sandbox.grep(pattern, filter)?;
                Ok(serde_json::to_string_pretty(&matches)?)
            }
            "write_file" => {
                let path = input["path"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("write_file requires path"))?;
                let content = input["content"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("write_file requires content"))?;
                tools::write_file(&self.sandbox, path, content)
            }
            "head" => {
                let path = input["path"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("head requires path"))?;
                let n = input["lines"].as_u64().unwrap_or(30) as usize;
                tools::head(&self.sandbox, path, n)
            }
            "read_section" => {
                let path = input["path"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("read_section requires path"))?;
                let start = input["start_line"].as_u64()
                    .ok_or_else(|| anyhow::anyhow!("read_section requires start_line"))? as usize;
                let end = input["end_line"].as_u64()
                    .ok_or_else(|| anyhow::anyhow!("read_section requires end_line"))? as usize;
                tools::read_section(&self.sandbox, path, start, end)
            }
            "file_info" => {
                let path = input["path"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("file_info requires path"))?;
                tools::file_info(&self.sandbox, path)
            }
            "csv_stats" => {
                let path = input["path"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("csv_stats requires path"))?;
                let sample = input["sample_rows"].as_u64().unwrap_or(5) as usize;
                tools::csv_stats(&self.sandbox, path, sample)
            }
            "csv_rows" => {
                let path = input["path"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("csv_rows requires path"))?;
                let start = input["start_row"].as_u64()
                    .ok_or_else(|| anyhow::anyhow!("csv_rows requires start_row"))? as usize;
                let end = input["end_row"].as_u64()
                    .ok_or_else(|| anyhow::anyhow!("csv_rows requires end_row"))? as usize;
                tools::csv_rows(&self.sandbox, path, start, end)
            }
            "json_query" => {
                let path = input["path"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("json_query requires path"))?;
                let pointer = input["pointer"].as_str().unwrap_or("");
                tools::json_query(&self.sandbox, path, pointer)
            }
            "done" => {
                let summary = input["summary"]
                    .as_str()
                    .unwrap_or("(no summary provided)")
                    .to_string();
                Ok(summary)
            }
            other => Err(anyhow::anyhow!("unknown tool: {other}")),
        }
    }
}
