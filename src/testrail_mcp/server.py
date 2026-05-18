"""TestRail MCP server.

Exposes TestRail as MCP tools plus AI helpers that generate test cases from
free-form text, a Jira ticket, or a Confluence page — and optionally push them
into TestRail in one call.

Prompt, ADF parser, section-hierarchy logic and house-style defaults are
ported from a battle-tested Slack-bot version of the same prompt and helpers.

Run locally via stdio:
    python server.py

Or inspect interactively:
    npx @modelcontextprotocol/inspector python server.py
"""
from __future__ import annotations

import asyncio
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

# TestRail defaults — match the original Slack-bot prototype house style
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
# Atlassian Document Format → plain text (ported from the original Slack-bot prototype)
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
# Case-generation prompt — ported verbatim from the original Slack-bot prototype
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
- Titles in English

If "## HOUSE STYLE EXAMPLES" appears in the user message, the cases there
were authored by humans in this exact TestRail project. Match their tone,
naming, level of detail, step granularity, expected-result phrasing, and
how preconditions are written. The goal is that a reader cannot tell which
cases are new vs which already existed."""


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]+|#\d+);")
_WHITESPACE_RE = re.compile(r"[ \t]*\n[ \t]*")


def _clean_richtext(s: str) -> str:
    """TestRail stores case bodies as HTML-flavoured markup. Strip tags for clean
    style examples; collapse whitespace; decode the most common entities."""
    if not s:
        return ""
    txt = _HTML_TAG_RE.sub("", s)
    txt = (txt.replace("&nbsp;", " ")
              .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&quot;", '"').replace("&#39;", "'"))
    txt = _HTML_ENTITY_RE.sub("", txt)
    txt = _WHITESPACE_RE.sub("\n", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def _simplify_case(case: dict) -> dict:
    """Reduce a raw TestRail case to {title, preconditions, steps[{step, expected}]} —
    the same shape our AI emits, so it's directly usable as a style example.
    Strips TestRail's HTML markup so Claude sees clean prose."""
    steps_raw = case.get("custom_steps_separated") or []
    steps = [
        {
            "step": _clean_richtext(s.get("content", "")),
            "expected": _clean_richtext(s.get("expected", "")),
        }
        for s in steps_raw
    ]
    if not steps and case.get("custom_steps"):
        steps = [{"step": _clean_richtext(case["custom_steps"]), "expected": ""}]
    return {
        "title": case.get("title", "").strip(),
        "preconditions": _clean_richtext(case.get("custom_preconds") or ""),
        "steps": steps,
    }


def _format_house_style_block(examples: list[dict]) -> str:
    if not examples:
        return ""
    parts = ["## HOUSE STYLE EXAMPLES",
             "Below are existing cases in the target section. New cases must match this style."]
    for i, ex in enumerate(examples, 1):
        parts.append("")
        parts.append(f"### Example {i}")
        parts.append(f"Title: {ex['title']}")
        if ex.get("preconditions"):
            parts.append(f"Preconditions: {ex['preconditions']}")
        parts.append("Steps:")
        for j, s in enumerate(ex.get("steps", []), 1):
            parts.append(f"  {j}. {s.get('step','')}")
            if s.get("expected"):
                parts.append(f"     Expected: {s['expected']}")
    parts.append("")
    return "\n".join(parts) + "\n"


