"""TestRail MCP server.

Exposes TestRail as MCP tools plus AI helpers that generate test cases from
free-form text, a Jira ticket, or a Confluence page — and optionally push them
into TestRail in one call.

Prompt, ADF parser, section-hierarchy logic and house-style defaults are
ported from the battle-tested qa_bot.py used in the Travel Sort QA workflow.

Run locally via stdio:
    python server.py

Or inspect interactively:
    npx @modelcontextprotocol/inspector python server.py
"""
from __future__ import annotations

import json
import os
import re
import time
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
TR_PROJECT_ID = int(os.getenv("TESTRAIL_PROJECT_ID", "0") or 0)
TR_SUITE_ID = int(os.getenv("TESTRAIL_SUITE_ID", "0") or 0)

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_USER = os.getenv("JIRA_USER", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")

CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/")
CONFLUENCE_EMAIL = os.getenv("CONFLUENCE_EMAIL", JIRA_USER)
CONFLUENCE_API_TOKEN = os.getenv("CONFLUENCE_API_TOKEN", JIRA_API_TOKEN)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEN_MODEL = os.getenv("CASE_GEN_MODEL", "claude-haiku-4-5-20251001")

# TestRail defaults — match qa_bot.py house style
TR_TEMPLATE_ID = int(os.getenv("TR_TEMPLATE_ID", "2"))   # "Test Case (Steps)"
TR_TYPE_ID = int(os.getenv("TR_TYPE_ID", "7"))           # adjust to your TR types
TR_PRIORITY_ID = int(os.getenv("TR_PRIORITY_ID", "3"))   # Medium

mcp = FastMCP("testrail-mcp")

# ──────────────────────────────────────────────────────────────────────
# TestRail thin client
# ──────────────────────────────────────────────────────────────────────


def _tr_auth_header() -> str:
    raw = f"{TESTRAIL_USER}:{TESTRAIL_API_KEY}".encode()
    return "Basic " + b64encode(raw).decode()


async def _tr_request(method: str, path: str, retries: int = 3, **kwargs) -> Any:
    if not (TESTRAIL_BASE_URL and TESTRAIL_USER and TESTRAIL_API_KEY):
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
        for attempt in range(retries):
            resp = await client.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 429:
                # TestRail rate limit — back off as instructed
                retry_after = int(resp.headers.get("Retry-After", "60"))
                time.sleep(min(retry_after, 90))
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else None
        raise RuntimeError(f"TestRail request failed after {retries} retries: {path}")


# ──────────────────────────────────────────────────────────────────────
# Jira & Confluence read-only clients
# ──────────────────────────────────────────────────────────────────────


async def _jira_get_issue(issue_key: str) -> dict:
    if not (JIRA_BASE_URL and JIRA_USER and JIRA_API_TOKEN):
        raise RuntimeError(
            "Jira not configured. Set JIRA_BASE_URL, JIRA_USER, JIRA_API_TOKEN in .env."
        )
    url = (
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
        "?fields=summary,description,issuetype,status,priority,"
        "fixVersions,labels,comment,subtasks,attachment"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, auth=(JIRA_USER, JIRA_API_TOKEN))
        resp.raise_for_status()
        return resp.json()


async def _confluence_get_page(page_id: str) -> tuple[str, str, str]:
    """Return (title, body_plain_text, version_label)."""
    if not (CONFLUENCE_BASE_URL and CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN):
        raise RuntimeError(
            "Confluence not configured. Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, "
            "CONFLUENCE_API_TOKEN in .env."
        )
    auth = (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await client.get(
            f"{CONFLUENCE_BASE_URL}/wiki/api/v2/pages/{page_id}?body-format=storage",
            auth=auth, headers=headers,
        )
        meta.raise_for_status()
        title = meta.json().get("title", "")
        full = await client.get(
            f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}"
            "?expand=body.export_view,version",
            auth=auth, headers=headers,
        )
        full.raise_for_status()
        data = full.json()
    body = data.get("body", {}).get("export_view", {}).get("value", "")
    version = data.get("version", {}).get("number", 0)
    clean = re.sub(r"<[^>]+>", " ", body)
    clean = re.sub(r"\s+", " ", clean).strip()
    return title, clean, f"v{version}"


# ──────────────────────────────────────────────────────────────────────
# Atlassian Document Format → plain text (ported from qa_bot.py)
# ──────────────────────────────────────────────────────────────────────


def _adf_to_text(node: Any, depth: int = 0) -> str:
    """Recursively convert ADF JSON to plain text.

    Handles paragraph, text, bulletList, orderedList, listItem, heading,
    codeBlock, blockquote, table, tableRow, tableCell, mention, inlineCard.
    """
    if not node:
        return ""

    node_type = node.get("type", "")
    content = node.get("content", [])
    text = node.get("text", "")

    if node_type == "text":
        return text
    if node_type == "mention":
        return node.get("attrs", {}).get("text", "@someone")
    if node_type == "emoji":
        return node.get("attrs", {}).get("text", "")
    if node_type in ("inlineCard", "blockCard"):
        return node.get("attrs", {}).get("url", "")
    if node_type == "hardBreak":
        return "\n"

    children_text = "".join(_adf_to_text(c, depth) for c in content)

    if node_type == "doc":
        return children_text
    if node_type == "paragraph":
        return children_text.strip() + "\n"
    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 1)
        return "#" * level + " " + children_text.strip() + "\n"
    if node_type == "bulletList":
        lines = []
        for item in content:
            item_text = _adf_to_text(item, depth + 1).strip()
            lines.append("  " * depth + "• " + item_text)
        return "\n".join(lines) + "\n"
    if node_type == "orderedList":
        lines = []
        for i, item in enumerate(content, 1):
            item_text = _adf_to_text(item, depth + 1).strip()
            lines.append("  " * depth + f"{i}. " + item_text)
        return "\n".join(lines) + "\n"
    if node_type == "listItem":
        return children_text
    if node_type == "codeBlock":
        lang = node.get("attrs", {}).get("language", "")
        return f"```{lang}\n{children_text.strip()}\n```\n"
    if node_type == "blockquote":
        return "> " + children_text.strip() + "\n"
    if node_type == "table":
        rows = []
        for row in content:
            cells = [_adf_to_text(cell, depth).strip() for cell in row.get("content", [])]
            rows.append(" | ".join(cells))
        return "\n".join(rows) + "\n"
    if node_type in ("tableRow", "tableCell", "tableHeader"):
        return children_text
    if node_type == "rule":
        return "---\n"
    if node_type == "mediaSingle":
        return ""
    return children_text


