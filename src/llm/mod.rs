pub mod claude;
pub mod openai;
#[cfg(feature = "llamacpp")]
pub mod llamacpp;

use anyhow::Result;
use serde::{Deserialize, Serialize};

// ── Message types matching the Anthropic wire format ─────────────────────────

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
}

/// A single content block inside a message.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ContentBlock {
    Text {
        text: String,
    },
    ToolUse {
        id: String,
        name: String,
        input: serde_json::Value,
    },
    ToolResult {
        tool_use_id: String,
        content: String,
    },
}

impl ContentBlock {
    pub fn text(s: impl Into<String>) -> Self {
        ContentBlock::Text { text: s.into() }
    }
    pub fn tool_result(tool_use_id: impl Into<String>, content: impl Into<String>) -> Self {
        ContentBlock::ToolResult {
            tool_use_id: tool_use_id.into(),
            content: content.into(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: Role,
    pub content: Vec<ContentBlock>,
}

impl Message {
    pub fn user(text: impl Into<String>) -> Self {
        Message {
            role: Role::User,
            content: vec![ContentBlock::text(text)],
        }
    }
    pub fn tool_results(results: Vec<ContentBlock>) -> Self {
        Message {
            role: Role::User,
            content: results,
        }
    }
}

// ── Tool definitions ──────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolDef {
    pub name: String,
    pub description: String,
    pub input_schema: serde_json::Value,
}

impl ToolDef {
    pub fn new(
        name: impl Into<String>,
        description: impl Into<String>,
        schema: serde_json::Value,
    ) -> Self {
        ToolDef {
            name: name.into(),
            description: description.into(),
            input_schema: schema,
        }
    }
}

// ── LLM response / backend trait ─────────────────────────────────────────────

#[derive(Debug, PartialEq)]
pub enum StopReason {
    ToolUse,
    EndTurn,
    Other(String),
}

impl From<String> for StopReason {
    fn from(s: String) -> Self {
        match s.as_str() {
            "tool_use" => StopReason::ToolUse,
            "end_turn" => StopReason::EndTurn,
            _ => StopReason::Other(s),
        }
    }
}

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct Usage {
    pub input_tokens: u32,
    pub output_tokens: u32,
}

impl Usage {
    pub fn total(&self) -> u32 {
        self.input_tokens + self.output_tokens
    }
    pub fn add(&mut self, other: &Usage) {
        self.input_tokens += other.input_tokens;
        self.output_tokens += other.output_tokens;
    }
}

#[derive(Debug)]
pub struct LlmResponse {
    pub content: Vec<ContentBlock>,
    pub stop_reason: StopReason,
    pub usage: Usage,
}

pub trait LlmBackend: Send + Sync {
    fn complete(
        &self,
        system: &str,
        messages: &[Message],
        tools: &[ToolDef],
    ) -> Result<LlmResponse>;
}

// ── Noop backend (for testing without a real LLM) ────────────────────────────

pub struct NoopBackend;

impl LlmBackend for NoopBackend {
    fn complete(
        &self,
        _system: &str,
        _messages: &[Message],
        _tools: &[ToolDef],
    ) -> Result<LlmResponse> {
        Ok(LlmResponse {
            content: vec![ContentBlock::ToolUse {
                id: "noop-1".to_string(),
                name: "done".to_string(),
                input: serde_json::json!({ "summary": "noop backend — no analysis performed" }),
            }],
            stop_reason: StopReason::ToolUse,
            usage: Usage::default(),
        })
    }
}