def _generate_cases_via_claude(
    title: str,
    content: str,
    section: str,
    style_examples: list[dict] | None = None,
) -> list[dict]:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env.")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    style_block = _format_house_style_block(style_examples or [])
    user_prompt = (
        f"Feature: {title}\n"
        f"Section in TestRail: {section}\n\n"
        f"{style_block}"
        f"## SPECIFICATION\n{content[:12000]}\n\n"
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


def _normalize_title(t: str) -> str:
    """Lowercase, strip non-alphanumeric for fuzzy title matching."""
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


_TITLE_STOPWORDS = {"the", "a", "an", "of", "for", "in", "on", "and", "or", "to"}


def _title_overlap(a: str, b: str) -> float:
    """Token-containment ratio: shared tokens / smaller side. Returns 1.0 when one
    title is a token-subset of the other — i.e. the case is plausibly the same."""
    sa = set(_normalize_title(a).split()) - _TITLE_STOPWORDS
    sb = set(_normalize_title(b).split()) - _TITLE_STOPWORDS
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


async def _existing_titles_in_section(project_id: int, suite_id: int, section_id: int) -> list[dict]:
    """All cases currently in a section, simplified to {id, title}."""
    path = f"get_cases/{project_id}&suite_id={suite_id}&section_id={section_id}&limit=250"
    data = await _tr_request("GET", path)
    return [{"id": c["id"], "title": c.get("title", "")} for c in _unwrap(data, "cases")]


async def _claude_json(system: str, user: str, max_tokens: int = 4000) -> Any:
    """Minimal helper for one-shot Claude calls that should return JSON."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=GEN_MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    return json.loads(raw)


async def _fetch_house_style_examples(
    project_id: int,
    suite_id: int,
    section_id: int,
    n: int = 5,
) -> list[dict]:
    """Pull up to N existing cases from the target section so AI matches their style."""
    path = f"get_cases/{project_id}&suite_id={suite_id}&section_id={section_id}&limit={n*4}"
    data = await _tr_request("GET", path)
    raw_cases = _unwrap(data, "cases")
    # Prefer cases that actually have steps + preconditions — they're more useful as templates
    scored = sorted(
        raw_cases,
        key=lambda c: (
            bool(c.get("custom_steps_separated")),
            bool(c.get("custom_preconds")),
            -len(c.get("title") or ""),
        ),
        reverse=True,
    )
    return [_simplify_case(c) for c in scored[:n] if c.get("title")]


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
    for s in _unwrap(resp, "sections"):
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
    return _unwrap(data, "projects")


def _unwrap(data: Any, key: str) -> list:
    """Newer TestRail wraps lists as {'offset':..., key: [...]}; older returns the list directly."""
    if isinstance(data, dict) and key in data:
        return data[key] or []
    return data or []


@mcp.tool()
async def create_suite(
    name: str,
    description: str | None = None,
    project_id: int | None = None,
) -> dict:
    """Create a new TestRail suite in a project (only works on multi-suite projects).

    `project_id` defaults to TESTRAIL_PROJECT_ID from env.
    """
    pid = project_id or TR_PROJECT_ID
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    payload: dict[str, Any] = {"name": name}
    if description:
        payload["description"] = description
    return await _tr_request("POST", f"add_suite/{pid}", json=payload)


@mcp.tool()
async def list_suites(project_id: int | None = None) -> list[dict]:
    """List suites under a TestRail project.

    `project_id` defaults to TESTRAIL_PROJECT_ID from env when omitted or 0.
    """
    pid = project_id or TR_PROJECT_ID
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    data = await _tr_request("GET", f"get_suites/{pid}")
    return _unwrap(data, "suites")


@mcp.tool()
async def list_sections(
    project_id: int | None = None,
    suite_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """List sections in a suite, returning {id, name, parent_id}.

    Defaults to TESTRAIL_PROJECT_ID / TESTRAIL_SUITE_ID from env when omitted.
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id or TR_SUITE_ID
    if not (pid and sid):
        raise ValueError("project_id and suite_id required (or set in .env).")
    data = await _tr_request("GET", f"get_sections/{pid}&suite_id={sid}")
    sections = _unwrap(data, "sections")
    return [
        {"id": s["id"], "name": s["name"], "parent_id": s.get("parent_id")}
        for s in sections[:limit]
    ]


@mcp.tool()
async def find_populated_section(
    project_id: int | None = None,
    suite_id: int | None = None,
    min_cases: int = 3,
    scan_limit: int = 40,
) -> dict | None:
    """Find a section that has at least `min_cases` real cases — useful as a
    house-style anchor when bootstrapping a new feature. Returns the first
    section meeting the threshold (sections are scanned in TestRail order).
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id or TR_SUITE_ID
    if not (pid and sid):
        raise ValueError("project_id and suite_id required (or set in .env).")
    sections = await list_sections(project_id=pid, suite_id=sid, limit=scan_limit)
    for s in sections:
        cases = await search_test_cases(
            project_id=pid, suite_id=sid, section_id=s["id"], limit=min_cases,
        )
        if len(cases) >= min_cases:
            return {**s, "case_count_seen": len(cases)}
    return None


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
    cases = _unwrap(data, "cases")
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
    """Resolve a section hierarchy like `Auth > Login > Edge Cases`,
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
async def preview_house_style(
    section_id: int | None = None,
    section_hierarchy: str | None = None,
    project_id: int | None = None,
    suite_id: int | None = None,
    n: int = 5,
) -> dict:
    """Return up to N existing cases from the target section in style-example format.

    Use this to preview what Claude will see as "house style" before generating.
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id or TR_SUITE_ID
    if not (pid and sid):
        raise ValueError("project_id and suite_id required (or set in .env).")
    target_section_id = section_id
    if section_hierarchy and not target_section_id:
        target_section_id = await _resolve_section(pid, sid, section_hierarchy, False)
    if not target_section_id:
        raise ValueError("section_id or section_hierarchy required.")
    examples = await _fetch_house_style_examples(pid, sid, target_section_id, n=n)
    return {"section_id": target_section_id, "examples": examples}


@mcp.tool()
async def generate_cases_from_text(
    text: str,
    feature_title: str = "Untitled feature",
    section_hierarchy: str | None = None,
    section_id: int | None = None,
    project_id: int | None = None,
    suite_id: int | None = None,
    house_style: bool = True,
    house_style_section_id: int | None = None,
    house_style_examples: int = 5,
) -> dict:
    """Generate TestRail test cases from a free-form spec/PRD/text.

    Targeting modes (pick one when you want them created):
      - `section_id` — push straight into an existing section ID
      - `section_hierarchy` — like `Auth > Login`; missing nodes are created.
        Uses TESTRAIL_PROJECT_ID/SUITE_ID from env unless overridden.

    House-style matching:
      - If `house_style` is True (default) and a target section is known,
        the server fetches a few existing cases from that section and passes
        them to Claude as style examples, so new cases match local conventions.
      - Override the style source with `house_style_section_id` to pull examples
        from a different "golden" section while writing into another.

    Without a target section, cases are returned but not created in TestRail.

    Returns {"cases", "created_ids", "section_id", "style_section_id", "style_examples_used"}.
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id or TR_SUITE_ID

    target_section_id = section_id
    if section_hierarchy and target_section_id is None and pid and sid:
        target_section_id = await _resolve_section(pid, sid, section_hierarchy, True)

    style_examples: list[dict] = []
    style_section_id = house_style_section_id or target_section_id
    if house_style and style_section_id and pid and sid and house_style_examples > 0:
        try:
            style_examples = await _fetch_house_style_examples(
                pid, sid, style_section_id, n=house_style_examples
            )
        except Exception:
            # Style examples are a nice-to-have; never fail the run because of them.
            style_examples = []

    cases = _generate_cases_via_claude(
        title=feature_title,
        content=text,
        section=section_hierarchy or "ad-hoc",
        style_examples=style_examples,
    )

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
        "style_section_id": style_section_id if style_examples else None,
        "style_examples_used": len(style_examples),
    }


