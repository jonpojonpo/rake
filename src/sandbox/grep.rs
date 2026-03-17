use std::collections::HashMap;
use std::path::PathBuf;

use anyhow::Result;
use grep_regex::RegexMatcher;
use grep_searcher::sinks::UTF8;
use grep_searcher::Searcher;
use serde_json::{json, Value};

pub fn grep_virtual_fs(
    pattern: &str,
    path_filter: &str,
    virtual_fs: &HashMap<PathBuf, Vec<u8>>,
) -> Result<Vec<Value>> {
    let matcher = RegexMatcher::new(pattern)?;
    let mut results: Vec<Value> = Vec::new();

    for (file_path, content) in virtual_fs {
        let path_str = file_path.to_string_lossy();
        if !path_filter.is_empty() && !path_str.contains(path_filter) {
            continue;
        }

        let path_owned = path_str.to_string();
        Searcher::new().search_slice(
            &matcher,
            content.as_slice(),
            UTF8(|line_num, line| {
                results.push(json!({
                    "path": path_owned,
                    "line_number": line_num,
                    "line": line.trim_end_matches('\n'),
                }));
                Ok(true)
            }),
        )?;
    }

    Ok(results)
}
