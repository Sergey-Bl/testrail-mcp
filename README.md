# testrail-mcp

An MCP server for TestRail that goes beyond CRUD: it generates real test cases from Jira tickets or any free-form spec, optionally pushing them straight into TestRail.

Built on the official Python MCP SDK ([FastMCP](https://github.com/modelcontextprotocol/python-sdk)). Designed to plug into Claude Desktop, Claude Code, Cursor, or any other MCP-capable client.

## What it gives you

**CRUD over TestRail**
- `list_projects` — projects visible to the user
- `search_test_cases` — list cases under a project / suite / section, optionally filter by title substring
- `get_test_case` — fetch one case by ID
- `create_test_case` — create a case in a section

**AI tools (the actual differentiator)**
- `generate_cases_from_text` — feed a PRD chunk / spec / bug report → get TestRail-shaped cases, optionally created in the given section
- `generate_cases_from_jira` — pass a Jira issue key, the server pulls the ticket and generates cases in one call

## Quick start

```bash
git clone https://github.com/<you>/testrail-mcp
cd testrail-mcp

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in TESTRAIL_*, JIRA_* (optional), ANTHROPIC_API_KEY
```

### Inspect interactively (recommended first step)

```bash
npx @modelcontextprotocol/inspector python server.py
```

This opens a local web UI where each tool can be called by hand. Use it to verify auth and tool wiring before you plug into a client.

### Use in Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "testrail": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/testrail-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop. The `testrail` server should appear in the tools menu.

### Use in Claude Code

```bash
claude mcp add testrail -- /absolute/path/to/.venv/bin/python /absolute/path/to/server.py
```

### Use in Cursor

In `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "testrail": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

## Configuration

| Env var               | Required | Purpose                                    |
| --------------------- | -------- | ------------------------------------------ |
| `TESTRAIL_BASE_URL`   | yes      | e.g. `https://your-org.testrail.io`        |
| `TESTRAIL_USER`       | yes      | TestRail account email                     |
| `TESTRAIL_API_KEY`    | yes      | from My Settings → API Keys                |
| `ANTHROPIC_API_KEY`   | yes      | required for the AI generation tools       |
| `JIRA_BASE_URL`       | optional | only for `generate_cases_from_jira`        |
| `JIRA_USER`           | optional | Jira account email                         |
| `JIRA_API_TOKEN`      | optional | https://id.atlassian.com/manage-profile    |
| `CASE_GEN_MODEL`      | optional | defaults to `claude-sonnet-4-6`            |

## Roadmap

- [ ] House-style prompt: pull a few sibling cases from the target section to steer style consistency
- [ ] `lint_section` — flag duplicates, vague expecteds, missing preconditions
- [ ] Confluence ingest (`generate_cases_from_confluence`)
- [ ] `suggest_missing_coverage` — gap analysis against an existing section
- [ ] SSE transport for hosted use

## License

MIT
