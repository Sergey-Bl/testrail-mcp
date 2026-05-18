# testrail-mcp

An MCP server for TestRail that goes beyond CRUD: it generates real test cases from Jira tickets or any free-form spec, optionally pushing them straight into TestRail.

Built on the official Python MCP SDK ([FastMCP](https://github.com/modelcontextprotocol/python-sdk)). Designed to plug into Claude Desktop, Claude Code, Cursor, or any other MCP-capable client.

## What it gives you

**CRUD over TestRail**
- `list_projects` — projects visible to the user
- `list_suites` — suites under a project
- `search_test_cases` — list cases under a project / suite / section, optionally filter by title substring
- `get_test_case` — fetch one case by ID
- `create_test_case` — create a case in a section
- `get_or_create_section` — resolve a path like `1.5.0 > Tournament Race > Edge Cases`, creating missing nodes

**AI tools (the actual differentiator)**
- `generate_cases_from_text` — feed a PRD chunk / spec / bug report → get TestRail-shaped cases, optionally created in the given section (by ID or by hierarchy string)
- `generate_cases_from_jira` — pass a Jira issue key (e.g. `SH-1950`); server fetches summary, description, comments, subtasks, walks the ADF tree, generates cases
- `generate_cases_from_confluence` — pass a Confluence page ID; same flow, HTML body stripped to plain text
- `preview_house_style` — see the 5 sibling cases that will be passed to Claude as in-context style anchors

All three `generate_cases_*` tools pull a few existing cases from the target section and feed them to Claude as house-style examples by default, so new cases match local title casing, step granularity, and expected-result phrasing. Override with `house_style_section_id` to draw style from a different "golden" section, or set `house_style=False` to skip.

## Quick start

### Run with `uvx` (recommended — no clone, no venv)

Once the package is on PyPI:

```bash
uvx testrail-mcp
```

For local development from a checkout:

```bash
git clone https://github.com/Sergey-Bl/testrail-mcp
cd testrail-mcp
uv venv --python 3.12
uv pip install -e .
cp .env.example .env   # fill in TestRail / Jira / Anthropic keys
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
      "command": "uvx",
      "args": ["testrail-mcp"],
      "env": {
        "TESTRAIL_BASE_URL": "https://your-org.testrail.io",
        "TESTRAIL_USER": "you@example.com",
        "TESTRAIL_API_KEY": "...",
        "TESTRAIL_PROJECT_ID": "1",
        "TESTRAIL_SUITE_ID": "1",
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "JIRA_BASE_URL": "https://your-org.atlassian.net",
        "JIRA_USER": "you@example.com",
        "JIRA_API_TOKEN": "..."
      }
    }
  }
}
```

Restart Claude Desktop. The `testrail` server should appear in the tools menu.

### Use in Claude Code

```bash
claude mcp add testrail -- uvx testrail-mcp
```

(You'll still need to provide env vars — either via `claude mcp add --env KEY=VALUE` flags or a `.env` in the working directory.)

### Use in Cursor

In `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "testrail": {
      "command": "uvx",
      "args": ["testrail-mcp"],
      "env": { "TESTRAIL_BASE_URL": "...", "TESTRAIL_USER": "...", "TESTRAIL_API_KEY": "...", "ANTHROPIC_API_KEY": "..." }
    }
  }
}
```

## Configuration

| Env var                 | Required | Purpose                                                |
| ----------------------- | -------- | ------------------------------------------------------ |
| `TESTRAIL_BASE_URL`     | yes      | e.g. `https://your-org.testrail.io`                    |
| `TESTRAIL_USER`         | yes      | TestRail account email                                 |
| `TESTRAIL_API_KEY`      | yes      | from My Settings → API Keys                            |
| `TESTRAIL_PROJECT_ID`   | optional | default project ID for tools that take it              |
| `TESTRAIL_SUITE_ID`     | optional | default suite ID                                       |
| `TR_TEMPLATE_ID`        | optional | default template (2 = "Test Case (Steps)")             |
| `TR_TYPE_ID`            | optional | default case type                                      |
| `TR_PRIORITY_ID`        | optional | default priority (3 = Medium)                          |
| `ANTHROPIC_API_KEY`     | yes      | required for AI generation tools                       |
| `CASE_GEN_MODEL`        | optional | defaults to `claude-haiku-4-5-20251001` (cheap + fast) |
| `JIRA_BASE_URL`         | optional | only for `generate_cases_from_jira`                    |
| `JIRA_USER`             | optional | Jira account email                                     |
| `JIRA_API_TOKEN`        | optional | https://id.atlassian.com/manage-profile                |
| `CONFLUENCE_BASE_URL`   | optional | only for `generate_cases_from_confluence`              |
| `CONFLUENCE_EMAIL`      | optional | defaults to `JIRA_USER`                                |
| `CONFLUENCE_API_TOKEN`  | optional | defaults to `JIRA_API_TOKEN`                           |

## Example: from a Jira ticket straight into TestRail

In Claude Code or Claude Desktop, after the server is registered:

> Generate test cases from `SH-1950` and put them under `1.5.0 > Tournament Race > Smoke`.

The server walks the section path (creating missing nodes), pulls the Jira ticket, generates ~15-30 cases, then bulk-creates them with house-style defaults (template 2, type 7, priority 3). Reply contains every new case ID.

## Roadmap

- [ ] House-style prompt: pull a few sibling cases from the target section to steer style consistency
- [ ] `lint_section` — flag duplicates, vague expecteds, missing preconditions
- [ ] Confluence ingest (`generate_cases_from_confluence`)
- [ ] `suggest_missing_coverage` — gap analysis against an existing section
- [ ] SSE transport for hosted use

## License

MIT
