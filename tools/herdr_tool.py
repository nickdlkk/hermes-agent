#!/usr/bin/env python3
"""Hermes tool: control Herdr terminal workspace manager.

Provides pane/tab/workspace/agent management by wrapping `herdr` CLI commands.
Herdr must be running (herdr server start) before these tools are useful.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Optional

from tools.registry import registry, tool_error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _herdr_available() -> bool:
    return bool(shutil.which("herdr"))


def _run(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["herdr"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "herdr not found"
    except Exception as e:
        return 1, "", str(e)


# ---------------------------------------------------------------------------
# Tool: pane list
# ---------------------------------------------------------------------------

def herdr_list_panes_tool() -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH — is it installed?")
    code, out, err = _run(["pane", "list"])
    if code != 0:
        return tool_error(f"herdr pane list failed: {err or out}")
    return json.dumps({"ok": True, "output": out}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: pane split
# ---------------------------------------------------------------------------

def herdr_split_pane_tool(direction: str = "right") -> str:
    """Split a Herdr pane. direction: right|down"""
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    if direction not in ("right", "down"):
        return tool_error("direction must be 'right' (horizontal) or 'down' (vertical)")
    code, out, err = _run(["pane", "split", "--direction", direction])
    if code != 0:
        return tool_error(f"herdr pane split failed: {err or out}")
    return json.dumps({"ok": True, "output": out.strip()})


# ---------------------------------------------------------------------------
# Tool: pane close
# ---------------------------------------------------------------------------

def herdr_close_pane_tool(pane_id: Optional[str] = None) -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    args = ["pane", "close"]
    if pane_id:
        args.append(pane_id)
    code, out, err = _run(args)
    if code != 0:
        return tool_error(f"herdr pane close failed: {err or out}")
    return json.dumps({"ok": True, "output": out.strip()})


# ---------------------------------------------------------------------------
# Tool: agent start (spawn a new agent in a pane)
# ---------------------------------------------------------------------------

def herdr_start_agent_tool(agent_type: str, prompt: Optional[str] = None) -> str:
    """Start an agent in a Herdr pane.
    
    agent_type: claude|codex|cursor|opencode|devin|kimi|pi|opencode|qodercli
    prompt: optional initial prompt to send after starting
    """
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    
    valid = {"claude", "codex", "cursor", "opencode", "devin", "kimi", "pi", "qodercli", "copilot", "mastracode"}
    if agent_type not in valid:
        return tool_error(f"agent_type must be one of: {', '.join(sorted(valid))}")
    
    args = ["agent", "start", agent_type]
    code, out, err = _run(args, timeout=15)
    if code != 0:
        return tool_error(f"herdr agent start {agent_type} failed: {err or out}")
    
    result = {"ok": True, "output": out.strip()}
    if prompt:
        # Send the prompt to the newly started agent
        code2, out2, err2 = _run(["agent", "prompt", "--pane", "current", prompt], timeout=5)
        if code2 == 0:
            result["prompt_sent"] = True
        else:
            result["prompt_error"] = (err2 or out2).strip()
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: agent send-keys
# ---------------------------------------------------------------------------

def herdr_agent_send_keys_tool(keys: str, pane: Optional[str] = None) -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    args = ["agent", "send-keys"]
    if pane:
        args.extend(["--pane", pane])
    args.append(keys)
    code, out, err = _run(args, timeout=5)
    if code != 0:
        return tool_error(f"herdr agent send-keys failed: {err or out}")
    return json.dumps({"ok": True, "output": out.strip()})


# ---------------------------------------------------------------------------
# Tool: agent read
# ---------------------------------------------------------------------------

def herdr_agent_read_tool(pane: Optional[str] = None, lines: int = 30) -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    args = ["agent", "read"]
    if pane:
        args.extend(["--pane", pane])
    code, out, err = _run(args + ["--lines", str(lines)], timeout=5)
    if code != 0:
        return tool_error(f"herdr agent read failed: {err or out}")
    return json.dumps({"ok": True, "output": out}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: workspace create
# ---------------------------------------------------------------------------

def herdr_create_workspace_tool(name: Optional[str] = None) -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    args = ["workspace", "create"]
    if name:
        args.extend(["--name", name])
    code, out, err = _run(args, timeout=5)
    if code != 0:
        return tool_error(f"herdr workspace create failed: {err or out}")
    return json.dumps({"ok": True, "output": out.strip()})


# ---------------------------------------------------------------------------
# Tool: workspace list
# ---------------------------------------------------------------------------

def herdr_list_workspaces_tool() -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    code, out, err = _run(["workspace", "list"], timeout=5)
    if code != 0:
        return tool_error(f"herdr workspace list failed: {err or out}")
    return json.dumps({"ok": True, "output": out}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: tab new
# ---------------------------------------------------------------------------

def herdr_new_tab_tool(name: Optional[str] = None) -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    args = ["tab", "create"]
    if name:
        args.extend(["--label", name])
    code, out, err = _run(args, timeout=5)
    if code != 0:
        return tool_error(f"herdr tab create failed: {err or out}")
    return json.dumps({"ok": True, "output": out.strip()})


# ---------------------------------------------------------------------------
# Tool: server status
# ---------------------------------------------------------------------------

def herdr_server_status_tool() -> str:
    if not _herdr_available():
        return tool_error("herdr not found on PATH")
    code, out, err = _run(["status", "server"], timeout=5)
    if code != 0:
        return tool_error(f"herdr server status failed (server may not be running): {err or out}")
    return json.dumps({"ok": True, "output": out}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

PANE_LIST_SCHEMA = {
    "name": "herdr_list_panes",
    "description": "List all open panes in the Herdr terminal workspace. "
                   "Shows pane IDs, layout, and which pane is focused. "
                   "Requires herdr server to be running.",
    "parameters": {"type": "object", "properties": {}},
}

PANE_SPLIT_SCHEMA = {
    "name": "herdr_split_pane",
    "description": "Split a Herdr pane to create two side-by-side panes. "
                   "Creates room for parallel agents or side-by-side views.",
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["right", "down"],
                "description": "Split direction: right (horizontal, side by side) or down (vertical, stacked).",
                "default": "right",
            },
        },
        "required": [],
    },
}

PANE_CLOSE_SCHEMA = {
    "name": "herdr_close_pane",
    "description": "Close a specific Herdr pane by ID, or the current pane if no ID is given.",
    "parameters": {
        "type": "object",
        "properties": {
            "pane_id": {
                "type": "string",
                "description": "Pane ID to close. If omitted, closes the currently focused pane.",
            },
        },
        "required": [],
    },
}

AGENT_START_SCHEMA = {
    "name": "herdr_start_agent",
    "description": "Start a supported AI coding agent in a new Herdr pane. "
                   "Agent types: claude, codex, cursor, opencode, devin, kimi, pi, qodercli, copilot, mastracode. "
                   "Use herdr_split_pane first to create space.",
    "parameters": {
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "description": "Type of agent to start.",
                "enum": ["claude", "codex", "cursor", "opencode", "devin", "kimi", "pi", "qodercli", "copilot", "mastracode"],
            },
            "prompt": {
                "type": "string",
                "description": "Optional initial prompt to send to the agent once it starts.",
            },
        },
        "required": ["agent_type"],
    },
}

AGENT_SEND_KEYS_SCHEMA = {
    "name": "herdr_agent_send_keys",
    "description": "Send key presses to an agent running in a Herdr pane. "
                   "Use to inject commands or text (e.g. 'Build a REST API\\n' sends the text then Enter).",
    "parameters": {
        "type": "object",
        "properties": {
            "keys": {"type": "string", "description": "Keys to send (supports Enter, Ctrl+C, etc.)."},
            "pane": {"type": "string", "description": "Target pane ID. Defaults to current pane."},
        },
        "required": ["keys"],
    },
}

AGENT_READ_SCHEMA = {
    "name": "herdr_agent_read",
    "description": "Read the terminal output of an agent pane in Herdr. "
                   "Useful to check what an agent is currently doing.",
    "parameters": {
        "type": "object",
        "properties": {
            "pane": {"type": "string", "description": "Pane ID. Defaults to current pane."},
            "lines": {"type": "integer", "description": "Number of lines to read.", "default": 30},
        },
        "required": [],
    },
}

WORKSPACE_CREATE_SCHEMA = {
    "name": "herdr_create_workspace",
    "description": "Create a new named Herdr workspace. "
                   "Workspaces group tabs, panes, and agents for separate projects.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Workspace name."},
        },
        "required": [],
    },
}

WORKSPACE_LIST_SCHEMA = {
    "name": "herdr_list_workspaces",
    "description": "List all Herdr workspaces.",
    "parameters": {"type": "object", "properties": {}},
}

TAB_NEW_SCHEMA = {
    "name": "herdr_new_tab",
    "description": "Create a new Herdr tab.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tab name."},
        },
        "required": [],
    },
}

SERVER_STATUS_SCHEMA = {
    "name": "herdr_server_status",
    "description": "Check if the Herdr server is running. "
                   "Most other herdr_* tools require the server to be running first.",
    "parameters": {"type": "object", "properties": {}},
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="herdr_list_panes",
    toolset="terminal",
    schema=PANE_LIST_SCHEMA,
    handler=lambda args, **kw: herdr_list_panes_tool(),
    check_fn=_herdr_available,
    emoji="🖥️",
)

registry.register(
    name="herdr_split_pane",
    toolset="terminal",
    schema=PANE_SPLIT_SCHEMA,
    handler=lambda args, **kw: herdr_split_pane_tool(
        direction=args.get("direction", "horizontal"),
    ),
    check_fn=_herdr_available,
    emoji="🪟",
)

registry.register(
    name="herdr_close_pane",
    toolset="terminal",
    schema=PANE_CLOSE_SCHEMA,
    handler=lambda args, **kw: herdr_close_pane_tool(pane_id=args.get("pane_id")),
    check_fn=_herdr_available,
    emoji="❌",
)

registry.register(
    name="herdr_start_agent",
    toolset="terminal",
    schema=AGENT_START_SCHEMA,
    handler=lambda args, **kw: herdr_start_agent_tool(
        agent_type=args["agent_type"],
        prompt=args.get("prompt"),
    ),
    check_fn=_herdr_available,
    emoji="🤖",
)

registry.register(
    name="herdr_agent_send_keys",
    toolset="terminal",
    schema=AGENT_SEND_KEYS_SCHEMA,
    handler=lambda args, **kw: herdr_agent_send_keys_tool(
        keys=args["keys"],
        pane=args.get("pane"),
    ),
    check_fn=_herdr_available,
    emoji="⌨️",
)

registry.register(
    name="herdr_agent_read",
    toolset="terminal",
    schema=AGENT_READ_SCHEMA,
    handler=lambda args, **kw: herdr_agent_read_tool(
        pane=args.get("pane"),
        lines=args.get("lines", 30),
    ),
    check_fn=_herdr_available,
    emoji="📖",
)

registry.register(
    name="herdr_create_workspace",
    toolset="terminal",
    schema=WORKSPACE_CREATE_SCHEMA,
    handler=lambda args, **kw: herdr_create_workspace_tool(name=args.get("name")),
    check_fn=_herdr_available,
    emoji="📁",
)

registry.register(
    name="herdr_list_workspaces",
    toolset="terminal",
    schema=WORKSPACE_LIST_SCHEMA,
    handler=lambda args, **kw: herdr_list_workspaces_tool(),
    check_fn=_herdr_available,
    emoji="📋",
)

registry.register(
    name="herdr_new_tab",
    toolset="terminal",
    schema=TAB_NEW_SCHEMA,
    handler=lambda args, **kw: herdr_new_tab_tool(name=args.get("name")),
    check_fn=_herdr_available,
    emoji="➕",
)

registry.register(
    name="herdr_server_status",
    toolset="terminal",
    schema=SERVER_STATUS_SCHEMA,
    handler=lambda args, **kw: herdr_server_status_tool(),
    check_fn=_herdr_available,
    emoji="🟢",
)
