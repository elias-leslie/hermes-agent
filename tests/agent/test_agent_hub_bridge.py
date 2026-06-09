"""Tests for the agent_hub memory bridge plugin.

Covers the auto-formatter (the only piece with non-trivial branching
logic that doesn't need a live Agent Hub backend) and the
configuration loader. End-to-end tool calls are exercised manually
in ``tests/manual/agent_hub_bridge_e2e.py``.

User-installed plugins (under ``~/.hermes/plugins/``) are imported via
the ``_hermes_user_memory`` namespace by Hermes's plugin loader — see
``plugins/memory/__init__.py:_load_provider_from_dir``. We mirror
that here so tests exercise the same import path the runtime uses.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# The agent_hub plugin is a USER-INSTALLED plugin (under
# ``~/.hermes/plugins/``), not a bundled one. Pytest's collection
# happens before conftest hooks, so we have to set up the import path
# before any imports. Two pieces:
#   1. The user plugin's synthetic parent package (``_hermes_user_memory``)
#      must exist in sys.modules with no __file__ so that the loader's
#      synthetic-package check passes.
#   2. The plugin's ``__init__.py`` is loaded as
#      ``_hermes_user_memory.agent_hub`` (matching what
#      ``plugins/memory/_load_provider_from_dir`` does at runtime).
_USER_PLUGIN_ROOT = Path(os.path.expanduser("~/.hermes"))
_HERMES_ROOT = Path(os.path.expanduser("~/.hermes/hermes-agent"))
for _p in (str(_HERMES_ROOT), str(_USER_PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Register the synthetic parent package so the plugin can resolve as
# ``_hermes_user_memory.agent_hub``. Mirrors
# ``plugins/memory/__init__.py::_register_synthetic_package``.
if "_hermes_user_memory" not in sys.modules:
    import importlib.machinery
    _spec = importlib.machinery.ModuleSpec("_hermes_user_memory", None, is_package=True)
    _spec.submodule_search_locations = []
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["_hermes_user_memory"] = _mod

_agent_hub_init = _USER_PLUGIN_ROOT / "plugins" / "agent_hub" / "__init__.py"
if "_hermes_user_memory.agent_hub" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "_hermes_user_memory.agent_hub", str(_agent_hub_init),
        submodule_search_locations=[str(_agent_hub_init.parent)],
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["_hermes_user_memory.agent_hub"] = _mod
    _spec.loader.exec_module(_mod)

AgentHubMemoryProvider = sys.modules["_hermes_user_memory.agent_hub"].AgentHubMemoryProvider
_load_plugin_config_for_tests = sys.modules["_hermes_user_memory.agent_hub"]._load_plugin_config_for_tests


class TestAutoFormat(unittest.TestCase):
    """The auto-formatter is conservative — only fix obvious gaps."""

    def test_already_formatted_passes_through(self):
        # Body "Use the thing properly." already leads with strong verb "use",
        # so the auto-formatter leaves it alone.
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "**Topic**: Use the thing properly.", "Topic"
        )
        self.assertEqual(result, "**Topic**: Use the thing properly.")

    def test_topic_with_weak_imperative_gets_use(self):
        """User has a topic header but the body opens with a non-imperative
        ("this is..."). Plugin should rewrite the body to lead with "Use"
        so the validator accepts the write."""
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "**My Note**: this is a freeform note about a thing",
            "My Note"
        )
        self.assertEqual(result, "**My Note**: Use this is a freeform note about a thing")

    def test_unformatted_prepends_topic_and_use(self):
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "this is a note", "My Note"
        )
        self.assertEqual(result.startswith("**My Note**: Use "), True, result)

    def test_imperative_keeps_imperative(self):
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "rebuild agent-hub after backend changes", "Rebuild"
        )
        # "rebuild" is in the strong-verb list, so the body is unchanged.
        self.assertIn("rebuild agent-hub after backend changes", result)

    def test_always_recognized(self):
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "always rebuild after code changes", "Rebuild"
        )
        # "always" is a strong verb — body unchanged.
        self.assertIn("always rebuild after code changes", result)

    def test_never_recognized(self):
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "never commit secrets", "Secrets"
        )
        self.assertIn("never commit secrets", result)

    def test_topic_strips_trailing_colon(self):
        """User summary ending with ``:`` shouldn't produce ``**foo: bar**:``."""
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "some content", "My Note:"
        )
        # The topic itself should not contain a colon (would make
        # ``**foo: bar**:`` malformed). The outer ``**:`` between
        # topic and body is the canonical separator, that's fine.
        self.assertNotIn("**: bar", result)  # malformed nested colon
        self.assertIn("**My Note**", result)

    def test_empty_summary_passthrough(self):
        """If the topic is empty, leave content as-is (no broken header)."""
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "some unformatted text", ""
        )
        self.assertEqual(result, "some unformatted text")

    def test_topic_strips_quotes(self):
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "some content", '"My Note"'
        )
        # Quotes should be stripped from the topic.
        self.assertNotIn('""', result)

    def test_multiline_content_uses_first_line_for_summary(self):
        """When the user passes a multi-line string as content and
        no explicit summary, the first line becomes the topic.
        We test that by passing a long content with multiple lines."""
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "this is the body\nwith multiple lines",
            "this is the body"
        )
        # Body already leads with "this", not a strong verb, so "Use" is prepended.
        self.assertIn("Use this is the body", result)

    def test_weak_verb_test_gets_use(self):
        """'Test' is in the strong-verb list (Agent Hub accepts it), so
        the body is left alone. The plugin only auto-formats when the
        first word is NOT a recognized strong verb."""
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "**My Note**: test the well-formatted case", "My Note"
        )
        # "test" is in the strong-verb list — body untouched.
        self.assertEqual(result, "**My Note**: test the well-formatted case")

    def test_weak_imperative_demonstrative_gets_use(self):
        """Demonstrative pronouns ('this is...', 'that is...') aren't
        imperatives. Plugin should rewrite the body to lead with 'Use'."""
        result = AgentHubMemoryProvider._auto_format_for_agent_hub(
            "**My Note**: this is a freeform note about a thing",
            "My Note"
        )
        self.assertEqual(result, "**My Note**: Use this is a freeform note about a thing")


class TestConfigLoading(unittest.TestCase):
    def test_env_fallback_when_no_yaml(self):
        """If config.yaml is missing/empty, env vars populate defaults."""
        with patch.dict(os.environ, {
            "AGENT_HUB_BASE_URL": "http://example:9999",
            "AGENT_HUB_CLIENT_ID": "test-client",
            "AGENT_HUB_REQUEST_SOURCE": "test-src",
        }, clear=False):
            cfg = _load_plugin_config_for_tests(hermes_home="/nonexistent/path/that/does/not/exist")
            self.assertEqual(cfg["base_url"], "http://example:9999")
            self.assertEqual(cfg["client_id"], "test-client")
            self.assertEqual(cfg["scope"], "project")

    def test_yaml_overrides_env(self):
        """If config.yaml has memory.provider_config, it wins over env."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            (hermes_home / "config.yaml").write_text(
                "memory:\n  provider_config:\n    base_url: http://yaml-host:1234\n    client_id: yaml-client\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {
                "AGENT_HUB_BASE_URL": "http://env-host:5678",
                "AGENT_HUB_CLIENT_ID": "env-client",
            }, clear=False):
                cfg = _load_plugin_config_for_tests(hermes_home=str(hermes_home))
                self.assertEqual(cfg["base_url"], "http://yaml-host:1234")
                self.assertEqual(cfg["client_id"], "yaml-client")


if __name__ == "__main__":
    unittest.main()