@mcp.tool()
async def generate_cases_from_jira(
    issue_key: str,
    section_hierarchy: str | None = None,
    section_id: int | None = None,
    project_id: int | None = None,
    suite_id: int | None = None,
    house_style: bool = True,
    house_style_section_id: int | None = None,
    house_style_examples: int = 5,
) -> dict:
    """Fetch a Jira ticket by key, generate test cases, and optionally push them to TestRail.

    Example: issue_key="ABC-123", section_hierarchy="Auth > Login".
    Set `house_style=False` to skip pulling sibling cases as style anchors.
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
        house_style=house_style,
        house_style_section_id=house_style_section_id,
        house_style_examples=house_style_examples,
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
    house_style: bool = True,
    house_style_section_id: int | None = None,
    house_style_examples: int = 5,
) -> dict:
    """Fetch a Confluence page by ID, generate test cases, and optionally push them to TestRail.

    `page_id` is the numeric ID (last part of the page URL: `/wiki/spaces/X/pages/<page_id>`).
    Set `house_style=False` to skip pulling sibling cases as style anchors.
    """
    title, content, version_label = await _confluence_get_page(page_id)
    result = await generate_cases_from_text(
        text=content,
        feature_title=title,
        section_hierarchy=section_hierarchy,
        section_id=section_id,
        project_id=project_id,
        suite_id=suite_id,
        house_style=house_style,
        house_style_section_id=house_style_section_id,
        house_style_examples=house_style_examples,
    )
    result["source"] = {"type": "confluence", "page_id": page_id, "version": version_label}
    return result


@mcp.tool()
async def dedupe_against_section(
    cases: list[dict],
    section_id: int,
    project_id: int | None = None,
    suite_id: int | None = None,
    threshold: float = 0.65,
) -> dict:
    """Check generated cases against what's already in a TestRail section.

    Uses title-token overlap (no embeddings) — fast, deterministic. Returns:
      - `kept`: list of {case, reason} that look new
      - `duplicates`: list of {case, existing_id, existing_title, overlap}
    `threshold` is the minimum token-overlap (0..1) to flag as duplicate.
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id or TR_SUITE_ID
    if not (pid and sid):
        raise ValueError("project_id and suite_id required (or set in .env).")
    existing = await _existing_titles_in_section(pid, sid, section_id)

    kept: list[dict] = []
    duplicates: list[dict] = []
    for c in cases:
        title = c.get("title", "")
        best = None
        best_score = 0.0
        for e in existing:
            score = _title_overlap(title, e["title"])
            if score > best_score:
                best_score = score
                best = e
        if best and best_score >= threshold:
            duplicates.append({
                "case": c,
                "existing_id": best["id"],
                "existing_title": best["title"],
                "overlap": round(best_score, 2),
            })
        else:
            kept.append({"case": c, "closest_existing": best, "max_overlap": round(best_score, 2)})

    return {
        "kept": kept,
        "duplicates": duplicates,
        "kept_count": len(kept),
        "duplicates_count": len(duplicates),
        "section_id": section_id,
        "section_total_existing": len(existing),
    }


LINT_SYSTEM = """You are a senior QA lead reviewing test cases for a TestRail project.
Output ONLY valid JSON, no markdown.

For each case index, flag concrete quality issues. Return:
[
  {"index": 0, "warnings": ["vague title", "expected too generic on step 2"]},
  ...
]

Only include indexes that have warnings. Skip well-written cases.
Be terse — each warning under 12 words, actionable.

Common issues to flag:
- vague or generic titles ("Test feature works")
- missing or empty preconditions where the test obviously needs setup
- 'expected' results that say "should work" / "no errors" instead of observable state
- steps that combine multiple actions into one
- redundant cases that duplicate another in the same batch
- missing negative or edge cases for an obvious risk in the feature"""


