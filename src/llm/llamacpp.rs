//! Native llama.cpp inference via the `llama-cpp-2` crate.
//!
//! Enable with: `cargo build --features llamacpp`
//! Run with:    `rake --llm llamacpp --model-path /path/to/model.gguf <files>`
//!
//! Tool calling via system-prompt contract + JSON output parsing.
//! Works best with instruction-tuned models (Llama-3.1, Qwen2.5, Mistral-7B-Instruct …).

use std::num::NonZeroU32;
use std::path::Path;

use anyhow::{Context, Result};
use llama_cpp_2::context::params::LlamaContextParams;
use llama_cpp_2::llama_backend::LlamaBackend;
use llama_cpp_2::llama_batch::LlamaBatch;
use llama_cpp_2::model::params::LlamaModelParams;
#[allow(deprecated)]
use llama_cpp_2::model::Special;
use llama_cpp_2::model::{AddBos, LlamaModel};
use llama_cpp_2::sampling::LlamaSampler;

use super::{ContentBlock, LlmBackend, LlmResponse, Message, Role, StopReason, ToolDef, Usage};

pub struct LlamaCppBackend {
    backend: LlamaBackend,
    model: LlamaModel,
    pub n_ctx: u32,
    pub max_new_tokens: u32,
}

impl LlamaCppBackend {
    /// Load a GGUF model from disk.
    pub fn from_gguf(path: impl AsRef<Path>) -> Result<Self> {
        let backend = LlamaBackend::init().context("initialising llama.cpp backend")?;
        let model_params = LlamaModelParams::default();
        let model = LlamaModel::load_from_file(&backend, path.as_ref(), &model_params)
            .context("loading GGUF model")?;
        Ok(LlamaCppBackend {
            backend,
            model,
            n_ctx: 4096,
            max_new_tokens: 2048,
        })
    }
}

// ── Prompt formatting ─────────────────────────────────────────────────────────
//
// ChatML format — understood by most modern instruction-tuned models.
// Tools are injected into the system prompt as JSON.
//
// Tool-call protocol (model must follow):
//   • To call a tool  →  output ONLY a JSON line: {"call":"<name>","args":{…}}
//   • To think/reason  →  plain text

