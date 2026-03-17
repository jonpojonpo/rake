use anyhow::{anyhow, Context, Result};
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::{ContentBlock, LlmBackend, LlmResponse, Message, Role, StopReason, ToolDef, Usage};

pub struct OpenAiBackend {
    client: Client,
    api_key: String,
    pub model: String,
    pub base_url: String,
}

impl OpenAiBackend {
    pub fn new(api_key: impl Into<String>, model: impl Into<String>) -> Self {
        OpenAiBackend {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(180))
                .build()
                .unwrap(),
            api_key: api_key.into(),
            model: model.into(),
            base_url: "https://api.openai.com/v1".to_string(),
        }
    }

    pub fn with_base_url(mut self, url: impl Into<String>) -> Self {
        self.base_url = url.into().trim_end_matches('/').to_string();
        self
    }

    /// Convenience constructor for Ollama (no API key needed).
    pub fn ollama(model: impl Into<String>) -> Self {
        OpenAiBackend {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(300))
                .build()
                .unwrap(),
            api_key: "ollama".to_string(), // Ollama ignores the key
            model: model.into(),
            base_url: "http://localhost:11434/v1".to_string(),
        }
    }
}

// ── Anthropic → OpenAI format conversion ─────────────────────────────────────

/// Convert our internal (Anthropic-style) messages to OpenAI wire format.
/// The system prompt becomes the first message.
fn to_openai_messages(system: &str, messages: &[Message]) -> Vec<Value> {
    let mut out = vec![json!({"role": "system", "content": system})];

    for msg in messages {
        match &msg.role {
            Role::User => {
                // Separate tool-result blocks from regular text blocks.
                let tool_results: Vec<&ContentBlock> = msg
                    .content
                    .iter()
                    .filter(|b| matches!(b, ContentBlock::ToolResult { .. }))
                    .collect();
                let text_blocks: Vec<&ContentBlock> = msg
                    .content
                    .iter()
                    .filter(|b| matches!(b, ContentBlock::Text { .. }))
                    .collect();

                // OpenAI wants one "tool" message per tool result.
                for block in &tool_results {
                    if let ContentBlock::ToolResult { tool_use_id, content } = block {
                        out.push(json!({
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": content,
                        }));
                    }
                }

                // Regular user text (only if present alongside or instead of tool results).
                if !text_blocks.is_empty() {
                    let text = text_blocks
                        .iter()
                        .filter_map(|b| {
                            if let ContentBlock::Text { text } = b {
                                Some(text.as_str())
                            } else {
                                None
                            }
                        })
                        .collect::<Vec<_>>()
                        .join("\n");
                    out.push(json!({"role": "user", "content": text}));
                }
            }

            Role::Assistant => {
                let text: String = msg
                    .content
                    .iter()
                    .filter_map(|b| {
                        if let ContentBlock::Text { text } = b {
                            Some(text.as_str())
                        } else {
                            None
                        }
                    })
                    .collect::<Vec<_>>()
                    .join("\n");

                let tool_calls: Vec<Value> = msg
                    .content
                    .iter()
                    .filter_map(|b| {
                        if let ContentBlock::ToolUse { id, name, input } = b {
                            Some(json!({
                                "id": id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    // OpenAI wants arguments as a JSON string, not object.
                                    "arguments": serde_json::to_string(input).unwrap_or_default(),
                                }
                            }))
                        } else {
                            None
                        }
                    })
                    .collect();

                let mut m = json!({
                    "role": "assistant",
                    "content": if text.is_empty() { Value::Null } else { json!(text) },
                });
                if !tool_calls.is_empty() {
                    m["tool_calls"] = json!(tool_calls);
                }
                out.push(m);
            }
        }
    }

    out
}

/// Convert Anthropic-style ToolDef to OpenAI function-tool format.
fn to_openai_tools(tools: &[ToolDef]) -> Vec<Value> {
    tools
        .iter()
        .map(|t| {
            json!({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                }
            })
        })
        .collect()
}

// ── OpenAI wire types ─────────────────────────────────────────────────────────

#[derive(Serialize)]
struct ApiRequest {
    model: String,
    messages: Vec<Value>,
    tools: Vec<Value>,
}

#[derive(Deserialize, Debug)]
struct ApiResponse {
    choices: Vec<Choice>,
    usage: Option<OaiUsage>,
}

#[derive(Deserialize, Debug)]
struct Choice {
    message: OaiMessage,
    finish_reason: Option<String>,
}

#[derive(Deserialize, Debug)]
struct OaiMessage {
    content: Option<String>,
    tool_calls: Option<Vec<OaiToolCall>>,
}

#[derive(Deserialize, Debug)]
struct OaiToolCall {
    id: String,
    function: OaiFunction,
}

#[derive(Deserialize, Debug)]
struct OaiFunction {
    name: String,
    arguments: String, // JSON string
}

#[derive(Deserialize, Debug)]
struct OaiUsage {
    prompt_tokens: u32,
    completion_tokens: u32,
}

#[derive(Deserialize, Debug)]
struct ApiError {
    error: ApiErrorBody,
}
#[derive(Deserialize, Debug)]
struct ApiErrorBody {
    message: String,
}

// ── LlmBackend impl ──────────────────────────────────────────────────────────

impl LlmBackend for OpenAiBackend {
    fn complete(
        &self,
        system: &str,
        messages: &[Message],
        tools: &[ToolDef],
    ) -> Result<LlmResponse> {
        let body = ApiRequest {
            model: self.model.clone(),
            messages: to_openai_messages(system, messages),
            tools: to_openai_tools(tools),
        };

        let url = format!("{}/chat/completions", self.base_url);

        let resp = self
            .client
            .post(&url)
            .header("Authorization", format!("Bearer {}", self.api_key))
            .header("content-type", "application/json")
            .json(&body)
            .send()
            .context("sending request to OpenAI-compatible API")?;

        let status = resp.status();
        let bytes = resp.bytes().context("reading response body")?;

        if !status.is_success() {
            let err: ApiError = serde_json::from_slice(&bytes).unwrap_or(ApiError {
                error: ApiErrorBody {
                    message: String::from_utf8_lossy(&bytes).to_string(),
                },
            });
            return Err(anyhow!("API error {status}: {}", err.error.message));
        }

        let api_resp: ApiResponse =
            serde_json::from_slice(&bytes).context("parsing OpenAI response")?;

        let choice = api_resp
            .choices
            .into_iter()
            .next()
            .ok_or_else(|| anyhow!("empty choices array"))?;

        let mut content: Vec<ContentBlock> = Vec::new();

        if let Some(text) = choice.message.content.filter(|t| !t.is_empty()) {
            content.push(ContentBlock::Text { text });
        }
        if let Some(tool_calls) = choice.message.tool_calls {
            for tc in tool_calls {
                let input: Value = serde_json::from_str(&tc.function.arguments)
                    .unwrap_or(Value::Object(Default::default()));
                content.push(ContentBlock::ToolUse {
                    id: tc.id,
                    name: tc.function.name,
                    input,
                });
            }
        }

        let stop_reason = match choice.finish_reason.as_deref() {
            Some("tool_calls") => StopReason::ToolUse,
            Some("stop") => StopReason::EndTurn,
            other => StopReason::Other(other.unwrap_or("").to_string()),
        };

        let usage = api_resp.usage.map_or(Usage::default(), |u| Usage {
            input_tokens: u.prompt_tokens,
            output_tokens: u.completion_tokens,
        });

        Ok(LlmResponse { content, stop_reason, usage })
    }
}
