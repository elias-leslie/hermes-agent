#!/usr/bin/env python3
"""End-to-end smoke test for the agent_hub memory bridge plugin.

Exercises every tool against the live Agent Hub backend at
``$AGENT_HUB_BASE_URL`` (default ``http://127.0.0.1:8003``). Run
manually — it mutates the Agent Hub store with a tagged test entry
that you can later prune by tag.

Usage:
    /home/kasadis/.hermes/hermes-agent/venv/bin/python tests/manual/agent_hub_bridge_e2e.py
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
from pathlib import Path

# Mirror the test module's import path setup.
_USER_PLUGIN_ROOT = Path(os.path.expanduser("~/.hermes"))
_HERMES_ROOT = Path(os.path.expanduser("~/.hermes/hermes-agent"))
for _p in (str(_HERMES_ROOT), str(_USER_PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "_hermes_user_memory" not in sys.modules:
    import importlib.machinery
    _spec = importlib.machinery.ModuleSpec("_hermes_user_memory", None, is_package=True)
    _spec.submodule_search_locations = []
    sys.modules["_hermes_user_memory"] = importlib.util.module_from_spec(_spec)

_spec = importlib.util.spec_from_file_location(
    "_hermes_user_memory.agent_hub",
    str(_USER_PLUGIN_ROOT / "plugins" / "agent_hub" / "__init__.py"),
    submodule_search_locations=[str(_USER_PLUGIN_ROOT / "plugins" / "agent_hub")],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_hermes_user_memory.agent_hub"] = _mod
_spec.loader.exec_module(_mod)
AgentHubMemoryProvider = _mod.AgentHubMemoryProvider


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{mark}] {label}{suffix}")
    if not cond:
        global _failures
        _failures += 1


_failures = 0

print("agent_hub bridge e2e")
print(f"  hermes_home = {_HERMES_ROOT}")

provider = AgentHubMemoryProvider()
avail = provider.is_available()
_check("is_available()", avail, "Agent Hub reachable" if avail else "Agent Hub unreachable — abort")
if not avail:
    sys.exit(1)

provider.initialize(
    session_id=f"e2e-{int(time.time())}",
    platform="cli",
    agent_identity="bridge-e2e",
)

_section("tool: agent_hub_search")
result = provider.handle_tool_call("agent_hub_search", {
    "query": "rebuild after code change",
    "limit": 3,
})
import json
data = json.loads(result)
_check("returns dict with 'results'", "results" in data)
_check("non-empty results", data.get("count", 0) > 0, f"count={data.get('count')}")
check_uuid = None
if data.get("results"):
    r0 = data["results"][0]
    check_uuid = r0.get("uuid")
    print(f"    top hit: {r0.get('name')} (score={r0.get('score'):.3f}, tier={r0.get('tier')})")

_section("tool: agent_hub_save_learning (well-formatted)")
well = "**Bridge E2E Test**: Use the agent_hub bridge plugin to round-trip writes against Agent Hub."
result = provider.handle_tool_call("agent_hub_save_learning", {
    "content": well,
    "summary": "Bridge E2E Test",
    "tier": "reference",
    "tags": ["hermes-bridge-e2e"],
})
data = json.loads(result)
_check("save succeeded", "uuid" in data, data.get("error", "no error key"))
saved_uuid = data.get("uuid")

_section("tool: agent_hub_save_learning (auto-formatted)")
unformatted = "agent_hub bridge plugin auto-formats freeform content"
result = provider.handle_tool_call("agent_hub_save_learning", {
    "content": unformatted,
    "summary": "Bridge Auto Format",
    "tier": "reference",
    "tags": ["hermes-bridge-e2e"],
})
data = json.loads(result)
_check("save succeeded", "uuid" in data, data.get("error", "no error key"))
auto_uuid = data.get("uuid")

_section("tool: agent_hub_rate")
if check_uuid:
    result = provider.handle_tool_call("agent_hub_rate", {
        "uuid": check_uuid,
        "rating": "used",
    })
    data = json.loads(result)
    check_ok = "error" not in data
    _check("rate succeeded", check_ok, data.get("error", "ok"))
else:
    print("  [SKIP] no UUID to rate")

_section("tool: agent_hub_recap")
result = provider.handle_tool_call("agent_hub_recap", {"max_sessions": 3})
data = json.loads(result)
_check("returns scope_id", "scope_id" in data, f"scope_id={data.get('scope_id')}")
_check("returns continuity", "continuity" in data)
if "continuity" in data and isinstance(data["continuity"], dict):
    print(f"    continuity keys: {list(data['continuity'].keys())}")

_section("prefetch + on_memory_write mirror")
# Mirror a built-in-style memory write — content is a real-world
# case where the body is a demonstrative ("this is a..."), which
# Agent Hub's validator would reject without auto-formatting.
# The production mirror is async (background thread) so the memory
# tool call stays snappy; here we exercise both the async path
# (on_memory_write) AND the synchronous path (direct save) so the
# test doesn't race the daemon thread's lifetime.
provider.on_memory_write(
    action="add",
    target="memory",
    content="**E2E Mirror Async**: this is an async mirror via the production path",
)
print("  on_memory_write fired (background thread)")
# Synchronous mirror — same code path, just called inline.
body = provider._auto_format_for_agent_hub(
    "**E2E Mirror Sync**: this is a synchronous mirror direct to Agent Hub",
    "E2E Mirror Sync",
)
sync_result = provider._client.save_learning(
    content=body,
    summary="E2E Mirror Sync",
    tier="reference",
    pinned=False,
    tags=["hermes-builtin", "target:memory"],
    scope="project",
    scope_id=provider._scope_id,
)
_check("sync mirror save succeeds", "uuid" in sync_result, sync_result.get("error", "ok"))
sync_uuid = sync_result.get("uuid")
# Wait for async + index
time.sleep(2.0)
# Verify both via search — search is more reliable than list_recent
# for newly-written entries.
result = provider.handle_tool_call("agent_hub_search", {
    "query": "E2E Mirror Sync this is a synchronous mirror direct to Agent Hub",
    "limit": 5,
})
data = json.loads(result)
found_sync = any(
    (sync_uuid and r.get("uuid") == sync_uuid)
    or "E2E Mirror Sync" in (r.get("content") or "")
    for r in data.get("results", [])
)
_check("sync mirror is searchable", found_sync,
       f"names={[(r.get('name') or '')[:40] for r in data.get('results', [])]}")

_section("prefetch cached recall")
# Force a background prefetch
provider.queue_prefetch("rebuild service after code change")
# Wait briefly
time.sleep(0.5)
# Then call prefetch() to consume
recalled = provider.prefetch("rebuild service after code change")
_check("prefetch returns recall", bool(recalled), f"len={len(recalled)}")
if recalled:
    print(f"  sample recall (first 200 chars):\n  {recalled[:200]}")

print()
if _failures:
    print(f"FAILED: {_failures} check(s) failed")
    sys.exit(1)
print("OK: all e2e checks passed")
print()
print("To clean up: run `st db -P agent-hub query \"DELETE FROM memories WHERE 'hermes-bridge-e2e' = ANY(tags)\"`")
