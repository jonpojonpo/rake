# ── Stage 1: Build the rake binary ─────────────────────────────────────────
FROM rust:1.82-slim AS rust-builder

WORKDIR /build

# Cache dependency fetch separately from source compilation
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src && echo 'fn main(){}' > src/main.rs && \
    cargo build --release && \
    rm -rf src

# Build the real binary
COPY src ./src
RUN touch src/main.rs && cargo build --release

# ── Stage 2: Python FastAPI server ──────────────────────────────────────────
FROM python:3.11-slim

# Install the rake binary from the Rust build stage
COPY --from=rust-builder /build/target/release/rake /usr/local/bin/rake
RUN chmod +x /usr/local/bin/rake

WORKDIR /app

# Install Python dependencies
COPY python/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy rake Python SDK and server
COPY python/rake_sdk rake_sdk/
COPY python/server   server/

ENV RAKE_BINARY=/usr/local/bin/rake
ENV RAKE_LLM=claude
ENV RAKE_TIMEOUT=300

EXPOSE 8000
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