@mcp.tool()
async def lint_cases(cases: list[dict], feature_title: str = "") -> dict:
    """Run a QA-quality lint over generated cases via Claude.

    Returns {"warnings": [{"index", "title", "warnings": [...]}], "warning_count"}.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env.")
    if not cases:
        return {"warnings": [], "warning_count": 0}
    payload = []
    for i, c in enumerate(cases):
        payload.append({
            "index": i,
            "title": c.get("title", ""),
            "preconditions": c.get("preconditions", ""),
            "steps": c.get("steps", []),
        })
    user = (
        f"Feature: {feature_title or '(unspecified)'}\n\n"
        f"Cases to review (JSON):\n{json.dumps(payload, ensure_ascii=False)}"
    )
    raw_warnings = await _claude_json(LINT_SYSTEM, user, max_tokens=4000)
    warnings = []
    for w in raw_warnings:
        idx = w.get("index")
        if idx is None or idx >= len(cases):
            continue
        warnings.append({
            "index": idx,
            "title": cases[idx].get("title", ""),
            "warnings": w.get("warnings", []),
        })
    return {"warnings": warnings, "warning_count": len(warnings)}


COVERAGE_SYSTEM = """You are a QA architect. Given a feature spec and a list of test
case titles generated from it, identify what testable behaviour from the spec is
NOT covered by any case.

Output ONLY valid JSON:
{
  "gaps": ["short description of an uncovered behaviour", ...],
  "weak_areas": ["aspect that has only shallow coverage", ...]
}

Rules:
- 'gaps' must be concrete behaviours mentioned (or strongly implied) by the spec.
- Do not invent requirements that are not in the spec.
- Each gap under 20 words, written as a testable behaviour.
- If the spec is fully covered, return {"gaps": [], "weak_areas": []}.
- Maximum 10 gaps + 5 weak_areas."""


@mcp.tool()
async def coverage_gaps(
    spec_text: str,
    cases: list[dict],
    feature_title: str = "",
) -> dict:
    """Use Claude to find behaviours in the spec that are not covered by the case set."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env.")
    titles = [c.get("title", "") for c in cases]
    user = (
        f"Feature: {feature_title or '(unspecified)'}\n\n"
        f"## SPEC\n{spec_text[:12000]}\n\n"
        f"## EXISTING CASE TITLES ({len(titles)})\n"
        + "\n".join(f"- {t}" for t in titles)
    )
    result = await _claude_json(COVERAGE_SYSTEM, user, max_tokens=2000)
    return {
        "gaps": result.get("gaps", []),
        "weak_areas": result.get("weak_areas", []),
        "gaps_count": len(result.get("gaps", [])),
    }


# ──────────────────────────────────────────────────────────────────────
# Test-run reporting
# ──────────────────────────────────────────────────────────────────────

# TestRail's default status_id meanings:
TR_STATUS = {
    1: "passed",
    2: "blocked",
    3: "untested",
    4: "retest",
    5: "failed",
}


