FROM python:3.12-slim

# Install uv for fast, reproducible installs from PyPI
RUN pip install --no-cache-dir uv

# Install the published package (locked to current version)
RUN uv pip install --system --no-cache testrail-mcp-server==0.1.1

# Smithery / Cloud Run / Fly all set PORT in env; default to 8080 if unset.
ENV MCP_TRANSPORT=http \
    PORT=8080 \
    HOST=0.0.0.0

EXPOSE 8080

# All other config (TestRail/Jira/Confluence/Anthropic keys) is injected by the
# host platform — the user fills them in via Smithery's config UI.
ENTRYPOINT ["testrail-mcp-server"]