def _jira_to_context(issue: dict) -> tuple[str, str, str]:
    """Build (title, content_block, version_label) from a Jira issue payload."""
    fields = issue.get("fields", {})
    issue_key = issue.get("key", "")
    title = fields.get("summary", issue_key)
    issue_type = fields.get("issuetype", {}).get("name", "")
    status = fields.get("status", {}).get("name", "")
    priority = fields.get("priority", {}).get("name", "")
    fix_versions = ", ".join(v["name"] for v in fields.get("fixVersions", []))
    labels = ", ".join(fields.get("labels", []))
    description = _adf_to_text(fields.get("description") or {})

    comments_raw = fields.get("comment", {}).get("comments", [])
    comments = []
    for c in comments_raw[-5:]:
        author = c.get("author", {}).get("displayName", "?")
        body = _adf_to_text(c.get("body") or {})
        if body.strip():
            comments.append(f"[{author}]: {body.strip()}")

    subtasks = [f"- {s['fields']['summary']}" for s in fields.get("subtasks", [])]

    parts = [
        f"Issue: {issue_key} ({issue_type})",
        f"Status: {status} | Priority: {priority}",
    ]
    if fix_versions:
        parts.append(f"Fix Versions: {fix_versions}")
    if labels:
        parts.append(f"Labels: {labels}")
    parts.append("")
    parts.append("## Description")
    parts.append(description or "(no description)")
    if subtasks:
        parts.append("")
        parts.append("## Subtasks")
        parts.extend(subtasks)
    if comments:
        parts.append("")
        parts.append("## Comments")
        parts.extend(comments)

    return title, "\n".join(parts), (fix_versions or status)


# ──────────────────────────────────────────────────────────────────────
# Case-generation prompt — ported verbatim from qa_bot.py
# ──────────────────────────────────────────────────────────────────────

CASE_GEN_SYSTEM = """You are a QA engineer. Given a feature specification or Jira issue, generate test cases.
Output ONLY valid JSON array, no markdown, no explanation.

Each test case:
{
  "title": "Short descriptive title",
  "preconditions": "Setup needed before test (empty string if none)",
  "steps": [
    {"step": "Action to perform", "expected": "Expected result"}
  ]
}

Rules:
- Cover happy path, edge cases, negative cases
- Steps should be clear and atomic
- Expected results should be specific and verifiable
- Group logically: UI elements → core logic → edge cases → config
- 15-30 cases total depending on feature complexity
- Titles in English"""


