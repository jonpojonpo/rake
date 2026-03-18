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

# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.11-slim

# Install the rake binary from the Rust build stage
COPY --from=rust-builder /build/target/release/rake /usr/local/bin/rake
RUN chmod +x /usr/local/bin/rake

# Install Python dependencies (including office document conversion)
COPY python/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy rake Python SDK and Lambda handlers
COPY python/rake_sdk /app/rake_sdk
COPY python/aws/lambdas /app/lambdas

# Local dev server (optional)
COPY docker/local_server.py /app/local_server.py

# Verify rake works
RUN rake --help > /dev/null 2>&1 && echo "rake binary OK"

ENV RAKE_BINARY=/usr/local/bin/rake
ENV PYTHONPATH=/app

EXPOSE 8000
CMD ["uvicorn", "local_server:app", "--host", "0.0.0.0", "--port", "8000"]
