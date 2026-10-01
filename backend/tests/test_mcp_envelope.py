"""Every MCP tool answers in the same envelope (blueprint B13, S4a).

`config_get` returned its payload as the JSON-RPC result directly while
every other tool wrapped it in `_tool_text(...)`. The SDK's client reads
`result.content` and substitutes `{}` when there is none, so a container
calling `ctx.config.steps()` was told there were no steps — and told it
*silently*, because an empty list is a legal answer. The reference echo
agent recorded `step_config: None` for that reason (Codex P2).

One tool answering in a different shape is a shape nobody notices until
something reads it, so the guard is the shape of every tool's answer,
not this one tool's.
"""
from __future__ import annotations

import inspect
import json
import re
import pathlib

import pytest

from app.routers import mcp


def advertised_tool_names() -> list[str]:
    return [tool["name"] for tool in mcp._TOOLS]


def test_the_advertised_list_is_not_empty():
    """A scan over an empty list proves nothing."""
    assert len(advertised_tool_names()) >= 5


@pytest.mark.parametrize("name", advertised_tool_names())
def test_every_tool_returns_through_the_envelope(name):
    """Read from the source: each tool's branch in ``_call_tool`` must
    hand its answer to ``_tool_text``. A test that called every tool
    would need every capability, a run and a database; what is being
    checked here is that no branch returns a bare payload."""
    source = inspect.getsource(mcp._call_tool)
    # The branch for this tool, up to the next tool's branch.
    branches = re.split(r'\n    if name == "', source)
    branch = next((b for b in branches if b.startswith(f'{name}"')), None)

    assert branch is not None, f"no branch for {name} in _call_tool"
    returns = re.findall(r"\n\s+return (\S+)", branch)
    assert returns, f"the {name} branch returns nothing"
    assert all(
        r.startswith("_tool_text") for r in returns
    ), f"{name} returns outside the MCP content envelope: {returns}"


def test_the_envelope_is_what_the_sdk_client_reads():
    """The two halves of the contract, pinned against each other: the
    server writes `content[].text` as JSON, and the SDK reads exactly
    that and nothing else."""
    payload = mcp._tool_text({"steps": [{"step_id": "think"}]})

    assert payload["content"][0]["type"] == "text"
    assert json.loads(payload["content"][0]["text"]) == {"steps": [{"step_id": "think"}]}

    sdk = pathlib.Path(__file__).resolve().parents[2] / (
        "sdk/python/librerun-agent/src/librerun_agent/_mcp.py"
    )
    text = sdk.read_text()
    assert '(payload.get("result") or {}).get("content")' in text, (
        "the SDK client no longer reads result.content — this test and the "
        "envelope above are describing different protocols"
    )
