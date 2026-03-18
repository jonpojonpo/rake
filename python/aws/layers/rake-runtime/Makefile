# Makefile for building the rake Lambda Layer
#
# SAM calls `make build-RakeLayer` when building the layer.
# The layer structure must be:
#   /opt/rake                  — the rake binary (executable)
#   /opt/python/rake_sdk/      — the Python SDK
#   /opt/python/<dependencies>

ARTIFACTS_DIR ?= $(PWD)/.aws-sam/build/RakeLayer
ARCH ?= aarch64-unknown-linux-musl  # arm64 Lambda

build-RakeLayer:
	mkdir -p "$(ARTIFACTS_DIR)/bin"
	mkdir -p "$(ARTIFACTS_DIR)/python"

	# ── Build rake binary for Lambda (musl = static, no glibc dependency) ──
	@echo "Building rake for Lambda ($(ARCH))..."
	cd ../../.. && \
		cargo build --release --target $(ARCH) 2>/dev/null || \
		cargo build --release
	cp ../../../target/$(ARCH)/release/rake "$(ARTIFACTS_DIR)/rake" 2>/dev/null || \
		cp ../../../target/release/rake "$(ARTIFACTS_DIR)/rake"
	chmod +x "$(ARTIFACTS_DIR)/rake"

	# ── Install Python SDK and office conversion dependencies ──
	pip install \
		python-docx \
		openpyxl \
		python-pptx \
		"pdfminer.six" \
		boto3 \
		--target "$(ARTIFACTS_DIR)/python" \
		--quiet

	# ── Copy rake_sdk ──
	cp -r ../../rake_sdk "$(ARTIFACTS_DIR)/python/"

	@echo "Layer built at $(ARTIFACTS_DIR)"
	@echo "  rake binary: $(ARTIFACTS_DIR)/rake"
	@du -sh "$(ARTIFACTS_DIR)"