@mcp.tool()
async def list_runs(
    project_id: int | None = None,
    suite_id: int | None = None,
    is_completed: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """List test runs in a project.

    Filters:
      - `suite_id` — only runs of this suite
      - `is_completed` — True for closed/archived runs only, False for open only
    """
    pid = project_id or TR_PROJECT_ID
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    params = []
    if suite_id is not None and suite_id != 0:
        params.append(f"&suite_id={suite_id}")
    if is_completed is not None:
        params.append(f"&is_completed={1 if is_completed else 0}")
    params.append(f"&limit={min(limit, 250)}")
    data = await _tr_request("GET", f"get_runs/{pid}" + "".join(params))
    runs = _unwrap(data, "runs")
    # Trim heavy fields users rarely need
    return [{
        "id": r["id"],
        "name": r.get("name"),
        "suite_id": r.get("suite_id"),
        "is_completed": r.get("is_completed", False),
        "created_on": r.get("created_on"),
        "completed_on": r.get("completed_on"),
        "passed_count": r.get("passed_count", 0),
        "failed_count": r.get("failed_count", 0),
        "blocked_count": r.get("blocked_count", 0),
        "retest_count": r.get("retest_count", 0),
        "untested_count": r.get("untested_count", 0),
        "url": r.get("url"),
    } for r in runs[:limit]]


@mcp.tool()
async def get_run(run_id: int) -> dict:
    """Fetch the full metadata of a single test run."""
    return await _tr_request("GET", f"get_run/{run_id}")


@mcp.tool()
async def get_tests_in_run(run_id: int, limit: int = 250) -> list[dict]:
    """Tests in a run with their current status (current = latest result)."""
    data = await _tr_request("GET", f"get_tests/{run_id}&limit={min(limit, 250)}")
    tests = _unwrap(data, "tests")
    return [{
        "id": t["id"],
        "case_id": t.get("case_id"),
        "title": t.get("title"),
        "status_id": t.get("status_id"),
        "status": TR_STATUS.get(t.get("status_id"), "unknown"),
        "assignedto_id": t.get("assignedto_id"),
        "priority_id": t.get("priority_id"),
    } for t in tests[:limit]]


@mcp.tool()
async def get_results_for_run(run_id: int, limit: int = 250) -> list[dict]:
    """All result entries for a run — includes comments, defects, who tested when."""
    data = await _tr_request("GET", f"get_results_for_run/{run_id}&limit={min(limit, 250)}")
    results = _unwrap(data, "results")
    return [{
        "id": r["id"],
        "test_id": r.get("test_id"),
        "status_id": r.get("status_id"),
        "status": TR_STATUS.get(r.get("status_id"), "unknown"),
        "comment": _clean_richtext(r.get("comment", "") or ""),
        "defects": r.get("defects"),
        "created_on": r.get("created_on"),
        "created_by": r.get("created_by"),
        "elapsed": r.get("elapsed"),
    } for r in results[:limit]]


SUMMARIZE_RUN_SYSTEM = """You are a senior QA lead writing a release-readiness
status report from a TestRail test run. Output GitHub-flavoured Markdown.

Structure (use these section headings exactly):

## Summary
One short paragraph (3-4 sentences) for non-QA stakeholders: what was tested,
pass rate, ship-readiness verdict.

## Stats
A compact table of counts: Passed / Failed / Blocked / Retest / Untested,
plus pass rate percentage of executed tests.

## Top failures
Up to 8 most concerning failures. Group by area or root cause when obvious.
For each, give:
- The test title (verbatim)
- One-line hypothesis of what's broken, drawn from the tester comment

## Risk areas
2-5 short bullets calling out: clusters of failures in a feature, low coverage
visible in the breakdown, blocked tests that hide real signal, anything a
release manager should know.

## Recommendation
One of: **READY**, **NEEDS ATTENTION**, **BLOCKED** — followed by one short
paragraph explaining why and what unblocks the next decision.

Rules:
- Be concrete. Cite test names. No filler.
- Do not invent counts or test names — use only what the input contains.
- If untested ratio > 30%, call it out as a "coverage gap" risk.
- If the same test appears repeatedly across runs (you won't see this — only
  current run is provided), flag it as "potentially flaky — verify across runs"."""


@mcp.tool()
async def summarize_run(
    run_id: int,
    include_passed_titles: bool = False,
) -> dict:
    """Generate a human-readable Markdown report for a test run.

    Pulls run metadata, current per-test statuses, and per-failure comments,
    then asks Claude to synthesise a release-readiness report.

    `include_passed_titles=False` (default) keeps the prompt compact — Claude
    sees only failure/blocked/retest details plus aggregate pass counts. Set
    True only for tiny runs (<50 tests) when you want full granularity.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env.")

    run = await get_run(run_id)
    tests = await get_tests_in_run(run_id, limit=500)
    results = await get_results_for_run(run_id, limit=500)

    # Index latest result per test
    latest_by_test: dict[int, dict] = {}
    for r in results:
        tid = r.get("test_id")
        if not tid:
            continue
        if tid not in latest_by_test:
            latest_by_test[tid] = r

    # Bucket tests by status
    buckets: dict[str, list[dict]] = {k: [] for k in TR_STATUS.values()}
    for t in tests:
        bucket = TR_STATUS.get(t["status_id"], "unknown")
        latest = latest_by_test.get(t["id"], {})
        buckets.setdefault(bucket, []).append({
            "title": t["title"],
            "comment": (latest.get("comment") or "")[:600],
        })

    counts = {k: len(v) for k, v in buckets.items()}
    executed = counts.get("passed", 0) + counts.get("failed", 0) + counts.get("blocked", 0) + counts.get("retest", 0)
    pass_rate = (counts.get("passed", 0) / executed * 100) if executed else 0.0

    # Build compact input
    payload = {
        "run": {
            "id": run.get("id"),
            "name": run.get("name"),
            "description": (run.get("description") or "")[:400],
            "is_completed": run.get("is_completed", False),
            "created_on": run.get("created_on"),
            "completed_on": run.get("completed_on"),
        },
        "counts": counts,
        "executed": executed,
        "pass_rate_pct": round(pass_rate, 1),
        "failed": buckets.get("failed", [])[:50],
        "blocked": buckets.get("blocked", [])[:25],
        "retest": buckets.get("retest", [])[:25],
        "untested_count": counts.get("untested", 0),
    }
    if include_passed_titles:
        payload["passed_titles"] = [t["title"] for t in buckets.get("passed", [])[:200]]

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=GEN_MODEL,
        max_tokens=4000,
        system=SUMMARIZE_RUN_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    markdown = msg.content[0].text.strip()

    return {
        "run_id": run_id,
        "run_name": run.get("name"),
        "counts": counts,
        "pass_rate_pct": payload["pass_rate_pct"],
        "report_markdown": markdown,
    }


@mcp.tool()
async def create_run(
    name: str,
    suite_id: int | None = None,
    description: str = "",
    include_all: bool = True,
    case_ids: list[int] | None = None,
    milestone_id: int | None = None,
    project_id: int | None = None,
    refs: str | None = None,
) -> dict:
    """Create a new test run.

    By default includes every case in the suite (`include_all=True`). Pass an
    explicit `case_ids` list and set `include_all=False` to scope the run.
    """
    pid = project_id or TR_PROJECT_ID
    sid = suite_id or TR_SUITE_ID
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    payload: dict[str, Any] = {"name": name, "include_all": include_all}
    if sid:
        payload["suite_id"] = sid
    if description:
        payload["description"] = description
    if not include_all and case_ids:
        payload["case_ids"] = case_ids
        payload["include_all"] = False
    if milestone_id:
        payload["milestone_id"] = milestone_id
    if refs:
        payload["refs"] = refs
    return await _tr_request("POST", f"add_run/{pid}", json=payload)


@mcp.tool()
async def update_run(
    run_id: int,
    name: str | None = None,
    description: str | None = None,
    milestone_id: int | None = None,
    refs: str | None = None,
) -> dict:
    """Patch an existing test run's metadata. Pass only fields you want to change."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if milestone_id is not None:
        payload["milestone_id"] = milestone_id
    if refs is not None:
        payload["refs"] = refs
    if not payload:
        raise ValueError("Pass at least one field to update.")
    return await _tr_request("POST", f"update_run/{run_id}", json=payload)


@mcp.tool()
async def close_run(run_id: int) -> dict:
    """Mark a test run as completed/archived. This is irreversible in TestRail."""
    return await _tr_request("POST", f"close_run/{run_id}", json={})


# Inverse of TR_STATUS for accepting strings in add_result tools
_STATUS_TO_ID = {v: k for k, v in TR_STATUS.items()}


def _resolve_status(status: int | str) -> int:
    if isinstance(status, int):
        return status
    s = (status or "").strip().lower()
    if s in _STATUS_TO_ID:
        return _STATUS_TO_ID[s]
    try:
        return int(s)
    except ValueError:
        raise ValueError(
            f"Unknown status {status!r}. Use one of "
            f"{sorted(_STATUS_TO_ID)} or a numeric status_id."
        )


@mcp.tool()
async def add_result(
    run_id: int,
    case_id: int,
    status: int | str,
    comment: str = "",
    defects: str | None = None,
    elapsed: str | None = None,
    version: str | None = None,
) -> dict:
    """Post a single test result by case_id (TestRail looks up the test inside the run).

    `status` accepts a string ("passed" | "failed" | "blocked" | "retest" | "untested")
    or a raw TestRail status_id (1..5 by default).
    `elapsed` is a TestRail time string ("30s", "1m 30s", "1h 5m").
    `defects` is a comma-separated list of bug-tracker IDs ("BUG-1, BUG-2").
    """
    payload: dict[str, Any] = {"status_id": _resolve_status(status)}
    if comment:
        payload["comment"] = comment
    if defects:
        payload["defects"] = defects
    if elapsed:
        payload["elapsed"] = elapsed
    if version:
        payload["version"] = version
    return await _tr_request("POST", f"add_result_for_case/{run_id}/{case_id}", json=payload)


@mcp.tool()
async def add_bulk_results(
    run_id: int,
    results: list[dict],
) -> dict:
    """Post many results in one TestRail call (much faster than looping add_result).

    Each entry in `results` must have at least `case_id` and `status` (string or id).
    Optional per-entry fields: comment, defects, elapsed, version, assignedto_id.
    """
    if not results:
        raise ValueError("results list is empty.")
    payload_results: list[dict] = []
    for r in results:
        case_id = r.get("case_id")
        if not case_id:
            raise ValueError(f"Each result needs a case_id: {r}")
        entry: dict[str, Any] = {
            "case_id": case_id,
            "status_id": _resolve_status(r.get("status") or r.get("status_id")),
        }
        for k in ("comment", "defects", "elapsed", "version", "assignedto_id"):
            if r.get(k) is not None:
                entry[k] = r[k]
        payload_results.append(entry)
    return await _tr_request(
        "POST", f"add_results_for_cases/{run_id}",
        json={"results": payload_results},
    )


@mcp.tool()
async def update_case(case_id: int, fields: dict) -> dict:
    """Patch an existing test case. `fields` is forwarded to TestRail as-is.

    Common keys: title, custom_preconds, custom_steps_separated, priority_id, type_id,
    refs. To rewrite steps, pass a list under custom_steps_separated:
    [{"content": "...", "expected": "..."}, ...].
    """
    if not isinstance(fields, dict) or not fields:
        raise ValueError("`fields` must be a non-empty dict of TestRail case fields.")
    # If steps come in our AI shape ({step, expected}), translate to TestRail shape
    if "steps" in fields and "custom_steps_separated" not in fields:
        fields["custom_steps_separated"] = [
            {"content": s.get("step") or s.get("content", ""),
             "expected": s.get("expected", "")}
            for s in fields.pop("steps")
        ]
    return await _tr_request("POST", f"update_case/{case_id}", json=fields)


COMPARE_RUNS_SYSTEM = """You compare two TestRail runs and write a regression
delta report in GitHub-flavoured Markdown. Output ONLY the report, no preamble.

Structure (exact headings):

## Headline
One sentence: did things get better or worse, and by how much.

## Regressions (was passing → now failing)
Bullet list, test title + one-line hypothesis if a comment hints at the cause.
Skip this section entirely (drop the heading) if empty.

## Fixed (was failing → now passing)
Bullet list of test titles.

## Newly tested
Tests that exist in B but not A.

## No longer covered
Tests that exist in A but not B.

## Verdict
**BETTER** / **WORSE** / **MIXED** — and one short paragraph why.

Rules:
- Cite test titles verbatim. No invented data.
- Order regressions by perceived severity (the worse-sounding comments first).
- Be terse — bullets, not paragraphs."""


@mcp.tool()
async def compare_runs(run_id_a: int, run_id_b: int) -> dict:
    """Diff two runs (A = older/baseline, B = newer) and ask Claude for a narrative.

    Returns the raw status delta plus a Markdown report.
    """
    tests_a, tests_b = await asyncio.gather(
        get_tests_in_run(run_id_a, limit=500),
        get_tests_in_run(run_id_b, limit=500),
    )
    results_b = await get_results_for_run(run_id_b, limit=500)
    latest_b_by_test = {}
    for r in results_b:
        tid = r.get("test_id")
        if tid and tid not in latest_b_by_test:
            latest_b_by_test[tid] = r

    by_case_a = {t["case_id"]: t for t in tests_a if t.get("case_id")}
    by_case_b = {t["case_id"]: t for t in tests_b if t.get("case_id")}

    regressions, fixes, new_in_b, gone_in_b = [], [], [], []
    for cid, ta in by_case_a.items():
        tb = by_case_b.get(cid)
        if not tb:
            gone_in_b.append({"title": ta["title"]})
            continue
        sa, sb = ta["status"], tb["status"]
        if sa == "passed" and sb == "failed":
            comment = (latest_b_by_test.get(tb["id"], {}).get("comment") or "")[:400]
            regressions.append({"title": tb["title"], "comment": comment})
        elif sa == "failed" and sb == "passed":
            fixes.append({"title": tb["title"]})
    for cid, tb in by_case_b.items():
        if cid not in by_case_a:
            new_in_b.append({"title": tb["title"]})

    delta = {
        "run_a": run_id_a,
        "run_b": run_id_b,
        "regressions": regressions,
        "fixes": fixes,
        "new_in_b": new_in_b,
        "gone_in_b": gone_in_b,
        "counts": {
            "regressions": len(regressions),
            "fixes": len(fixes),
            "new_in_b": len(new_in_b),
            "gone_in_b": len(gone_in_b),
        },
    }

    if not ANTHROPIC_API_KEY:
        return delta

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=GEN_MODEL,
        max_tokens=3000,
        system=COMPARE_RUNS_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(delta, ensure_ascii=False)}],
    )
    delta["report_markdown"] = msg.content[0].text.strip()
    return delta