def _generate_cases_via_claude(title: str, content: str, section: str) -> list[dict]:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env.")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    user_prompt = (
        f"Feature: {title}\n"
        f"Section in TestRail: {section}\n\n"
        f"Specification:\n{content[:12000]}\n\n"
        "Generate test cases for this feature."
    )
    msg = client.messages.create(
        model=GEN_MODEL,
        max_tokens=8000,
        system=CASE_GEN_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    return json.loads(raw)


def _case_to_testrail_payload(case: dict) -> dict:
    """Map AI-shape case to TestRail add_case payload.

    AI returns steps as `{step, expected}`; TestRail expects `{content, expected}`.
    """
    steps = case.get("steps", [])
    custom_steps = [
        {"content": s.get("step") or s.get("content", ""),
         "expected": s.get("expected", "")}
        for s in steps
    ]
    return {
        "title": (case.get("title") or "Untitled case")[:250],
        "template_id": TR_TEMPLATE_ID,
        "type_id": TR_TYPE_ID,
        "priority_id": TR_PRIORITY_ID,
        "custom_preconds": case.get("preconditions", ""),
        "custom_steps_separated": custom_steps,
    }


# ──────────────────────────────────────────────────────────────────────
# Section hierarchy helper — ports qa_bot.get_or_create_section
# ──────────────────────────────────────────────────────────────────────

_section_cache: dict[tuple[str, int | None], int] = {}


async def _load_sections(project_id: int, suite_id: int) -> None:
    resp = await _tr_request("GET", f"get_sections/{project_id}&suite_id={suite_id}")
    sections = resp.get("sections", resp) if isinstance(resp, dict) else resp
    for s in sections or []:
        _section_cache[(s["name"], s.get("parent_id"))] = s["id"]


async def _resolve_section(
    project_id: int, suite_id: int, hierarchy: str, create_missing: bool = True
) -> int:
    """Walk a 'Parent > Child > Grandchild' string and return the leaf section_id.
    Creates missing nodes when create_missing is True."""
    parts = [p.strip() for p in hierarchy.split(">") if p.strip()]
    if not parts:
        raise ValueError("Empty section hierarchy")

    if not _section_cache:
        await _load_sections(project_id, suite_id)

    parent_id: int | None = None
    for part in parts:
        key = (part, parent_id)
        if key not in _section_cache:
            if not create_missing:
                raise ValueError(f"Section not found: {' > '.join(parts)} (missing '{part}')")
            payload: dict[str, Any] = {"name": part, "suite_id": suite_id}
            if parent_id is not None:
                payload["parent_id"] = parent_id
            result = await _tr_request("POST", f"add_section/{project_id}", json=payload)
            _section_cache[key] = result["id"]
            time.sleep(0.4)
        parent_id = _section_cache[key]
    return parent_id  # type: ignore[return-value]


# ──────────────────────────────────────────────────────────────────────
# CRUD tools
# ──────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_projects() -> list[dict]:
    """List all TestRail projects visible to the configured user."""
    data = await _tr_request("GET", "get_projects")
    if isinstance(data, dict) and "projects" in data:
        return data["projects"]
    return data or []


@mcp.tool()
async def list_suites(project_id: int | None = None) -> list[dict]:
    """List suites under a TestRail project.

    `project_id` defaults to TESTRAIL_PROJECT_ID from env when omitted or 0.
    """
    pid = project_id or TR_PROJECT_ID
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    return await _tr_request("GET", f"get_suites/{pid}") or []


@mcp.tool()
async def search_test_cases(
    project_id: int | None = None,
    suite_id: int | None = None,
    section_id: int | None = None,
    title_contains: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List test cases in a project, optionally filtered by suite, section, or title substring.

    Both `project_id` and `suite_id` fall back to env defaults
    (TESTRAIL_PROJECT_ID / TESTRAIL_SUITE_ID) when omitted or 0.
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id if suite_id is not None and suite_id != 0 else (TR_SUITE_ID or None)
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    params: list[str] = []
    if sid is not None:
        params.append(f"&suite_id={sid}")
    if section_id is not None and section_id != 0:
        params.append(f"&section_id={section_id}")
    path = f"get_cases/{pid}" + "".join(params)
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

    `case` should look like:
        {
          "title": "...",
          "preconditions": "...",
          "steps": [{"step": "...", "expected": "..."}, ...]
        }
    """
    payload = _case_to_testrail_payload(case)
    return await _tr_request("POST", f"add_case/{section_id}", json=payload)


@mcp.tool()
async def get_or_create_section(
    hierarchy: str,
    project_id: int | None = None,
    suite_id: int | None = None,
) -> dict:
    """Resolve a section hierarchy like `1.5.0 > Tournament Race > Edge Cases`,
    creating missing parents as needed. Returns {"section_id": int, "path": str}.

    Defaults to TESTRAIL_PROJECT_ID / TESTRAIL_SUITE_ID from env when not provided.
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id or TR_SUITE_ID
    if not pid or not sid:
        raise ValueError(
            "project_id and suite_id are required (or set TESTRAIL_PROJECT_ID + TESTRAIL_SUITE_ID)."
        )
    section_id = await _resolve_section(pid, sid, hierarchy, create_missing=True)
    return {"section_id": section_id, "path": hierarchy}


# ──────────────────────────────────────────────────────────────────────
# AI tools — the actual differentiator
# ──────────────────────────────────────────────────────────────────────


@mcp.tool()
async def generate_cases_from_text(
    text: str,
    feature_title: str = "Untitled feature",
    section_hierarchy: str | None = None,
    section_id: int | None = None,
    project_id: int | None = None,
    suite_id: int | None = None,
) -> dict:
    """Generate TestRail test cases from a free-form spec/PRD/text.

    Targeting modes (pick one when you want them created):
      - `section_id` — push straight into an existing section ID
      - `section_hierarchy` — like `1.5.0 > Tournament Race`; missing nodes are created.
        Uses TESTRAIL_PROJECT_ID/SUITE_ID from env unless overridden.

    Without either, the cases are returned but not created in TestRail.

    Returns {"cases": [...], "created_ids": [...]}.
    """
    cases = _generate_cases_via_claude(
        title=feature_title,
        content=text,
        section=section_hierarchy or "ad-hoc",
    )

    target_section_id = section_id
    if section_hierarchy and target_section_id is None:
        pid = project_id or TR_PROJECT_ID
        sid = suite_id or TR_SUITE_ID
        if pid and sid:
            target_section_id = await _resolve_section(pid, sid, section_hierarchy, True)

    created_ids: list[int] = []
    if target_section_id is not None:
        for c in cases:
            res = await create_test_case(section_id=target_section_id, case=c)
            if isinstance(res, dict) and "id" in res:
                created_ids.append(res["id"])
            time.sleep(0.3)  # gentle on TestRail rate limit

    return {
        "cases": cases,
        "created_ids": created_ids,
        "section_id": target_section_id,
    }


@mcp.tool()
async def generate_cases_from_jira(
    issue_key: str,
    section_hierarchy: str | None = None,
    section_id: int | None = None,
    project_id: int | None = None,
    suite_id: int | None = None,
) -> dict:
    """Fetch a Jira ticket by key, generate test cases, and optionally push them to TestRail.

    Example: issue_key="SH-1950", section_hierarchy="1.5.0 > Tournament Race".
    """
    issue = await _jira_get_issue(issue_key)
    title, content, version_label = _jira_to_context(issue)
    result = await generate_cases_from_text(
        text=content,
        feature_title=title,
        section_hierarchy=section_hierarchy,
        section_id=section_id,
        project_id=project_id,
        suite_id=suite_id,
    )
    result["source"] = {"type": "jira", "key": issue_key, "version": version_label}
    return result


@mcp.tool()
async def generate_cases_from_confluence(
    page_id: str,
    section_hierarchy: str | None = None,
    section_id: int | None = None,
    project_id: int | None = None,
    suite_id: int | None = None,
) -> dict:
    """Fetch a Confluence page by ID, generate test cases, and optionally push them to TestRail.

    `page_id` is the numeric ID (last part of the page URL: `/wiki/spaces/X/pages/<page_id>`).
    """
    title, content, version_label = await _confluence_get_page(page_id)
    result = await generate_cases_from_text(
        text=content,
        feature_title=title,
        section_hierarchy=section_hierarchy,
        section_id=section_id,
        project_id=project_id,
        suite_id=suite_id,
    )
    result["source"] = {"type": "confluence", "page_id": page_id, "version": version_label}
    return result


if __name__ == "__main__":
    mcp.run()
