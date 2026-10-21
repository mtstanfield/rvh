# rvh worker image: one image for every target. The target repository
# is bind-mounted read-only at /workspace at run time, so nothing here is
# per-repo. The same image runs the run's LLM gateway (/opt/rvh/llm_proxy.py).
FROM ubuntu:24.04

ARG GOOSE_VERSION=1.52.0
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq python3 python3-pip ripgrep \
 && rm -rf /var/lib/apt/lists/*

# goose (the agent): pinned, checksum-verified.
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      arm64) goose_asset=goose-aarch64-unknown-linux-gnu.tar.gz; \
             goose_sha=ae602c4f6e9a785bf087da52c89908d4dc6aa605dcc17bf83293873f626d9c85 ;; \
      amd64) goose_asset=goose-x86_64-unknown-linux-gnu.tar.gz; \
             goose_sha=4aee1f770b405c44194c0e9407df1fb06bda4c50eee935f0d8fd10731821cc5e ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/goose.tgz "https://github.com/aaif-goose/goose/releases/download/v${GOOSE_VERSION}/${goose_asset}"; \
    echo "${goose_sha}  /tmp/goose.tgz" | sha256sum -c -; \
    tar -xzf /tmp/goose.tgz -C /usr/local/bin ./goose; \
    chmod 0755 /usr/local/bin/goose; \
    rm -f /tmp/goose.tgz

# The CPG MCP server's dependencies. mcp<2: FastMCP was renamed in 2.x, and
# goose silently drops an MCP extension that fails to import.
RUN python3 -m pip install --no-cache-dir --break-system-packages 'mcp==1.30.0' 'requests==2.33.0'

COPY cpg_client.py cpg_formatters.py cpg_mcp_server.py cpg_queries.py llm_proxy.py /opt/rvh/

# Default non-root user (the gateway runs as this). Workers run as the
# invoking host user instead, so files they write to /output are owned by it.
RUN useradd --create-home --shell /bin/bash rvh
USER rvh
WORKDIR /workspace
