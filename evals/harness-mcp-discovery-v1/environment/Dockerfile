# Main (agent) container for harness-mcp-discovery-v1.
#
# The agent under test is an external harness (Harbor's built-in claude-code
# or codex agent), installed by Harbor at trial setup. This image provides
# what that harness needs to reach NOUS over MCP: the REAL harness bridge from
# packages/harness-bridge (Node 24, tsx, pinned lockfile), the stdio launcher
# registered in task.toml, and python3 for the launcher and the verifier.
#
# Build context is the repository root. Only the bridge package, the root
# pnpm manifests, the shared verifier helpers and this task's launcher enter
# the image. The mock backend, its fixtures and tests/truth.json never do.
#
# node:24-bookworm-slim resolved on 2026-10-07 to the index digest below.
FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20

USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pin the bridge's runtime. Harbor's codex install puts an nvm Node 22 first on
# the agent's PATH, so the launcher never resolves `node` from PATH.
RUN install -d -m 0755 /opt/nous-eval/bin \
    && install -m 0755 /usr/local/bin/node /opt/nous-eval/bin/node

# Bridge dependencies from the committed lockfile. Every workspace manifest is
# present so the frozen-lockfile check sees the whole workspace, but only
# @nous/harness-bridge is installed. The root .npmrc (node-linker=hoisted) is
# deliberately not copied: hoisting would install the entire monorepo tree.
WORKDIR /opt/nous-bridge
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json frontend/package.json
COPY tests/e2e/package.json tests/e2e/package.json
COPY terminal/package.json terminal/package.json
COPY packages/chat-runtime/package.json packages/chat-runtime/package.json
COPY packages/harness-bridge/package.json packages/harness-bridge/tsconfig.json packages/harness-bridge/
RUN COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=true \
    corepack pnpm install --frozen-lockfile --filter @nous/harness-bridge \
    && test -f packages/harness-bridge/node_modules/tsx/dist/loader.mjs
COPY packages/harness-bridge/src packages/harness-bridge/src

# Verifier helpers (run_verifier_main) outside the agent's /app workspace.
COPY evals/harbor_common /opt/nous-eval/lib/evals/harbor_common
RUN printf '' > /opt/nous-eval/lib/evals/__init__.py

# Stdio MCP launcher registered in task.toml. The final command is a
# build-time proof that the bridge entry point loads under the pinned runtime
# (it also warms the tsx transpile cache for the first MCP spawn).
COPY evals/harness-mcp-discovery-v1/environment/launcher/start-mcp.sh \
     evals/harness-mcp-discovery-v1/environment/launcher/launch.py \
     evals/harness-mcp-discovery-v1/environment/launcher/forwarder.py \
     /opt/nous-eval/
RUN chmod 0555 /opt/nous-eval/start-mcp.sh /opt/nous-eval/launch.py /opt/nous-eval/forwarder.py \
    && chmod -R a+rX /opt/nous-eval /opt/nous-bridge \
    && /opt/nous-eval/bin/node \
        --import /opt/nous-bridge/packages/harness-bridge/node_modules/tsx/dist/loader.mjs \
        /opt/nous-bridge/packages/harness-bridge/src/cli.ts --help > /dev/null

# Codex's hosted web_search tool would let a codex trial cite papers found on
# the web instead of the NOUS tools. Harbor 0.6.6's codex agent runs with
# CODEX_HOME=/tmp/codex-home and only appends (>>) its MCP entry to
# config.toml, so this pre-seeded top-level key stays in force. Claude Code's
# WebSearch/WebFetch are removed by the pre-registered
# `--ak disallowed_tools=WebSearch,WebFetch` (specs/.../harness.md).
RUN install -d -m 0777 /tmp/codex-home \
    && printf 'web_search = "disabled"\n' > /tmp/codex-home/config.toml \
    && chmod 0666 /tmp/codex-home/config.toml

RUN mkdir -p /app
WORKDIR /app

# Harbor supplies its own long-running command (sleep infinity).
ENTRYPOINT []
