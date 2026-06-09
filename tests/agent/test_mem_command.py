"""Tests for the /mem slash command handler.

The handler is a thin CLI wrapper around the agent_hub plugin — the
plugin itself is covered by ``test_agent_hub_bridge.py`` and the
end-to-end script ``tests/manual/agent_hub_bridge_e2e.py``. This
file focuses on the handler's argument parsing, error states, and
output shape.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# Same user-plugin path setup as the bridge tests.
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

_agent_hub_init = _USER_PLUGIN_ROOT / "plugins" / "agent_hub" / "__init__.py"
if "_hermes_user_memory.agent_hub" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "_hermes_user_memory.agent_hub", str(_agent_hub_init),
        submodule_search_locations=[str(_agent_hub_init.parent)],
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["_hermes_user_memory.agent_hub"] = _mod
    _spec.loader.exec_module(_mod)


class TestMemCommandRegistry(unittest.TestCase):
    def test_mem_command_registered(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        mem = next((c for c in COMMAND_REGISTRY if c.name == "mem"), None)
        self.assertIsNotNone(mem, "/mem must be in COMMAND_REGISTRY")
        self.assertTrue(mem.cli_only, "/mem is a CLI-only command")
        self.assertIn("search", mem.args_hint)
        self.assertIn("save", mem.args_hint)
        self.assertIn("rate", mem.args_hint)
        self.assertIn("recap", mem.args_hint)


class TestMemHandlerDispatch(unittest.TestCase):
    """The handler must dispatch on the first token and produce output
    for each subcommand. We test by calling the bound method on a
    stub class that mimics HermesCLI's surface."""

    def setUp(self):
        # Build a minimal class with the handler bound, so we can call it.
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        self._mixin_handler = CLICommandsMixin._handle_mem_command

    def _call(self, *cmd_tokens, provider=None):
        """Invoke the handler with the given /mem body. Returns stdout."""
        cmd = "/mem " + " ".join(cmd_tokens) if cmd_tokens else "/mem"
        # Build a real class instance with the handler bound, so MRO
        # lookup finds the actual method (not a MagicMock attr).
        # MagicMock's auto-attrs shadow real method names when the
        # attribute doesn't exist on the mock's class — using a real
        # subclass of HermesCLI surfaces the bound methods correctly.
        from cli import HermesCLI
        stub_self = HermesCLI.__new__(HermesCLI)
        # Set just the surface the handler touches. Skip __init__ —
        # we'd be building a 100+ attribute CLI just to call one method.
        stub_self.agent = type("A", (), {"session_id": "test-session"})()

        # Patch the plugin loader to return our stub provider
        def fake_load(name):
            return provider
        with patch("plugins.memory.load_memory_provider", fake_load):
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    self._mixin_handler(stub_self, cmd)
                except Exception as e:
                    # Surface handler errors to the test, not just silent fail
                    raise AssertionError(
                        f"handler raised {type(e).__name__}: {e}"
                    ) from e
            return buf.getvalue()

    def test_help_subcommand(self):
        out = self._call("help")
        self.assertIn("/mem", out)
        self.assertIn("search", out)
        self.assertIn("save", out)
        self.assertIn("rate", out)

    def test_bare_mem_shows_bridge_inactive_when_no_provider(self):
        out = self._call()
        self.assertIn("not active", out)
        self.assertIn("memory.provider", out)

    def test_bare_mem_shows_bridge_inactive_when_provider_unavailable(self):
        provider = MagicMock()
        provider.is_available.return_value = False
        out = self._call(provider=provider)
        self.assertIn("not active", out)

    def test_search_subcommand_prints_query(self):
        provider = MagicMock()
        provider.is_available.return_value = True
        provider._client = MagicMock()  # already initialized
        provider.handle_tool_call.return_value = (
            '{"results": [{"uuid": "abc-123", "name": "Test Memory", '
            '"tier": "mandate", "score": 0.95, '
            '"content": "**Topic**: Use this.", "helpful_count": 2, '
            '"harmful_count": 0}], "count": 1, "query": "test"}'
        )
        out = self._call("search", "test", provider=provider)
        self.assertIn("Search", out)
        self.assertIn("abc-123", out)
        self.assertIn("MANDATE", out)
        self.assertIn("Test Memory", out)
        # Verify the right tool was called
        call = provider.handle_tool_call.call_args
        self.assertEqual(call.args[0], "agent_hub_search")
        self.assertEqual(call.args[1]["query"], "test")

    def test_search_subcommand_without_query_shows_usage(self):
        provider = MagicMock()
        provider.is_available.return_value = True
        provider._client = MagicMock()
        out = self._call("search", provider=provider)
        self.assertIn("Usage", out)
        provider.handle_tool_call.assert_not_called()

    def test_save_subcommand_calls_save_learning(self):
        provider = MagicMock()
        provider.is_available.return_value = True
        provider._client = MagicMock()
        provider.handle_tool_call.return_value = (
            '{"uuid": "new-uuid-456", "status": "provisional", '
            '"is_duplicate": false, "scope_id": "profile-default"}'
        )
        out = self._call("save", "**Test Note**: Use this rule.", provider=provider)
        self.assertIn("Saved", out)
        self.assertIn("new-uuid-456", out)
        call = provider.handle_tool_call.call_args
        self.assertEqual(call.args[0], "agent_hub_save_learning")
        self.assertEqual(call.args[1]["content"], "**Test Note**: Use this rule.")
        self.assertEqual(call.args[1]["summary"], "**Test Note**: Use this rule."[:200])

    def test_rate_subcommand_validates_rating(self):
        provider = MagicMock()
        provider.is_available.return_value = True
        provider._client = MagicMock()
        out = self._call("rate", "abc-123", "wrongrating", provider=provider)
        self.assertIn("Usage", out)
        provider.handle_tool_call.assert_not_called()

    def test_rate_subcommand_calls_rate(self):
        provider = MagicMock()
        provider.is_available.return_value = True
        provider._client = MagicMock()
        provider.handle_tool_call.return_value = (
            '{"helpful_count": 3, "harmful_count": 0}'
        )
        out = self._call("rate", "abc-123", "helpful", provider=provider)
        self.assertIn("Rated", out)
        self.assertIn("abc-123", out)
        call = provider.handle_tool_call.call_args
        self.assertEqual(call.args[0], "agent_hub_rate")
        self.assertEqual(call.args[1]["rating"], "helpful")

    def test_recap_subcommand_shows_recent(self):
        provider = MagicMock()
        provider.is_available.return_value = True
        provider._client = MagicMock()
        provider.handle_tool_call.return_value = (
            '{"scope_id": "profile-test", '
            '"recent": [{"uuid": "u1", "name": "Recent Memory", '
            '"tier": "reference", "content": "Some content"}], '
            '"continuity": {"markdown": "## Recent\\n- yesterday: did thing"}}'
        )
        out = self._call("recap", provider=provider)
        self.assertIn("profile-test", out)
        self.assertIn("Recent Memory", out)
        self.assertIn("REFERENCE", out)
        self.assertIn("Continuity", out)

    def test_unknown_subcommand_shows_help_hint(self):
        provider = MagicMock()
        provider.is_available.return_value = True
        provider._client = MagicMock()
        out = self._call("frobnicate", provider=provider)
        self.assertIn("Unknown subcommand", out)
        self.assertIn("frobnicate", out)


if __name__ == "__main__":
    unittest.main()