@mcp.tool()
async def flaky_test_detector(
    case_id: int,
    project_id: int | None = None,
    last_n_runs: int = 10,
) -> dict:
    """Pull this case's result across the last N runs and flag flakiness.

    A test is "flaky" if it has BOTH passes and failures in the window AND
    flips between them at least twice. Returns the run-by-run trace.
    """
    pid = project_id or TR_PROJECT_ID
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    runs_data = await _tr_request("GET", f"get_runs/{pid}&limit={min(last_n_runs * 5, 250)}")
    runs = _unwrap(runs_data, "runs")[: max(last_n_runs * 5, last_n_runs)]
    trace: list[dict] = []
    for run in runs:
        if len(trace) >= last_n_runs:
            break
        # find test for this case in this run
        tests = await get_tests_in_run(run["id"], limit=500)
        match = next((t for t in tests if t.get("case_id") == case_id), None)
        if not match:
            continue
        # find latest result for that test in that run
        results = await get_results_for_run(run["id"], limit=500)
        latest = next((r for r in results if r.get("test_id") == match["id"]), None)
        trace.append({
            "run_id": run["id"],
            "run_name": run.get("name"),
            "completed": run.get("is_completed", False),
            "status": match["status"],
            "comment": (latest.get("comment") if latest else "") or "",
        })

    pass_count = sum(1 for t in trace if t["status"] == "passed")
    fail_count = sum(1 for t in trace if t["status"] == "failed")
    # Count flips in the status sequence
    statuses = [t["status"] for t in trace if t["status"] in ("passed", "failed")]
    flips = sum(1 for i in range(1, len(statuses)) if statuses[i] != statuses[i - 1])
    is_flaky = pass_count > 0 and fail_count > 0 and flips >= 2

    return {
        "case_id": case_id,
        "runs_scanned": len(trace),
        "passed": pass_count,
        "failed": fail_count,
        "flips": flips,
        "is_flaky": is_flaky,
        "trace": trace,
    }


