use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use anyhow::Result;
use bitflags::bitflags;
use serde::{Deserialize, Serialize};
use wasmtime::{Config, Engine, Linker, Module, Store, StoreLimitsBuilder};
use wasmtime_wasi::pipe::MemoryOutputPipe;

pub mod grep;
pub mod host_imports;
pub mod wasi;

use host_imports::StoreData;

bitflags! {
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct ToolSet: u32 {
        const READ  = 0b0001;
        const WRITE = 0b0010;
        const GREP  = 0b0100;
        const EXEC  = 0b1000;
    }
}

#[derive(Clone, Debug)]
pub struct SandboxConfig {
    pub max_memory_bytes: u64,
    pub tools: ToolSet,
}

impl Default for SandboxConfig {
    fn default() -> Self {
        Self {
            max_memory_bytes: 40 * 1024 * 1024,
            tools: ToolSet::READ | ToolSet::GREP,
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SandboxOutput {
    pub stdout: Vec<u8>,
    pub result: serde_json::Value,
}

pub struct Sandbox {
    engine: Arc<Engine>,
    virtual_fs: HashMap<PathBuf, Vec<u8>>,
    config: SandboxConfig,
    // Persistent scratch space for the LLM agent loop (separate from per-run WASM scratch).
    scratch_cache: Arc<Mutex<HashMap<PathBuf, Vec<u8>>>>,
}

impl Sandbox {
    pub fn new(config: SandboxConfig) -> Result<Self> {
        let mut wt_config = Config::new();
        wt_config.cranelift_opt_level(wasmtime::OptLevel::Speed);
        let engine = Arc::new(Engine::new(&wt_config)?);
        Ok(Self {
            engine,
            virtual_fs: HashMap::new(),
            config,
            scratch_cache: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    pub fn mount(&mut self, path: impl Into<PathBuf>, bytes: Vec<u8>) {
        self.virtual_fs.insert(path.into(), bytes);
    }

    pub fn list_files(&self) -> Vec<String> {
        let mut files: Vec<String> = self
            .virtual_fs
            .keys()
            .map(|p| p.to_string_lossy().to_string())
            .collect();
        files.sort();
        files
    }

    pub fn read_file(&self, path: &str) -> Option<Vec<u8>> {
        self.virtual_fs.get(&PathBuf::from(path)).cloned()
    }

    pub fn grep(&self, pattern: &str, filter: &str) -> Result<Vec<serde_json::Value>> {
        grep::grep_virtual_fs(pattern, filter, &self.virtual_fs)
    }

    pub fn write_scratch(&self, path: &str, bytes: Vec<u8>) {
        // No-op if run() hasn't been called yet (scratch is per-run).
        // For the LLM agent loop we maintain a persistent scratch map here.
        self.scratch_cache
            .lock()
            .unwrap()
            .insert(PathBuf::from(path), bytes);
    }

    pub fn read_scratch(&self, path: &str) -> Option<Vec<u8>> {
        self.scratch_cache.lock().unwrap().get(&PathBuf::from(path)).cloned()
    }

    /// Load a skill's `SKILL.md` file from `/skills/<name>/SKILL.md`.
    ///
    /// Returns the full Markdown content so the agent can read the skill's
    /// instructions and follow them.  Returns `None` if the skill is not
    /// mounted (caller should surface a helpful error).
    pub fn read_skill(&self, name: &str) -> Option<String> {
        let path = PathBuf::from(format!("/skills/{name}/SKILL.md"));
        self.virtual_fs
            .get(&path)
            .and_then(|b| String::from_utf8(b.clone()).ok())
    }

    pub fn list_scratch(&self) -> Vec<String> {
        let mut files: Vec<String> = self
            .scratch_cache
            .lock()
            .unwrap()
            .keys()
            .map(|p| p.to_string_lossy().to_string())
            .collect();
        files.sort();
        files
    }

    pub fn run(&mut self, wasm_bytes: &[u8]) -> Result<SandboxOutput> {
        run_wasm_with_engine(
            &self.engine,
            Arc::new(self.virtual_fs.clone()),
            Arc::new(self.config.clone()),
            wasm_bytes,
        )
    }
}

pub(crate) fn run_wasm_with_engine(
    engine: &Arc<Engine>,
    virtual_fs: Arc<HashMap<PathBuf, Vec<u8>>>,
    config: Arc<SandboxConfig>,
    wasm_bytes: &[u8],
) -> Result<SandboxOutput> {
    let module = Module::new(engine, wasm_bytes)?;

    let stdout_pipe = MemoryOutputPipe::new(4 * 1024 * 1024);
    let wasi_ctx = wasi::build_wasi_ctx(stdout_pipe.clone());

    let limits = StoreLimitsBuilder::new()
        .memory_size(config.max_memory_bytes as usize)
        .build();

    let store_data = StoreData {
        wasi: wasi_ctx,
        virtual_fs: virtual_fs.clone(),
        scratch: Arc::new(Mutex::new(HashMap::new())),
        config: config.clone(),
        engine: engine.clone(),
        limits,
    };

    let mut store = Store::new(engine, store_data);
    store.limiter(|data| &mut data.limits);

    let mut linker: Linker<StoreData> = Linker::new(engine);
    wasi::add_to_linker(&mut linker)?;
    host_imports::add_to_linker(&mut linker)?;

    let instance = linker.instantiate(&mut store, &module)?;

    if let Some(start) = instance.get_func(&mut store, "_start") {
        let typed = start.typed::<(), ()>(&store)?;
        match typed.call(&mut store, ()) {
            Ok(()) => {}
            Err(e) => {
                if let Some(exit) = e.downcast_ref::<wasmtime_wasi::I32Exit>() {
                    if exit.0 != 0 {
                        anyhow::bail!("agent exited with code {}", exit.0);
                    }
                } else {
                    return Err(e.into());
                }
            }
        }
    }

    let stdout = stdout_pipe.contents().to_vec();

    Ok(SandboxOutput {
        stdout,
        result: serde_json::Value::Null,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_mount_and_read() {
        let mut sb = Sandbox::new(SandboxConfig::default()).unwrap();
        sb.mount("hello.txt", b"hello world".to_vec());
        assert_eq!(
            sb.virtual_fs[&PathBuf::from("hello.txt")],
            b"hello world"
        );
    }

    #[test]
    fn test_grep_returns_matches() {
        let mut vfs = HashMap::new();
        vfs.insert(
            PathBuf::from("main.rs"),
            b"fn main() {\n    println!(\"hello\");\n}\n".to_vec(),
        );
        let matches = grep::grep_virtual_fs("println", "", &vfs).unwrap();
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0]["path"], "main.rs");
        assert_eq!(matches[0]["line_number"], 2);
    }

    #[test]
    fn test_grep_path_filter() {
        let mut vfs = HashMap::new();
        vfs.insert(PathBuf::from("src/a.rs"), b"needle\n".to_vec());
        vfs.insert(PathBuf::from("src/b.txt"), b"needle\n".to_vec());
        let matches = grep::grep_virtual_fs("needle", ".rs", &vfs).unwrap();
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0]["path"], "src/a.rs");
    }
}
