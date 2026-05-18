# testrail-mcp

An MCP server for TestRail that goes beyond CRUD: it prepares everything an MCP client needs to generate real test cases from Jira tickets, Confluence pages, or any free-form spec — and pushes them back to TestRail.

Built on the official Python MCP SDK ([FastMCP](https://github.com/modelcontextprotocol/python-sdk)). Designed to plug into Claude Desktop, Claude Code, Cursor, or any other MCP-capable client.

**No `ANTHROPIC_API_KEY` required.** The server never calls an LLM. It fetches data (Jira/Confluence/TestRail), bundles in house-style anchors, and returns it with instructions for your MCP client. The LLM work happens in your client (Claude Desktop / Cursor / etc.) on your existing subscription — you pay once.

## What it gives you

**CRUD over TestRail**
- `list_projects` — projects visible to the user
- `list_suites` — suites under a project
- `search_test_cases` — list cases under a project / suite / section, optionally filter by title substring
- `get_test_case` — fetch one case by ID
- `create_test_case` — create a case in a section
- `get_or_create_section` — resolve a path like `Auth > Login > Edge Cases`, creating missing nodes

**Preparation tools — fetch + bundle data, hand off to your MCP client's LLM**
- `prepare_cases_from_text` — feed any spec/PRD/bug-report text → returns spec + house-style anchors + schema + instructions for the client to generate cases.
- `prepare_cases_from_jira` — pass a Jira issue key (e.g. `ABC-123`); server fetches summary, description, comments, subtasks, walks the ADF tree.
- `prepare_cases_from_confluence` — pass a Confluence page ID; HTML body stripped to plain text.
- `preview_house_style` — see the 5 sibling cases the server will use as in-context style anchors.
- `add_test_cases_bulk` — once the LLM has the cases array, push them all into TestRail in one call (with auto section-hierarchy creation).

All three `prepare_cases_*` tools pull a few existing cases from the target section as house-style examples by default, so generated cases match local title casing, step granularity, and expected-result phrasing. Override with `house_style_section_id` to draw style from a different "golden" section, or set `house_style=False` to skip.

## Quick start

### Run with `uvx` (recommended — no clone, no venv)

Once the package is on PyPI:

```bash
uvx testrail-mcp-server
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
      "args": ["testrail-mcp-server"],
      "env": {
        "TESTRAIL_BASE_URL": "https://your-org.testrail.io",
        "TESTRAIL_USER": "you@example.com",
        "TESTRAIL_API_KEY": "...",
        "TESTRAIL_PROJECT_ID": "1",
        "TESTRAIL_SUITE_ID": "1",
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
claude mcp add testrail -- uvx testrail-mcp-server
```

(You'll still need to provide env vars — either via `claude mcp add --env KEY=VALUE` flags or a `.env` in the working directory.)

### Use in Cursor

In `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "testrail": {
      "command": "uvx",
      "args": ["testrail-mcp-server"],
      "env": { "TESTRAIL_BASE_URL": "...", "TESTRAIL_USER": "...", "TESTRAIL_API_KEY": "..." }
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
| `JIRA_BASE_URL`         | optional | only for `prepare_cases_from_jira`                     |
| `JIRA_USER`             | optional | Jira account email                                     |
| `JIRA_API_TOKEN`        | optional | https://id.atlassian.com/manage-profile                |
| `CONFLUENCE_BASE_URL`   | optional | only for `prepare_cases_from_confluence`               |
| `CONFLUENCE_EMAIL`      | optional | defaults to `JIRA_USER`                                |
| `CONFLUENCE_API_TOKEN`  | optional | defaults to `JIRA_API_TOKEN`                           |

## Example: from a Jira ticket straight into TestRail

In Claude Code or Claude Desktop, after the server is registered:

> Generate test cases from `ABC-123` and put them under `Auth > Login > Smoke`.

The server walks the section path (creating missing nodes), pulls the Jira ticket, generates ~15-30 cases, then bulk-creates them with house-style defaults (template 2, type 7, priority 3). Reply contains every new case ID.

## Tool reference

In addition to the basics above:

**Test-case authoring & QA**
- `prepare_feature_bootstrap` — one-shot: ingest a Confluence page / Jira ticket / raw spec, plan the suite + section, fetch house-style anchors. Hand it all back to the client LLM with step-by-step instructions for generation + push.
- `dedupe_against_section` — flag generated cases that look like duplicates of cases already in a target section (title-token containment; configurable `threshold`).
- `prepare_lint` — return the case batch + lint instructions; the client LLM reviews and reports vague titles, generic expecteds, combined steps, etc.
- `prepare_coverage_gaps` — return the spec + case titles + instructions; the client LLM lists testable behaviours not covered.
- `list_sections`, `find_populated_section`, `create_suite`, `update_case` — CRUD helpers.

**Test-run management & reporting**
- `list_runs`, `get_run`, `get_tests_in_run`, `get_results_for_run` — read-side access.
- `create_run`, `update_run`, `close_run` — write-side. Pair with CI to auto-create a run per build.
- `add_result`, `add_bulk_results` — post results back from automation. Status accepts either a string (`"passed"`, `"failed"`, `"blocked"`, `"retest"`) or a TestRail status_id.
- `prepare_run_summary` — fetch run metadata + statuses + failure comments; client LLM writes the ship-ready Markdown report.
- `prepare_runs_diff(run_a, run_b)` — regression/fix delta between two runs; client LLM narrates the diff as Markdown.
- `flaky_test_detector(case_id, last_n_runs)` — pull a case's status across recent runs and flag flakiness when it flips between pass/fail. Pure data, no LLM needed.

## Roadmap

- [ ] SSE / HTTP transport for hosted use
- [ ] Embedding-based dedupe (semantic, not just token overlap)
- [ ] `update_case` — patch existing cases when the spec changes
- [ ] Suite cloning across projects

## License

MIT
