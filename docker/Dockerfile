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

# ── Stage 2: Azure Functions Python runtime ─────────────────────────────────
FROM mcr.microsoft.com/azure-functions/python:4-python3.11

# Install the rake binary from the Rust build stage
COPY --from=rust-builder /build/target/release/rake /usr/local/bin/rake
RUN chmod +x /usr/local/bin/rake

# Install Python dependencies
COPY python/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy rake Python SDK and services
COPY python/rake_sdk /home/site/wwwroot/rake_sdk
COPY python/services  /home/site/wwwroot/services
COPY python/host.json /home/site/wwwroot/host.json

# Verify rake works
RUN rake --help > /dev/null 2>&1 && echo "rake OK"

# Azure Functions expects FUNCTIONS_WORKER_RUNTIME
ENV FUNCTIONS_WORKER_RUNTIME=python
ENV RAKE_BINARY=/usr/local/bin/rake

EXPOSE 80
