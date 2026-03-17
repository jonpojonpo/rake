use anyhow::{anyhow, Result};
use serde_json::{json, Value};

use crate::sandbox::Sandbox;

// ── write_file ────────────────────────────────────────────────────────────────

pub fn write_file(sandbox: &Sandbox, path: &str, content: &str) -> Result<String> {
    sandbox.write_scratch(path, content.as_bytes().to_vec());
    Ok(format!("wrote {} bytes to {path}", content.len()))
}

// ── head ─────────────────────────────────────────────────────────────────────

pub fn head(sandbox: &Sandbox, path: &str, n: usize) -> Result<String> {
    let bytes = sandbox
        .read_file(path)
        .ok_or_else(|| anyhow!("file not found: {path}"))?;
    let text = String::from_utf8_lossy(&bytes);
    let lines: Vec<&str> = text.lines().take(n).collect();
    let total = text.lines().count();
    let mut out = lines.join("\n");
    if total > n {
        out.push_str(&format!("\n… ({} more lines)", total - n));
    }
    Ok(out)
}

// ── file_info ─────────────────────────────────────────────────────────────────

pub fn file_info(sandbox: &Sandbox, path: &str) -> Result<String> {
    let bytes = sandbox
        .read_file(path)
        .ok_or_else(|| anyhow!("file not found: {path}"))?;
    let size = bytes.len();
    let line_count = bytes.iter().filter(|&&b| b == b'\n').count();
    let ext = std::path::Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("unknown");
    let mime = ext_to_mime(ext);
    let printable = bytes.iter().take(512).all(|&b| b.is_ascii() || b >= 0x80);
    Ok(serde_json::to_string_pretty(&json!({
        "path": path,
        "size_bytes": size,
        "line_count": line_count,
        "extension": ext,
        "mime_guess": mime,
        "appears_text": printable,
    }))?)
}

fn ext_to_mime(ext: &str) -> &'static str {
    match ext {
        "py"                     => "text/x-python",
        "rs"                     => "text/x-rust",
        "js" | "mjs"             => "text/javascript",
        "ts"                     => "text/typescript",
        "json"                   => "application/json",
        "csv"                    => "text/csv",
        "toml"                   => "text/x-toml",
        "yaml" | "yml"           => "text/yaml",
        "md"                     => "text/markdown",
        "html" | "htm"           => "text/html",
        "sh" | "bash"            => "text/x-shellscript",
        "go"                     => "text/x-go",
        "c" | "h"                => "text/x-c",
        "cpp" | "cc" | "cxx"     => "text/x-c++",
        "java"                   => "text/x-java",
        "rb"                     => "text/x-ruby",
        "sql"                    => "text/x-sql",
        "xml"                    => "application/xml",
        "parquet"                => "application/octet-stream",
        _                        => "application/octet-stream",
    }
}

// ── csv_stats ─────────────────────────────────────────────────────────────────

pub fn csv_stats(sandbox: &Sandbox, path: &str, sample_rows: usize) -> Result<String> {
    let bytes = sandbox
        .read_file(path)
        .ok_or_else(|| anyhow!("file not found: {path}"))?;

    let mut rdr = csv::Reader::from_reader(bytes.as_slice());
    let headers: Vec<String> = rdr
        .headers()
        .map_err(|e| anyhow!("CSV parse error: {e}"))?
        .iter()
        .map(|s| s.to_string())
        .collect();
    let n_cols = headers.len();

    // Collect all rows.
    let mut rows: Vec<Vec<String>> = Vec::new();
    for result in rdr.records() {
        let record = result.map_err(|e| anyhow!("CSV row error: {e}"))?;
        rows.push(record.iter().map(|f| f.to_string()).collect());
    }
    let n_rows = rows.len();

    // Per-column stats.
    let mut col_stats: Vec<Value> = Vec::with_capacity(n_cols);
    for col_idx in 0..n_cols {
        let values: Vec<&str> = rows
            .iter()
            .filter_map(|r| r.get(col_idx).map(|s| s.as_str()))
            .collect();
        let non_empty = values.iter().filter(|&&v| !v.is_empty()).count();

        // Try numeric.
        let nums: Vec<f64> = values
            .iter()
            .filter_map(|v| v.parse::<f64>().ok())
            .collect();

        let stat = if nums.len() > n_rows / 2 {
            // Mostly numeric column.
            let min = nums.iter().cloned().fold(f64::INFINITY, f64::min);
            let max = nums.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let sum: f64 = nums.iter().sum();
            let mean = sum / nums.len() as f64;
            json!({
                "name": &headers[col_idx],
                "type": "numeric",
                "non_empty": non_empty,
                "min": round2(min),
                "max": round2(max),
                "sum": round2(sum),
                "mean": round2(mean),
            })
        } else {
            // Categorical column — unique values (up to 20).
            let mut uniq: Vec<&str> = values.clone();
            uniq.sort_unstable();
            uniq.dedup();
            let n_uniq = uniq.len();
            let show: Vec<&str> = uniq.into_iter().take(20).collect();
            json!({
                "name": &headers[col_idx],
                "type": "categorical",
                "non_empty": non_empty,
                "unique_count": n_uniq,
                "unique_values": show,
            })
        };
        col_stats.push(stat);
    }

    // Sample rows as an array of objects.
    let sample: Vec<Value> = rows
        .iter()
        .take(sample_rows)
        .map(|row| {
            let mut obj = serde_json::Map::new();
            for (i, val) in row.iter().enumerate() {
                if let Some(col) = headers.get(i) {
                    obj.insert(col.clone(), json!(val));
                }
            }
            Value::Object(obj)
        })
        .collect();

    Ok(serde_json::to_string_pretty(&json!({
        "path": path,
        "row_count": n_rows,
        "col_count": n_cols,
        "columns": col_stats,
        "sample": sample,
    }))?)
}

fn round2(f: f64) -> f64 {
    (f * 100.0).round() / 100.0
}

// ── json_query ────────────────────────────────────────────────────────────────
//
// Supports JSON Pointer syntax (RFC 6901): /foo/bar/0
// and a simple dot-path shorthand: foo.bar.0  →  /foo/bar/0

pub fn json_query(sandbox: &Sandbox, path: &str, pointer: &str) -> Result<String> {
    let bytes = sandbox
        .read_file(path)
        .ok_or_else(|| anyhow!("file not found: {path}"))?;

    let root: Value =
        serde_json::from_slice(&bytes).map_err(|e| anyhow!("JSON parse error: {e}"))?;

    // Normalise dot-path to JSON Pointer.
    let ptr = if pointer.starts_with('/') {
        pointer.to_string()
    } else if pointer.is_empty() {
        // Empty pointer = return root summary.
        let summary = match &root {
            Value::Array(a) => json!({
                "type": "array",
                "length": a.len(),
                "first": a.first(),
            }),
            Value::Object(o) => json!({
                "type": "object",
                "keys": o.keys().collect::<Vec<_>>(),
            }),
            other => other.clone(),
        };
        return Ok(serde_json::to_string_pretty(&summary)?);
    } else {
        format!("/{}", pointer.replace('.', "/"))
    };

    let node = root
        .pointer(&ptr)
        .ok_or_else(|| anyhow!("pointer '{ptr}' not found in {path}"))?;

    // For large arrays, summarise rather than dump everything.
    let out = match node {
        Value::Array(a) if a.len() > 50 => json!({
            "type": "array",
            "length": a.len(),
            "first_5": &a[..5.min(a.len())],
            "last_5":  &a[a.len().saturating_sub(5)..],
        }),
        other => other.clone(),
    };

    Ok(serde_json::to_string_pretty(&out)?)
}
