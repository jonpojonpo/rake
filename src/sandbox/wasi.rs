use anyhow::Result;
use wasmtime::Linker;
use wasmtime_wasi::pipe::MemoryOutputPipe;
use wasmtime_wasi::preview1::WasiP1Ctx;
use wasmtime_wasi::WasiCtxBuilder;

use super::host_imports::StoreData;

pub fn build_wasi_ctx(stdout: MemoryOutputPipe) -> WasiP1Ctx {
    WasiCtxBuilder::new()
        .stdout(stdout)
        .inherit_stderr()
        .build_p1()
}

pub fn add_to_linker(linker: &mut Linker<StoreData>) -> Result<()> {
    wasmtime_wasi::preview1::add_to_linker_sync(linker, |s: &mut StoreData| &mut s.wasi)?;
    Ok(())
}