@mcp.tool()
async def bootstrap_feature(
    source_type: str,
    source_value: str,
    project_id: int | None = None,
    new_suite_name: str | None = None,
    existing_suite_id: int | None = None,
    section_name: str = "General",
    style_from_suite_id: int | None = None,
    push: bool = False,
    dedupe: bool = True,
    lint: bool = True,
    find_gaps: bool = True,
    dedupe_threshold: float = 0.65,
) -> dict:
    """One-shot pipeline: ingest a feature spec, set up TestRail structure, generate cases, push.

    Required:
      - `source_type`: "confluence" | "jira" | "text"
      - `source_value`: page_id / issue_key / raw text

    Target:
      - `project_id` (defaults to env TESTRAIL_PROJECT_ID)
      - Either `new_suite_name` (creates a new suite) OR `existing_suite_id` (writes into it)
      - `section_name` — created inside that suite. Hierarchies allowed: "A > B > C".

    House style:
      - `style_from_suite_id` — pull style anchors from a populated suite (recommended
        when bootstrapping a brand-new empty suite). If omitted, no style anchors.

    Safety:
      - `push=False` (default) → dry-run: cases generated and returned, NOTHING is
        written to TestRail.
      - `push=True` → suite + section created, cases pushed.

    Returns a full report: suite, section, source meta, cases, created_ids, style info.
    """
    pid = project_id or TR_PROJECT_ID
    if not pid:
        raise ValueError("project_id required (or set TESTRAIL_PROJECT_ID in .env).")
    if not new_suite_name and not existing_suite_id:
        raise ValueError("Pass either new_suite_name or existing_suite_id.")
    if new_suite_name and existing_suite_id:
        raise ValueError("Pass either new_suite_name OR existing_suite_id, not both.")

    report: dict[str, Any] = {"project_id": pid, "push": push}

    # 1. Source fetch
    if source_type == "confluence":
        title, content, version = await _confluence_get_page(source_value)
        report["source"] = {"type": "confluence", "page_id": source_value, "version": version}
    elif source_type == "jira":
        issue = await _jira_get_issue(source_value)
        title, content, version = _jira_to_context(issue)
        report["source"] = {"type": "jira", "key": source_value, "version": version}
    elif source_type == "text":
        title = "Inline spec"
        content = source_value
        report["source"] = {"type": "text"}
    else:
        raise ValueError(f"Unknown source_type: {source_type!r}")
    report["feature_title"] = title

    # 2. House-style anchors (optional)
    style_examples: list[dict] = []
    chosen_anchor_section: dict | None = None
    if style_from_suite_id:
        try:
            chosen_anchor_section = await find_populated_section(
                project_id=pid, suite_id=style_from_suite_id, min_cases=3,
            )
            if chosen_anchor_section:
                style_examples = await _fetch_house_style_examples(
                    pid, style_from_suite_id, chosen_anchor_section["id"], n=5,
                )
        except Exception as e:
            report["style_warning"] = f"Could not load style anchors: {e}"
    report["style_anchor"] = (
        {"suite_id": style_from_suite_id, **(chosen_anchor_section or {})}
        if style_from_suite_id else None
    )
    report["style_examples_used"] = len(style_examples)

    # 3. Generate cases (no push yet — we wire it manually below)
    cases = _generate_cases_via_claude(
        title=title,
        content=content,
        section=section_name,
        style_examples=style_examples,
    )
    report["cases"] = cases
    report["cases_count"] = len(cases)

    # 4. Lint + coverage-gap analysis (non-blocking, advisory)
    if lint:
        try:
            report["lint"] = await lint_cases(cases, feature_title=title)
        except Exception as e:
            report["lint"] = {"error": str(e)}
    if find_gaps:
        try:
            report["coverage"] = await coverage_gaps(
                spec_text=content, cases=cases, feature_title=title
            )
        except Exception as e:
            report["coverage"] = {"error": str(e)}

    # 5. Resolve target suite (create new OR use existing) — needed for dedupe and push
    if push or (dedupe and existing_suite_id):
        if new_suite_name:
            if push:
                suite = await create_suite(
                    name=new_suite_name,
                    description=f"Generated via testrail-mcp bootstrap_feature from {report['source']}",
                    project_id=pid,
                )
                suite_id = suite["id"]
                report["suite"] = {"id": suite_id, "name": new_suite_name, "created": True}
            else:
                suite_id = None
                report["suite"] = {"name": new_suite_name, "created": False, "would_create_on_push": True}
        else:
            suite_id = existing_suite_id
            report["suite"] = {"id": suite_id, "created": False}
    else:
        suite_id = None
        report["suite"] = None

    # 6. Dedupe vs existing section content (only meaningful if target section exists)
    if dedupe and suite_id and existing_suite_id:
        # Resolve section without creating, only to check duplicates against an existing target
        try:
            existing_section_id = await _resolve_section(
                pid, suite_id, section_name, create_missing=False
            )
            dd = await dedupe_against_section(
                cases=cases, section_id=existing_section_id,
                project_id=pid, suite_id=suite_id, threshold=dedupe_threshold,
            )
            report["dedupe"] = {
                "duplicates_count": dd["duplicates_count"],
                "duplicates": dd["duplicates"],
                "kept_count": dd["kept_count"],
            }
            cases = [item["case"] for item in dd["kept"]]
        except Exception as e:
            report["dedupe"] = {"skipped_reason": str(e)}

    if not push:
        report["dry_run"] = True
        report["note"] = "No TestRail writes performed. Re-run with push=True to commit."
        return report

    # 7. Create section (hierarchy supported)
    _section_cache.clear()
    section_id = await _resolve_section(pid, suite_id, section_name, create_missing=True)
    report["section"] = {"id": section_id, "path": section_name}

    # 8. Push remaining (post-dedupe) cases
    created: list[dict] = []
    failed: list[dict] = []
    for c in cases:
        try:
            res = await create_test_case(section_id=section_id, case=c)
            created.append({"id": res["id"], "title": c["title"]})
            time.sleep(0.3)
        except Exception as e:
            failed.append({"title": c["title"], "error": str(e)})
    report["created"] = created
    report["failed"] = failed
    report["created_count"] = len(created)
    report["failed_count"] = len(failed)
    report["suite_url"] = (
        f"{TESTRAIL_BASE_URL}/index.php?/suites/view/{suite_id}" if TESTRAIL_BASE_URL else None
    )
    return report


def main() -> None:
    """Console entry point. Transport is selected via MCP_TRANSPORT env var:
      - unset / "stdio" → default stdio (Claude Desktop, Cursor, local uvx).
      - "http" → HTTP streamable transport on PORT (for hosted runtimes like Smithery).
      - "sse" → SSE transport on PORT (legacy hosted clients).
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        mcp.settings.host = os.getenv("HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("PORT", "8080"))
        mcp.run(transport="sse" if transport == "sse" else "streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
