use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use anyhow::{anyhow, Result};
use wasmtime::{Caller, Linker};
use wasmtime_wasi::preview1::WasiP1Ctx;

use super::{SandboxConfig, ToolSet};

pub struct StoreData {
    pub wasi: WasiP1Ctx,
    pub virtual_fs: Arc<HashMap<PathBuf, Vec<u8>>>,
    pub scratch: Arc<Mutex<HashMap<PathBuf, Vec<u8>>>>,
    pub config: Arc<SandboxConfig>,
    pub engine: Arc<wasmtime::Engine>,
    pub limits: wasmtime::StoreLimits,
}

/// Read a UTF-8 string from guest linear memory.
fn read_guest_str(caller: &mut Caller<'_, StoreData>, ptr: i32, len: i32) -> Result<String> {
    let mem = caller
        .get_export("memory")
        .and_then(|e| e.into_memory())
        .ok_or_else(|| anyhow!("no memory export"))?;
    let data = mem.data(&*caller);
    let slice = data
        .get(ptr as usize..(ptr + len) as usize)
        .ok_or_else(|| anyhow!("memory out of bounds (str)"))?;
    Ok(String::from_utf8(slice.to_vec())?)
}

/// Read raw bytes from guest linear memory.
fn read_guest_bytes(caller: &mut Caller<'_, StoreData>, ptr: i32, len: i32) -> Result<Vec<u8>> {
    let mem = caller
        .get_export("memory")
        .and_then(|e| e.into_memory())
        .ok_or_else(|| anyhow!("no memory export"))?;
    let data = mem.data(&*caller);
    Ok(data
        .get(ptr as usize..(ptr + len) as usize)
        .ok_or_else(|| anyhow!("memory out of bounds (bytes)"))?
        .to_vec())
}

/// Allocate space in the guest via `rake_alloc`, write bytes, return (ptr, len).
fn alloc_and_write(caller: &mut Caller<'_, StoreData>, bytes: &[u8]) -> Result<(i32, i32)> {
    if bytes.is_empty() {
        return Ok((0, 0));
    }
    let len = bytes.len() as i32;

    let alloc_fn = caller
        .get_export("rake_alloc")
        .and_then(|e| e.into_func())
        .ok_or_else(|| anyhow!("agent must export rake_alloc(i32)->i32"))?;
    let alloc_fn = alloc_fn.typed::<i32, i32>(&*caller)?;
    let ptr = alloc_fn.call(&mut *caller, len)?;

    // Re-fetch memory after potential growth from the alloc call.
    let mem = caller
        .get_export("memory")
        .and_then(|e| e.into_memory())
        .ok_or_else(|| anyhow!("no memory export after alloc"))?;
    mem.data_mut(&mut *caller)
        .get_mut(ptr as usize..(ptr as usize + bytes.len()))
        .ok_or_else(|| anyhow!("allocated region out of bounds"))?
        .copy_from_slice(bytes);

    Ok((ptr, len))
}

fn host_rake_read(
    mut caller: Caller<'_, StoreData>,
    path_ptr: i32,
    path_len: i32,
) -> Result<(i32, i32)> {
    if !caller.data().config.tools.contains(ToolSet::READ) {
        anyhow::bail!("read tool not enabled");
    }
    let path = read_guest_str(&mut caller, path_ptr, path_len)?;
    let key = PathBuf::from(&path);

    let data = {
        let vfs = caller.data().virtual_fs.clone();
        let scratch = caller.data().scratch.clone();
        if let Some(b) = vfs.get(&key) {
            b.clone()
        } else {
            scratch
                .lock()
                .unwrap()
                .get(&key)
                .cloned()
                .ok_or_else(|| anyhow!("file not found: {path}"))?
        }
    };

    alloc_and_write(&mut caller, &data)
}

fn host_rake_write(
    mut caller: Caller<'_, StoreData>,
    path_ptr: i32,
    path_len: i32,
    data_ptr: i32,
    data_len: i32,
) -> Result<()> {
    if !caller.data().config.tools.contains(ToolSet::WRITE) {
        anyhow::bail!("write tool not enabled");
    }
    let path = read_guest_str(&mut caller, path_ptr, path_len)?;
    let data = read_guest_bytes(&mut caller, data_ptr, data_len)?;
    caller
        .data()
        .scratch
        .lock()
        .unwrap()
        .insert(PathBuf::from(path), data);
    Ok(())
}

fn host_rake_grep(
    mut caller: Caller<'_, StoreData>,
    pattern_ptr: i32,
    pattern_len: i32,
    path_ptr: i32,
    path_len: i32,
) -> Result<(i32, i32)> {
    if !caller.data().config.tools.contains(ToolSet::GREP) {
        anyhow::bail!("grep tool not enabled");
    }
    let pattern = read_guest_str(&mut caller, pattern_ptr, pattern_len)?;
    let path_filter = read_guest_str(&mut caller, path_ptr, path_len)?;

    let vfs = caller.data().virtual_fs.clone();
    let matches = super::grep::grep_virtual_fs(&pattern, &path_filter, &vfs)?;
    let json = serde_json::to_vec(&matches)?;

    alloc_and_write(&mut caller, &json)
}

fn host_rake_exec(
    mut caller: Caller<'_, StoreData>,
    wasm_ptr: i32,
    wasm_len: i32,
) -> Result<(i32, i32)> {
    if !caller.data().config.tools.contains(ToolSet::EXEC) {
        anyhow::bail!("exec tool not enabled");
    }
    let wasm_bytes = read_guest_bytes(&mut caller, wasm_ptr, wasm_len)?;

    let engine = caller.data().engine.clone();
    let vfs = caller.data().virtual_fs.clone();
    let config = caller.data().config.clone();

    let output = super::run_wasm_with_engine(&engine, vfs, config, &wasm_bytes)?;

    alloc_and_write(&mut caller, &output.stdout)
}

pub fn add_to_linker(linker: &mut Linker<StoreData>) -> Result<()> {
    linker.func_wrap("env", "rake_read", host_rake_read)?;
    linker.func_wrap("env", "rake_write", host_rake_write)?;
    linker.func_wrap("env", "rake_grep", host_rake_grep)?;
    linker.func_wrap("env", "rake_exec", host_rake_exec)?;
    Ok(())
}
