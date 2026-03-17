;; Minimal agent that:
;;   1. Writes "hello from agent\n" to stdout via WASI fd_write
;;   2. Exports rake_alloc / rake_free (required for rake_* host calls)
;;
;; Compile with:  wat2wasm hello_agent.wat -o hello_agent.wasm
(module
  (import "wasi_snapshot_preview1" "fd_write"
    (func $fd_write (param i32 i32 i32 i32) (result i32)))

  (memory (export "memory") 1)

  ;; String "hello from agent\n" at offset 64 (17 bytes)
  (data (i32.const 64) "hello from agent\n")

  ;; iovec at offset 0: [ptr=64, len=17]
  ;; nwritten at offset 16

  (func (export "_start")
    ;; Set up iovec at address 0
    i32.const 0
    i32.const 64     ;; ptr to string
    i32.store
    i32.const 4
    i32.const 17     ;; length
    i32.store

    ;; fd_write(fd=1, iovs=0, iovs_len=1, nwritten=16)
    i32.const 1
    i32.const 0
    i32.const 1
    i32.const 16
    call $fd_write
    drop
  )

  ;; Bump allocator: returns fixed offset 0x10000 for simplicity.
  ;; Real agents should maintain a bump pointer in a global.
  (func (export "rake_alloc") (param $size i32) (result i32)
    i32.const 65536
  )

  (func (export "rake_free") (param $ptr i32) (param $len i32))
)