fn build_prompt(system: &str, messages: &[Message], tools: &[ToolDef]) -> String {
    let tools_json = serde_json::to_string_pretty(tools).unwrap_or_default();
    let augmented_system = format!(
        "{system}

## Available tools (JSON schema)
{tools_json}

## Tool-call protocol
- To call a tool, output ONLY this JSON on one line (nothing else):
  {{\"call\": \"<tool_name>\", \"args\": {{<arguments>}}}}
- To finish, call the \"done\" tool.
- For reasoning/analysis, write plain text."
    );

    let mut prompt = format!("<|im_start|>system\n{augmented_system}<|im_end|>\n");

    for msg in messages {
        match msg.role {
            Role::User => {
                let text = msg
                    .content
                    .iter()
                    .map(|b| match b {
                        ContentBlock::Text { text } => text.clone(),
                        ContentBlock::ToolResult { tool_use_id, content } => {
                            format!("[tool_result id={tool_use_id}]\n{content}")
                        }
                        _ => String::new(),
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                prompt.push_str(&format!("<|im_start|>user\n{text}<|im_end|>\n"));
            }
            Role::Assistant => {
                let text = msg
                    .content
                    .iter()
                    .map(|b| match b {
                        ContentBlock::Text { text } => text.clone(),
                        ContentBlock::ToolUse { name, input, .. } => {
                            let args = serde_json::to_string(input).unwrap_or_default();
                            format!("{{\"call\": \"{name}\", \"args\": {args}}}")
                        }
                        _ => String::new(),
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                prompt.push_str(&format!("<|im_start|>assistant\n{text}<|im_end|>\n"));
            }
        }
    }

    prompt.push_str("<|im_start|>assistant\n");
    prompt
}

// ── Response parsing ──────────────────────────────────────────────────────────

fn parse_response(text: &str, tools: &[ToolDef]) -> Vec<ContentBlock> {
    let trimmed = text.trim();

    // Single-line JSON tool call?
    if trimmed.starts_with('{') {
        if let Some(block) = try_parse_tool_call(trimmed, tools) {
            return vec![block];
        }
    }

    // Multi-line: scan for embedded tool calls.
    let mut blocks: Vec<ContentBlock> = Vec::new();
    let mut text_lines: Vec<&str> = Vec::new();

    for line in trimmed.lines() {
        if line.trim_start().starts_with('{') {
            if let Some(block) = try_parse_tool_call(line.trim(), tools) {
                if !text_lines.is_empty() {
                    let t = text_lines.join("\n");
                    if !t.trim().is_empty() {
                        blocks.push(ContentBlock::Text { text: t });
                    }
                    text_lines.clear();
                }
                blocks.push(block);
                continue;
            }
        }
        text_lines.push(line);
    }

    if !text_lines.is_empty() {
        let t = text_lines.join("\n");
        if !t.trim().is_empty() {
            blocks.push(ContentBlock::Text { text: t });
        }
    }

    if blocks.is_empty() {
        blocks.push(ContentBlock::Text { text: trimmed.to_string() });
    }
    blocks
}

fn try_parse_tool_call(s: &str, tools: &[ToolDef]) -> Option<ContentBlock> {
    let v: serde_json::Value = serde_json::from_str(s).ok()?;
    let name = v["call"].as_str()?;
    if !tools.iter().any(|t| t.name == name) {
        return None;
    }
    let args = v.get("args").cloned().unwrap_or(serde_json::Value::Object(Default::default()));
    Some(ContentBlock::ToolUse {
        id: format!("lc-{:08x}", pseudo_rand()),
        name: name.to_string(),
        input: args,
    })
}

fn pseudo_rand() -> u32 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos()
}

// ── LlmBackend impl ──────────────────────────────────────────────────────────

impl LlmBackend for LlamaCppBackend {
    fn complete(
        &self,
        system: &str,
        messages: &[Message],
        tools: &[ToolDef],
    ) -> Result<LlmResponse> {
        let prompt = build_prompt(system, messages, tools);

        let ctx_params = LlamaContextParams::default()
            .with_n_ctx(NonZeroU32::new(self.n_ctx));
        let mut ctx = self
            .model
            .new_context(&self.backend, ctx_params)
            .context("creating llama.cpp context")?;

        // Tokenise prompt.
        let prompt_tokens = self
            .model
            .str_to_token(&prompt, AddBos::Always)
            .context("tokenising prompt")?;
        let n_prompt = prompt_tokens.len();

        // Prefill batch.
        let mut batch = LlamaBatch::new(n_prompt.max(512), 1);
        for (i, &tok) in prompt_tokens.iter().enumerate() {
            batch.add(tok, i as i32, &[0], i == n_prompt - 1)?;
        }
        ctx.decode(&mut batch).context("prefill decode")?;

        // Autoregressive generation with a greedy sampler.
        let mut sampler = LlamaSampler::greedy();
        let mut output_tokens = Vec::new();
        let mut n_cur = n_prompt;
        let mut raw_output = String::new();

        loop {
            if output_tokens.len() >= self.max_new_tokens as usize {
                break;
            }

            let next_tok = sampler.sample(&ctx, batch.n_tokens() - 1);
            sampler.accept(next_tok);

            if next_tok == self.model.token_eos() {
                break;
            }

            output_tokens.push(next_tok);

            // Decode this token to check for stop-markers.
            // token_to_str is deprecated upstream in favour of token_to_piece
            // (which needs an encoding_rs::Decoder); suppress the lint here.
            #[allow(deprecated)]
            let piece = self.model.token_to_str(next_tok, Special::Tokenize).unwrap_or_default();
            raw_output.push_str(&piece);

            if raw_output.contains("<|im_end|>") || raw_output.contains("</s>") {
                break;
            }

            batch.clear();
            batch.add(next_tok, n_cur as i32, &[0], true)?;
            n_cur += 1;
            ctx.decode(&mut batch).context("generation decode")?;
        }

        let text = raw_output
            .trim_end_matches("<|im_end|>")
            .trim_end_matches("</s>")
            .trim()
            .to_string();

        let content = parse_response(&text, tools);
        let stop_reason = if content.iter().any(|b| matches!(b, ContentBlock::ToolUse { .. })) {
            StopReason::ToolUse
        } else {
            StopReason::EndTurn
        };

        let usage = Usage {
            input_tokens: n_prompt as u32,
            output_tokens: output_tokens.len() as u32,
        };

        Ok(LlmResponse { content, stop_reason, usage })
    }
}
