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

// ── read_section ──────────────────────────────────────────────────────────────
//
// Reads a contiguous block of lines from a file (1-indexed, inclusive).
// This is the primary navigation tool for large documents.
// The index file (_index.md) provides start/end line numbers for each section.

pub fn read_section(sandbox: &Sandbox, path: &str, start: usize, end: usize) -> Result<String> {
    if start == 0 {
        return Err(anyhow!("start_line is 1-indexed; use 1 for the first line"));
    }
    let bytes = sandbox
        .read_file(path)
        .ok_or_else(|| anyhow!("file not found: {path}"))?;
    let text = String::from_utf8_lossy(&bytes);
    let total = text.lines().count();

    let lo = start.saturating_sub(1); // convert to 0-indexed
    let hi = end.min(total);          // clamp to file length

    if lo >= total {
        return Err(anyhow!(
            "start_line {start} exceeds file length ({total} lines)"
        ));
    }

    let lines: Vec<&str> = text.lines().skip(lo).take(hi.saturating_sub(lo)).collect();
    let mut out = lines.join("\n");

    let remaining = total.saturating_sub(hi);
    if remaining > 0 {
        out.push_str(&format!(
            "\n\n[showing lines {start}–{end} of {total}; {} more lines below]",
            remaining
        ));
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
        "xlsx" | "xls"           => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx" | "doc"           => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx" | "ppt"           => "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdf"                    => "application/pdf",
        "zip"                    => "application/zip",
        "txt"                    => "text/plain",
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
            // Determine if this is free-form text or a low-cardinality categorical.
            let total_chars: usize = values.iter().map(|v| v.len()).sum();
            let avg_len = if non_empty > 0 { total_chars / non_empty } else { 0 };

            if avg_len > 50 {
                // Free-form text column — show length stats and longest samples.
                let mut by_len: Vec<&str> = values
                    .iter()
                    .filter(|&&v| !v.is_empty())
                    .cloned()
                    .collect();
                by_len.sort_by_key(|v| std::cmp::Reverse(v.len()));
                let samples: Vec<String> = by_len
                    .iter()
                    .take(3)
                    .map(|s| {
                        if s.len() > 120 {
                            format!("{}…", &s[..120])
                        } else {
                            s.to_string()
                        }
                    })
                    .collect();
                json!({
                    "name": &headers[col_idx],
                    "type": "text",
                    "non_empty": non_empty,
                    "avg_len": avg_len,
                    "max_len": by_len.first().map(|s| s.len()).unwrap_or(0),
                    "longest_samples": samples,
                })
            } else {
                // Categorical column — unique values (up to 20, truncated to 60 chars each).
                let mut uniq: Vec<&str> = values.clone();
                uniq.sort_unstable();
                uniq.dedup();
                let n_uniq = uniq.len();
                let show: Vec<String> = uniq
                    .into_iter()
                    .take(20)
                    .map(|v| if v.len() > 60 { format!("{}…", &v[..60]) } else { v.to_string() })
                    .collect();
                json!({
                    "name": &headers[col_idx],
                    "type": "categorical",
                    "non_empty": non_empty,
                    "unique_count": n_uniq,
                    "unique_values": show,
                })
            }
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

// ── csv_rows ──────────────────────────────────────────────────────────────────
//
// Returns a slice of rows as JSON objects, streaming past skipped rows so only
// the requested window is held in memory. Use csv_stats first to learn the
// total row count, then iterate with csv_rows for sliding-window analysis of
// large text-heavy CSVs.

pub fn csv_rows(sandbox: &Sandbox, path: &str, start_row: usize, end_row: usize) -> Result<String> {
    if end_row <= start_row {
        return Err(anyhow!("end_row ({end_row}) must be greater than start_row ({start_row})"));
    }

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

    let mut rows: Vec<Value> = Vec::new();
    let mut row_idx = 0usize;

    for result in rdr.records() {
        if row_idx >= end_row {
            break;
        }
        let record = result.map_err(|e| anyhow!("CSV row error: {e}"))?;
        if row_idx >= start_row {
            let mut obj = serde_json::Map::new();
            for (i, val) in record.iter().enumerate() {
                if let Some(col) = headers.get(i) {
                    obj.insert(col.clone(), json!(val));
                }
            }
            rows.push(Value::Object(obj));
        }
        row_idx += 1;
    }

    Ok(serde_json::to_string_pretty(&json!({
        "path": path,
        "start_row": start_row,
        "end_row": start_row + rows.len(),
        "returned": rows.len(),
        "rows": rows,
    }))?)
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
