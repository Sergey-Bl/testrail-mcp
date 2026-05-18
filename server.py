"""TestRail MCP server.

Exposes TestRail API as MCP tools plus AI helpers that generate test cases
from free-form text or a Jira ticket.

Run locally via stdio:
    python server.py

Or inspect interactively:
    npx @modelcontextprotocol/inspector python server.py
"""
from __future__ import annotations

import json
import os
import re
from base64 import b64encode
from typing import Any

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# ──────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────

TESTRAIL_BASE_URL = os.getenv("TESTRAIL_BASE_URL", "").rstrip("/")
TESTRAIL_USER = os.getenv("TESTRAIL_USER", "")
TESTRAIL_API_KEY = os.getenv("TESTRAIL_API_KEY", "")

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_USER = os.getenv("JIRA_USER", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEN_MODEL = os.getenv("CASE_GEN_MODEL", "claude-sonnet-4-6")

mcp = FastMCP("testrail-mcp")

# ──────────────────────────────────────────────────────────────────────
# TestRail thin client
# ──────────────────────────────────────────────────────────────────────


def _tr_auth_header() -> str:
    raw = f"{TESTRAIL_USER}:{TESTRAIL_API_KEY}".encode()
    return "Basic " + b64encode(raw).decode()


async def _tr_request(method: str, path: str, **kwargs) -> Any:
    if not TESTRAIL_BASE_URL or not TESTRAIL_USER or not TESTRAIL_API_KEY:
        raise RuntimeError(
            "TestRail not configured. Set TESTRAIL_BASE_URL, TESTRAIL_USER, "
            "TESTRAIL_API_KEY in .env."
        )
    url = f"{TESTRAIL_BASE_URL}/index.php?/api/v2/{path}"
    headers = {
        "Authorization": _tr_auth_header(),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None


# ──────────────────────────────────────────────────────────────────────
# Jira client (read-only, used by generate_cases_from_jira)
# ──────────────────────────────────────────────────────────────────────


async def _jira_get_issue(issue_key: str) -> dict:
    if not (JIRA_BASE_URL and JIRA_USER and JIRA_API_TOKEN):
        raise RuntimeError(
            "Jira not configured. Set JIRA_BASE_URL, JIRA_USER, JIRA_API_TOKEN in .env."
        )
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    auth = (JIRA_USER, JIRA_API_TOKEN)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, auth=auth)
        resp.raise_for_status()
        return resp.json()


def _adf_to_text(adf: Any) -> str:
    """Flatten Jira Atlassian Document Format into plain text."""
    if not isinstance(adf, dict):
        return ""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t == "text":
                out.append(node.get("text", ""))
            elif t in {"paragraph", "heading", "listItem"}:
                for child in node.get("content", []):
                    walk(child)
                out.append("\n")
            else:
                for child in node.get("content", []):
                    walk(child)
        elif isinstance(node, list):
            for n in node:
                walk(n)

    walk(adf)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


# ──────────────────────────────────────────────────────────────────────
# Case-generation prompt
# ──────────────────────────────────────────────────────────────────────

CASE_GEN_SYSTEM = """\
You are a senior QA engineer. From the input (a Jira ticket, PRD excerpt,
or free-form spec) produce TestRail-compatible test cases.

Return STRICT JSON, an array of cases. Each case object must have:
{
  "title": "Action-oriented title under 90 chars",
  "preconditions": "What must be true before running the case (or empty string).",
  "steps": [
    {"content": "Step 1 action", "expected": "Step 1 expected result"},
    ...
  ]
}

Rules:
- Cover happy path, negative cases, and 1-2 edge cases.
- Steps are atomic — one user action per step.
- 'expected' is observable, not 'should work'.
- No markdown, no explanations, output JSON only.
"""


def _generate_cases_via_claude(context: str) -> list[dict]:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env.")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=GEN_MODEL,
        max_tokens=4096,
        system=CASE_GEN_SYSTEM,
        messages=[{"role": "user", "content": context}],
    )
    raw = msg.content[0].text.strip()
    # Strip ```json fences if present
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    return json.loads(raw)


def _cases_to_testrail_payload(case: dict) -> dict:
    """Map AI-generated case into TestRail add_case payload."""
    steps = case.get("steps", [])
    custom_steps_separated = [
        {"content": s.get("content", ""), "expected": s.get("expected", "")}
        for s in steps
    ]
    return {
        "title": case.get("title", "Untitled case")[:250],
        "custom_preconds": case.get("preconditions", ""),
        "custom_steps_separated": custom_steps_separated,
        "template_id": 2,  # "Test Case (Steps)" — adjust if your TR uses different
        "type_id": 1,
    }


# ──────────────────────────────────────────────────────────────────────
# CRUD tools
# ──────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_projects() -> list[dict]:
    """List all TestRail projects visible to the configured user."""
    data = await _tr_request("GET", "get_projects")
    # TestRail returns dict with "projects" key on newer versions, list on older
    if isinstance(data, dict) and "projects" in data:
        return data["projects"]
    return data or []


@mcp.tool()
async def search_test_cases(
    project_id: int,
    suite_id: int | None = None,
    section_id: int | None = None,
    title_contains: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search test cases in a project. Optionally filter by suite, section, or title substring."""
    params: list[str] = []
    if suite_id is not None:
        params.append(f"&suite_id={suite_id}")
    if section_id is not None:
        params.append(f"&section_id={section_id}")
    path = f"get_cases/{project_id}" + "".join(params)
    data = await _tr_request("GET", path)
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if title_contains:
        needle = title_contains.lower()
        cases = [c for c in cases if needle in (c.get("title") or "").lower()]
    return cases[:limit]


@mcp.tool()
async def get_test_case(case_id: int) -> dict:
    """Fetch a single test case by its TestRail ID."""
    return await _tr_request("GET", f"get_case/{case_id}")


@mcp.tool()
async def create_test_case(section_id: int, case: dict) -> dict:
    """Create a single test case in a TestRail section.

    `case` should follow the {title, preconditions, steps[{content,expected}]} shape.
    """
    payload = _cases_to_testrail_payload(case)
    return await _tr_request("POST", f"add_case/{section_id}", json=payload)


# ──────────────────────────────────────────────────────────────────────
# AI tools — the actual differentiator
# ──────────────────────────────────────────────────────────────────────


@mcp.tool()
async def generate_cases_from_text(
    text: str,
    section_id: int | None = None,
) -> dict:
    """Generate test cases from a free-form spec/PRD/text.

    If section_id is provided, the generated cases are also created in TestRail.
    Returns {"cases": [...], "created_ids": [...] or []}.
    """
    cases = _generate_cases_via_claude(text)
    created_ids: list[int] = []
    if section_id is not None:
        for c in cases:
            res = await create_test_case(section_id=section_id, case=c)
            if isinstance(res, dict) and "id" in res:
                created_ids.append(res["id"])
    return {"cases": cases, "created_ids": created_ids}


@mcp.tool()
async def generate_cases_from_jira(
    issue_key: str,
    section_id: int | None = None,
) -> dict:
    """Fetch a Jira ticket by key and generate TestRail test cases from it.

    If section_id is given, also creates the cases in TestRail.
    """
    issue = await _jira_get_issue(issue_key)
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    description = _adf_to_text(fields.get("description"))
    context = f"# {issue_key}: {summary}\n\n{description}"
    return await generate_cases_from_text(text=context, section_id=section_id)


if __name__ == "__main__":
    mcp.run()
