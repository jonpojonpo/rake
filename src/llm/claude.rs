use anyhow::{anyhow, Context, Result};
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{ContentBlock, LlmBackend, LlmResponse, Message, StopReason, ToolDef, Usage};

pub struct ClaudeBackend {
    client: Client,
    api_key: String,
    pub model: String,
}

impl ClaudeBackend {
    pub fn new(api_key: impl Into<String>) -> Self {
        ClaudeBackend {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(180))
                .build()
                .unwrap(),
            api_key: api_key.into(),
            model: "claude-sonnet-4-6".to_string(),
        }
    }

    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model = model.into();
        self
    }
}

// ── Wire types for the Anthropic API ─────────────────────────────────────────

#[derive(Serialize)]
struct ApiRequest<'a> {
    model: &'a str,
    max_tokens: u32,
    system: &'a str,
    tools: &'a [ToolDef],
    messages: &'a [Message],
}

#[derive(Deserialize, Debug)]
struct ApiUsage {
    input_tokens: u32,
    output_tokens: u32,
}

#[derive(Deserialize, Debug)]
struct ApiResponse {
    content: Vec<RawBlock>,
    stop_reason: String,
    usage: Option<ApiUsage>,
}

/// Raw deserialization — the API returns a superset of what we send.
#[derive(Deserialize, Debug)]
struct RawBlock {
    #[serde(rename = "type")]
    kind: String,
    // text block
    text: Option<String>,
    // tool_use block
    id: Option<String>,
    name: Option<String>,
    input: Option<Value>,
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

impl LlmBackend for ClaudeBackend {
    fn complete(
        &self,
        system: &str,
        messages: &[Message],
        tools: &[ToolDef],
    ) -> Result<LlmResponse> {
        let body = ApiRequest {
            model: &self.model,
            max_tokens: 4096,
            system,
            tools,
            messages,
        };

        let resp = self
            .client
            .post("https://api.anthropic.com/v1/messages")
            .header("x-api-key", &self.api_key)
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json")
            .json(&body)
            .send()
            .context("sending request to Anthropic API")?;

        let status = resp.status();
        let bytes = resp.bytes().context("reading response body")?;

        if !status.is_success() {
            let err: ApiError = serde_json::from_slice(&bytes)
                .unwrap_or(ApiError {
                    error: ApiErrorBody {
                        message: String::from_utf8_lossy(&bytes).to_string(),
                    },
                });
            return Err(anyhow!("Anthropic API error {status}: {}", err.error.message));
        }

        let api_resp: ApiResponse =
            serde_json::from_slice(&bytes).context("parsing Anthropic response")?;

        let content = api_resp
            .content
            .into_iter()
            .filter_map(|b| match b.kind.as_str() {
                "text" => Some(ContentBlock::Text {
                    text: b.text.unwrap_or_default(),
                }),
                "tool_use" => Some(ContentBlock::ToolUse {
                    id: b.id.unwrap_or_default(),
                    name: b.name.unwrap_or_default(),
                    input: b.input.unwrap_or(Value::Object(Default::default())),
                }),
                _ => None,
            })
            .collect();

        let usage = api_resp.usage.map_or(Usage::default(), |u| Usage {
            input_tokens: u.input_tokens,
            output_tokens: u.output_tokens,
        });

        Ok(LlmResponse {
            content,
            stop_reason: StopReason::from(api_resp.stop_reason),
            usage,
        })
    }
}
